import asyncio
from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from sqlalchemy import text

from danmaku.adapters.bilibili import BilibiliSource, platform_error
from danmaku.application.sessions import LocalSession, LocalSessions
from danmaku.domain.inputs import SourceBatch, SourceItem, VideoPart
from danmaku.domain.workbench import Principal, WorkbenchError
from danmaku.persistence.database import Database
from danmaku.persistence.repository import ProjectRow, TaskEventRow, TaskRow

from test_workbench import API, ORIGIN, PROJECT, create_project, open_client, connect_session, wait_task

BINARY = {"Content-Type": "application/octet-stream"}
SRT = "1\n00:00:01,000 --> 00:00:04,500\n合成中文\n第二行\n"
ASS = r"""[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:03.10,0:00:05.20,Main,,0,0,0,,{\i1}虚构对白{\i0}\N下一行
Dialogue: 0,0:00:01.00,0:00:04.00,Alt,,0,0,0,,同一时间轴
Comment: 0,0:00:01.00,0:00:04.00,Alt,,0,0,0,,虚构注释
Dialogue: 0,0:00:02.00,0:00:03.00,Alt,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}
"""


def upload(client, project, headers, blob, format="srt", encoding="utf-8"):
    return client.post(f"{API}/projects/{project}/subtitles",
                       params={"format": format, "encoding": encoding},
                       headers=headers | BINARY, content=blob)


def test_project_parts_and_subtitle_upload(tmp_path):
    client = open_client(tmp_path)
    with client:
        headers = connect_session(client)
        parts = client.get(f"{API}/videos/{PROJECT['source_bvid']}/parts").json()
        assert [part["page"] for part in parts] == [1, 2]
        invalid = client.post(f"{API}/projects?source=mock", headers=headers,
                              json={**PROJECT, "source_page": 3})
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "video_part_required"
        response = client.post(f"{API}/projects?source=mock", headers=headers,
                               json={**PROJECT, "source_page": 2})
        assert response.status_code == 201
        project = response.json()["id"]
        path = f"{API}/projects/{project}"
        assert client.get(f"{path}/inputs").json()["parts"]["source"] == parts[1]

        original = SRT.replace("00:00:04,500", "01:00:00,001").encode("gb18030")
        bad_encoding = upload(client, project, headers, original)
        assert bad_encoding.status_code == 422
        assert bad_encoding.json()["error"]["code"] == "subtitle_bad_encoding"
        srt = upload(client, project, headers, original, encoding="gb18030")
        assert srt.status_code == 201
        assert srt.json()["sha256"] == sha256(original).hexdigest()
        assert upload(client, project, headers, original, encoding="gb18030").json() == srt.json()
        ass = upload(client, project, headers, ASS.encode(), format="ass")
        assert ass.status_code == 201
        assert ass.json()["cue_count"] == 2
        assert ass.json()["warnings"] == [{"line": 7, "code": "non_text"}, {"line": 8, "code": "non_text"}]
        txt = upload(client, project, headers, ASS.encode(), format="txt")
        assert txt.status_code == 201
        assert txt.json()["format"] == "txt"
        assert txt.json()["cue_count"] == ass.json()["cue_count"]
        assert txt.json()["warnings"] == ass.json()["warnings"]
        invalid_txt = upload(client, project, headers, b"plain text", format="txt")
        assert invalid_txt.status_code == 422
        assert "TXT 文件不符合要求" in invalid_txt.json()["error"]["message"]
        detail = client.get(path).json()
        assert sorted(detail["subtitle_version_ids"]) == sorted(
            version.json()["subtitle_version_id"] for version in (srt, ass, txt)
        )

        for malformed in ("00:00:00,001", "00:99:00,001"):
            result = upload(client, project, headers, SRT.replace("00:00:04,500", malformed).encode())
            assert result.status_code == 422 and "第 2 行" in result.json()["error"]["message"]
        at_video_end = original.replace(b"00:00:01,000", b"01:00:00,000")
        result = upload(client, project, headers, at_video_end, encoding="gb18030")
        assert result.status_code == 422 and "第 2 行" in result.json()["error"]["message"]
        malformed_txt = upload(client, project, headers, SRT.replace("00:00:01,000", "00:99:01,000").encode(), "txt")
        assert malformed_txt.json()["error"]["code"] == "subtitle_invalid_cue"
        assert malformed_txt.status_code == 422 and "第 2 行" in malformed_txt.json()["error"]["message"]
        assert upload(client, project, headers, b"").json()["error"]["code"] == "subtitle_empty"
        assert upload(client, project, headers, b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
        assert upload(client, project, {}, original).status_code == 403
        assert upload(client, "other-project", headers, original).status_code == 404
        assert client.get(f"{API}/projects/other-project/inputs").status_code == 404

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
        cues = [json.loads(row[0]) for row in conn.execute("SELECT cue FROM subtitle_cues")]
        assert {cue["plaintext"] for cue in cues} == {"合成中文\n第二行", "虚构对白\n下一行", "同一时间轴"}
        assert {cue["style"] for cue in cues} == {"Default", "Main", "Alt"}
        assert next(cue["end_ms"] for cue in cues if cue["style"] == "Default") == 3_600_001
        assert conn.execute("SELECT count(*) FROM subtitle_versions").fetchone() == (3,)
        saved = conn.execute("SELECT original FROM subtitle_versions WHERE id=?",
                             (srt.json()["subtitle_version_id"],)).fetchone()
        assert saved == (original,)


@pytest.mark.parametrize("format", ["srt", "txt"])
@pytest.mark.parametrize("codec, bom, encoding", [
    ("utf-8-sig", b"", "utf-8"),
    ("utf-16-le", BOM_UTF16_LE, "utf-16"),
    ("utf-16-be", BOM_UTF16_BE, "utf-16"),
])
def test_upload_srt_content_with_bom_and_formatting(tmp_path, format, codec, bom, encoding):
    subtitle = (
        '1\r\n00:00:01,125 --> 00:00:04,750\r\n'
        '<font color="#ffffff"><i>合成台詞 &amp; 記号</i></font><br />次の行 &lt;b&gt;\r\n'
        'R&notebook\r\n'
    )
    original = bom + subtitle.encode(codec)
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        path = f"{API}/projects/{project}/subtitles"
        filename = f"synthetic.{format}"
        params = {"format": format, "filename": filename, "language": "japanese"}
        response = client.post(path, params=params, headers=headers | BINARY, content=original)
        assert response.status_code == 201
        version = response.json()
        assert (version["format"], version["filename"]) == ("srt", filename)
        assert version["encoding"] == encoding
        assert version["cue_count"] == 1
        assert client.post(path, params=params | {"encoding": encoding},
                           headers=headers | BINARY, content=original).json() == version
        inputs = client.get(f"{API}/projects/{project}/inputs").json()
        assert inputs["workflow"]["japanese_subtitle_id"] == version["subtitle_version_id"]

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
        assert conn.execute("SELECT original FROM subtitle_versions").fetchone() == (original,)
        cue = json.loads(conn.execute("SELECT cue FROM subtitle_cues").fetchone()[0])
        assert (cue["start_ms"], cue["end_ms"]) == (1125, 4750)
        assert cue["plaintext"] == "合成台詞 & 記号\n次の行 <b>\nR&notebook"


def test_delete_unused_subtitle_is_owner_scoped_and_removes_original_and_cues(tmp_path):
    sessions = LocalSessions()
    with open_client(tmp_path, sessions=sessions) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        other_project = create_project(client, headers)
        path = f"{API}/projects/{project}"
        removed = upload(client, project, headers, SRT.encode()).json()["subtitle_version_id"]
        kept = upload(client, project, headers, ASS.encode(), format="ass").json()["subtitle_version_id"]
        endpoint = f"{path}/subtitles/{removed}"

        assert client.delete(endpoint).status_code == 403
        assert client.delete(f"{API}/projects/{other_project}/subtitles/{removed}", headers=headers).status_code == 404
        owner_cookie = client.cookies.get("danmaku_session")
        other = LocalSession(Principal(id="other-synthetic-owner"), "other-csrf", float("inf"))
        sessions.sessions["other-session"] = other
        client.cookies.clear()
        client.cookies.set("danmaku_session", "other-session")
        assert client.delete(endpoint, headers={"Origin": ORIGIN, "X-CSRF-Token": other.csrf_token}).status_code == 404
        client.cookies.clear()
        client.cookies.set("danmaku_session", owner_cookie)

        response = client.delete(endpoint, headers=headers)
        assert (response.status_code, response.content) == (204, b"")
        repeated = client.delete(endpoint, headers=headers)
        assert repeated.status_code == 404
        assert repeated.json()["error"]["code"] == "subtitle_not_found"
        assert client.get(path).json()["subtitle_version_ids"] == [kept]
        assert [row["subtitle_version_id"] for row in client.get(f"{path}/inputs").json()["subtitles"]] == [kept]

        with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
            assert conn.execute("SELECT id FROM subtitle_versions").fetchall() == [(kept,)]
            assert conn.execute("SELECT DISTINCT version_id FROM subtitle_cues").fetchall() == [(kept,)]

        uploaded = upload(client, project, headers, SRT.encode())
        assert uploaded.status_code == 201 and uploaded.json()["subtitle_version_id"] == removed


def test_delete_rejects_both_subtitle_roles_used_in_analysis(tmp_path):
    async def execute(record):
        part = VideoPart(bvid=record.source_bvid, page=1, cid="42", title="合成分 P", duration_ms=12_000)
        return SourceBatch(part=part, segment_count=1, segment=0, items=[])

    with open_client(tmp_path, execute_batch=execute) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        path = f"{API}/projects/{project}"
        download = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "download"},
                               json={"kind": "download"})
        assert download.status_code == 202
        snapshot = download.json()["id"]
        wait_task(client, snapshot, "succeeded")
        chinese = upload(client, project, headers, SRT.encode()).json()["subtitle_version_id"]
        japanese = upload(client, project, headers, ASS.encode(), format="ass").json()["subtitle_version_id"]
        analysis = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "analysis"}, json={
            "kind": "classify", "snapshot_id": snapshot, "subtitle_version_id": chinese,
            "japanese_subtitle_version_id": japanese, "llm_config_version": 0,
        })
        assert analysis.status_code == 202
        for version in (chinese, japanese):
            rejected = client.delete(f"{path}/subtitles/{version}", headers=headers)
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "subtitle_in_use"
        wait_task(client, analysis.json()["id"], "succeeded")
        assert set(client.get(path).json()["subtitle_version_ids"]) == {chinese, japanese}


class MockLiveSource:
    def __init__(self, *, blocked=None, after_restart=False):
        self.blocked = blocked
        self.after_restart = after_restart
        self.requested = []

    async def parts(self, bvid):
        assert not self.after_restart
        return [VideoPart(bvid=bvid, page=2, cid="123456", title="合成分 P", duration_ms=400_000)]

    async def fetch(self, part, segment, count):
        self.requested.append((part.cid, segment))
        assert count == 2
        if segment == 1 and self.blocked is not None:
            self.blocked.set()
            await asyncio.Future()
        item = SourceItem(platform_id="same-id", text="合成弹幕", time_ms=1000,
                          color=0xAABBCC, font_size=25, mode=1)
        entries = [
            item.model_copy(update={"text": ""}),
            item.model_copy(update={"platform_id": "blank", "text": " \t\u3000"}),
            item,
            item,
        ] if segment == 0 else [
            item, item.model_copy(update={"platform_id": None, "time_ms": 370_000, "color": "special"}),
            item.model_copy(update={"platform_id": None, "time_ms": 400_001}),
        ]
        return SourceBatch(part=part, segment_count=count, segment=segment, items=entries)


def test_bilibili_download_preserves_old_snapshots_and_analysis_inputs(tmp_path):
    class Source(MockLiveSource):
        fail = False

        async def fetch(self, part, segment, count):
            if self.fail:
                raise WorkbenchError(403, "bilibili_access_denied", "合成错误")
            return await super().fetch(part, segment, count)

    source = Source()
    client = open_client(tmp_path, live_source=source)
    with client:
        headers = connect_session(client)
        project = create_project(client, headers, source_page=2)
        path = f"{API}/projects/{project}"

        def fetch(key, state="succeeded"):
            created = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": key},
                                  json={"kind": "download", "mode": "bilibili"})
            assert created.status_code == 202
            task_id = created.json()["id"]
            wait_task(client, task_id, state)
            return task_id

        def snapshots():
            return {snapshot["id"]: snapshot["state"] for snapshot in client.get(f"{path}/inputs").json()["snapshots"]}

        analyzed = fetch("first")
        chinese = upload(client, project, headers, SRT.encode()).json()["subtitle_version_id"]
        japanese = upload(client, project, headers, ASS.encode(), format="ass").json()["subtitle_version_id"]
        analysis = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "analysis"}, json={
            "kind": "classify", "snapshot_id": analyzed, "subtitle_version_id": chinese,
            "japanese_subtitle_version_id": japanese, "llm_config_version": 0,
        })
        assert analysis.status_code == 202
        run_id = analysis.json()["id"]
        wait_task(client, run_id, "succeeded")

        obsolete = fetch("second")
        assert snapshots() == {obsolete: "complete", analyzed: "complete"}

        source.fail = True
        failed = fetch("failed", "failed")
        assert snapshots() == {failed: "draft", obsolete: "complete", analyzed: "complete"}

        source.fail = False
        latest = fetch("latest")
        expected = {latest: "complete", failed: "draft", obsolete: "complete", analyzed: "complete"}
        assert snapshots() == expected
        assert client.get(path).json()["snapshot_ids"] == list(expected)
        assert client.get(f"{path}/runs/{run_id}").json()["snapshot_id"] == analyzed
        assert len(client.get(f"{path}/runs/{run_id}/classifications").json()["items"]) == 3
        assert fetch("second") == obsolete
        assert snapshots() == expected

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
        assert set(conn.execute("SELECT id FROM source_snapshots")) == {(key,) for key in expected}
        assert set(conn.execute("SELECT DISTINCT snapshot_id FROM source_items")) == {(latest,), (obsolete,), (analyzed,)}


@pytest.mark.parametrize("difference", [1000, 1001])
def test_target_only_project_merges_numbered_sources_and_persists_workflow(tmp_path, difference):
    second_bvid = "BV0000000003"

    class Source:
        async def parts(self, bvid):
            duration = 12_000 + (difference if bvid == second_bvid else 0)
            return [VideoPart(bvid=bvid, page=1, cid="42", title="合成分 P",
                              video_title="合成视频", duration_ms=duration)]

        async def fetch(self, part, segment, count):
            items = [SourceItem(platform_id="1000", text="合成弹幕", time_ms=1000, color=0x12AB34,
                                font_size=25, mode=5)]
            return SourceBatch(part=part, segment_count=count, segment=segment, items=items)

    with open_client(tmp_path, live_source=Source()) as client:
        headers = connect_session(client)
        target = {"target_bvid": PROJECT["target_bvid"], "target_page": 1}
        created = client.post(f"{API}/projects?source=bilibili", headers=headers, json=target)
        assert created.status_code == 201
        project = created.json()
        assert project["name"] == "合成视频" and project["source_bvid"] is None
        path = f"{API}/projects/{project['id']}"
        assert client.get(f"{path}/inputs").json()["parts"]["source"] is None
        assert client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "no-source"},
                           json={"kind": "download"}).json()["error"]["code"] == "source_required"

        source_ids = []
        for index, bvid in enumerate((PROJECT["source_bvid"], second_bvid), 1):
            submitted = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": f"source-{index}"},
                                    json={"kind": "download", "mode": "bilibili", "source_bvid": bvid, "source_page": 1})
            source_id = submitted.json()["id"]
            source_ids.append(source_id)
            wait_task(client, source_id, "succeeded")
            newest = client.get(f"{path}/inputs").json()["snapshots"][0]
            assert newest["name"] == f"{index:02} 合成视频 P1 {bvid}"
            assert newest["part"]["bvid"] == bvid

        request = {"snapshot_ids": source_ids}
        merge_endpoint = f"{path}/snapshots/merge"
        result = client.post(merge_endpoint, headers=headers, json=request)
        if difference > 1000:
            assert result.status_code == 409 and result.json()["error"]["code"] == "snapshot_duration_mismatch"
            assert len(client.get(f"{path}/inputs").json()["snapshots"]) == 2
            request["confirm_duration_mismatch"] = True
            result = client.post(merge_endpoint, headers=headers, json=request)
        assert result.status_code == 200
        merged = result.json()
        assert merged["name"] == "合并01+02" and merged["item_count"] == 2
        assert merged["part"]["duration_ms"] == 12_000 + difference
        assert client.post(merge_endpoint, headers=headers, json=request).json()["id"] == merged["id"]
        assert len(client.get(f"{path}/inputs").json()["snapshots"]) == 3

        items_endpoint = f"{path}/snapshots/{merged['id']}/items"
        first = client.get(items_endpoint, params={"limit": 1}).json()
        second = client.get(items_endpoint, params={"limit": 1, "cursor": first["next_cursor"]}).json()
        items = first["items"] + second["items"]
        assert len({item["id"] for item in items}) == 2
        assert [(item["text"], item["mode"], item["font_size"], item["color_rgb"]) for item in items] == [
            ("合成弹幕", 5, 25, 0x12AB34),
        ] * 2
        assert second["next_cursor"] is None
        assert client.get(f"{path}/snapshots/{source_ids[0]}/items",
                          params={"cursor": first["next_cursor"]}).status_code == 400

        selected = client.put(f"{path}/workflow/snapshot", headers=headers, json={"snapshot_id": merged["id"]})
        assert selected.json()["snapshot_id"] == merged["id"]
        uploaded = client.post(f"{path}/subtitles", headers=headers | BINARY,
                               params={"format": "srt", "language": "chinese"}, content=SRT.encode())
        assert uploaded.status_code == 201
        subtitle_id = uploaded.json()["subtitle_version_id"]
        assert client.get(f"{path}/inputs").json()["workflow"]["chinese_subtitle_id"] == subtitle_id
        analysis = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "analysis"}, json={
            "kind": "classify", "snapshot_id": merged["id"], "subtitle_version_id": subtitle_id,
            "llm_config_version": 0,
        }).json()["id"]
        wait_task(client, analysis, "succeeded")
        assert client.get(f"{path}/runs/{analysis}").json()["name"] == "合并01+02分析"
        assert client.get(path).json()["activity"] == {"stage": "review", "running": False, "active": False}
        preview = client.get(f"{path}/send-preview", params={"run_id": analysis}).json()
        assert preview["duplicate_count"] == 0 and preview["unstable_count"] == 0
        assert [part["bvid"] for part in preview["source_parts"]] == [PROJECT["source_bvid"], second_bvid]
        assert [part["duration_ms"] for part in preview["source_parts"]] == [12_000, 12_000 + difference]
        ready = {"run_id": analysis, "revision": 0}
        endpoint = f"{path}/workflow/review"
        assert client.put(endpoint, headers=headers, json=ready | {"revision": 1}).status_code == 409
        assert client.get(path).json()["activity"]["stage"] == "review"
        assert client.put(endpoint, headers=headers, json=ready).json()["review"] == ready
        assert client.get(path).json()["activity"] == {"stage": "send", "running": False, "active": False}

    with open_client(tmp_path) as client:
        headers = connect_session(client)
        inputs = client.get(f"{path}/inputs").json()
        assert inputs["workflow"]["review"] == ready
        card, = client.get(f"{API}/projects").json()["items"]
        assert card["activity"]["stage"] == "send"
        assert inputs["snapshots"][0]["name"] == merged["name"]
        rows = client.get(f"{path}/review", params={"run_id": analysis}).json()["items"]
        item = rows[0]
        changed = client.patch(f"{path}/review", headers=headers, json={
            "run_id": analysis, "expected_revision": 0,
            "item_ids": [item["item_id"]], "selected": not item["selected"],
        })
        assert changed.json()["revision"] == 1
        assert client.get(path).json()["activity"]["stage"] == "review"
        assert client.put(endpoint, headers=headers, json=ready | {"revision": 1}).status_code == 200
        assert client.get(path).json()["activity"]["stage"] == "send"
        replacement = client.post(f"{path}/subtitles", headers=headers | BINARY,
                                  params={"format": "srt", "language": "chinese"}, content=SRT.encode())
        assert replacement.status_code == 201
        reset = client.get(f"{path}/inputs").json()["workflow"]
        assert reset["snapshot_id"] == merged["id"] and reset["review"] is None and reset["analysis_id"] is None
        assert client.get(path).json()["activity"]["stage"] == "review"
        switched = client.put(f"{path}/workflow/snapshot", headers=headers, json={"snapshot_id": source_ids[0]}).json()
        assert switched["analysis_id"] is None and switched["review"] is None
        assert switched["chinese_subtitle_id"] == subtitle_id
        cleared = client.delete(f"{path}/workflow/subtitles/chinese", headers=headers).json()
        assert cleared["chinese_subtitle_id"] is None
        assert subtitle_id in client.get(path).json()["subtitle_version_ids"]
        assert client.get(f"{path}/runs/{analysis}").status_code == 200


def test_live_checkpoint_restart_and_whitelist(tmp_path):
    blocked = Event()
    source = MockLiveSource(blocked=blocked)
    key = {"Idempotency-Key": "synthetic-download"}
    mode = {"kind": "download", "mode": "bilibili"}
    client = open_client(tmp_path, live_source=source)
    with client:
        headers = connect_session(client)
        project = create_project(client, headers, source_page=2)
        path = f"{API}/projects/{project}"
        created = client.post(f"{path}/tasks", headers=headers | key, json=mode)
        assert created.status_code == 202
        task = created.json()["id"]
        assert blocked.wait(5)
        snapshot = client.get(f"{path}/inputs").json()["snapshots"][0]
        assert snapshot["state"] == "draft"
        assert (snapshot["item_count"], snapshot["completed_segments"], snapshot["covered_until_ms"]) == (
            1, 1, 360_000,
        )
        assert client.post(f"{path}/tasks", headers=headers | key, json=mode).json()["id"] == task
        assert client.post(f"{path}/tasks", headers=headers | key, json={"kind": "download"}).status_code == 409

    resumed = MockLiveSource(after_restart=True)
    client = open_client(tmp_path, live_source=resumed)
    with client:
        headers = connect_session(client)
        paused = wait_task(client, task, "paused")
        assert client.post(f"{API}/tasks/{task}/resume", headers=headers,
                           json={"expected_version": paused["version"]}).status_code == 202
        succeeded = wait_task(client, task, "succeeded")
        assert succeeded["progress"]["done"] == succeeded["progress"]["total"] == 3
        snapshot = client.get(f"{path}/inputs").json()["snapshots"][0]
        assert (snapshot["id"], snapshot["state"], snapshot["covered_until_ms"]) == (task, "complete", 400_000)
        assert client.get(path).json()["snapshot_ids"] == [task]

    assert source.requested == [("123456", 0), ("123456", 1)]
    assert resumed.requested == [("123456", 1)]
    with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
        rows = conn.execute(
            "SELECT raw, normalized FROM source_items WHERE source_key != ? ORDER BY time_ms",
            ("notice:repost",),
        ).fetchall()
        raw, normalized = map(json.loads, rows[0])
        assert set(raw) == {"platform_id", "text", "time_ms", "color", "font_size", "mode"}
        assert normalized["color_rgb"] == 0xAABBCC
        assert [json.loads(row[1])["diagnostic"] for row in rows] == [
            None, "unsupported_color", "outside_video",
        ]


def test_resumed_bilibili_download_does_not_replace_newer_snapshot(tmp_path):
    blocked = Event()
    mode = {"kind": "download", "mode": "bilibili"}
    client = open_client(tmp_path, live_source=MockLiveSource(blocked=blocked))
    with client:
        headers = connect_session(client)
        project = create_project(client, headers, source_page=2)
        path = f"{API}/projects/{project}"
        old = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "old"}, json=mode).json()["id"]
        assert blocked.wait(5)

    client = open_client(tmp_path, live_source=MockLiveSource())
    with client:
        headers = connect_session(client)
        paused = wait_task(client, old, "paused")
        latest = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "new"}, json=mode).json()["id"]
        wait_task(client, latest, "succeeded")
        assert client.get(path).json()["snapshot_ids"] == [latest, old]
        assert client.post(f"{API}/tasks/{old}/resume", headers=headers,
                           json={"expected_version": paused["version"]}).status_code == 202
        assert wait_task(client, old, "succeeded")["progress"]["done"] == 3
        assert client.get(path).json()["snapshot_ids"] == [latest, old]

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as conn:
        assert set(conn.execute("SELECT id FROM source_snapshots")) == {(latest,), (old,)}


def test_existing_schema_migrates_without_losing_projects(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    with database.engine.begin() as connection:
        for table in (ProjectRow.__table__, TaskRow.__table__, TaskEventRow.__table__):
            table.create(connection)
        connection.execute(text("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)"))
        connection.execute(text("INSERT INTO schema_version VALUES (1, 1)"))
        connection.execute(ProjectRow.__table__.insert(), {
            **PROJECT, "id": "old-project", "owner_id": "old-owner",
        })
    database.initialize()
    database.initialize()
    with sqlite3.connect(tmp_path / "workbench.sqlite3.v1.bak") as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (1,)
        assert backup.execute("SELECT name FROM projects").fetchone() == (PROJECT["name"],)
    with database() as repository:
        assert repository.project("old-owner", "old-project").name == PROJECT["name"]
        assert repository.snapshots("old-project") == []
        assert repository.subtitles("old-project") == []
    database.close()

def test_bilibili_sdk_contract_and_adapter_whitelist():
    calls = []
    sdk_video = SimpleNamespace()

    async def get_info():
        return {"title": "合成视频", "pages": [
            {"page": 2, "cid": 9007199254740993, "duration": 120, "part": "合成分 P",
             "owner": "synthetic-never-stored"},
        ]}

    async def get_download_url(**arguments):
        calls.append(arguments)
        return {"timelength": 120_375}

    async def get_danmakus(**arguments):
        calls.append(arguments)
        return [SimpleNamespace(id_=-1, id_str="", text="合成弹幕", dm_time=1.251, color="ab12",
                                mode=1, font_size=25, uid=100, send_time=200, crc32_id="synthetic")]

    sdk_video.get_info = get_info
    sdk_video.get_download_url = get_download_url
    sdk_video.get_danmakus = get_danmakus

    class TestSource(BilibiliSource):
        @asynccontextmanager
        async def _video(self, bvid):
            assert bvid == PROJECT["source_bvid"]
            yield sdk_video

    async def run():
        source = TestSource()
        part, = await source.parts(PROJECT["source_bvid"])
        assert part.cid == "9007199254740993"
        assert part.duration_ms == 120_375
        batch = await source.fetch(part, 0, 1)
        assert batch.items[0].model_dump() == {
            "platform_id": None, "text": "合成弹幕", "time_ms": 1251, "color": 0xAB12,
            "mode": 1, "font_size": 25,
        }

    asyncio.run(run())
    assert calls == [{"cid": 9007199254740993}, {"cid": 9007199254740993, "from_seg": 0, "to_seg": 0}]


def test_cid_download_uses_sdk_client_and_persists_snapshot(tmp_path, monkeypatch):
    network = pytest.importorskip("bilibili_api.utils.network")
    cid = "9007199254740993"
    calls = []

    async def request(*, url, params, **kwargs):
        calls.append((url.rsplit("/", 1)[-1], params))
        code = 200
        if url.endswith("/view"):
            raw = b"\x22\x02\x10\x04"
        elif params["segment_index"] == 1:
            # One entry with ID 42, 1250 ms, 25 px, RGB 0x12, text "Synthetic".
            raw = b"\x0a\x16\x08\x2a\x10\xe2\x09\x18\x01\x20\x19\x28\x12\x3a\x09Synthetic"
        elif params["segment_index"] == 2:
            code, raw = 304, b""
            assert not {"if-modified-since", "if-none-match"}.intersection(
                name.lower() for name in kwargs["headers"]
            )
        elif params["segment_index"] == 3:
            raw = b"\x0a\x10\x08\x2b\x10\xa0\xf9\x2b\x18\x01\x20\x19\x28\x12\x3a\x02Hi"
        else:
            raw = b""
        return SimpleNamespace(code=code, raw=raw, headers={})

    monkeypatch.setattr(network, "get_client", lambda: SimpleNamespace(request=request))
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        target = {"target_bvid": PROJECT["target_bvid"], "target_page": 1}
        response = client.post(f"{API}/projects?source=mock", headers=headers, json=target)
        assert response.status_code == 201
        path = f"{API}/projects/{response.json()['id']}"
        task_headers = headers | {"Idempotency-Key": "same-cid"}
        command = {"kind": "download", "mode": "bilibili", "source_cid": cid}
        for invalid in ({"source_cid": "0"}, {"source_bvid": PROJECT["source_bvid"], "source_page": 1}):
            assert client.post(f"{path}/tasks", headers=task_headers, json=command | invalid).status_code == 422
        submitted = client.post(f"{path}/tasks", headers=task_headers, json=command)
        assert submitted.status_code == 202
        task_id = submitted.json()["id"]
        wait_task(client, task_id, "succeeded")
        assert client.post(f"{path}/tasks", headers=task_headers, json=command).json()["id"] == task_id
        snapshot, = client.get(f"{path}/inputs").json()["snapshots"]
        assert snapshot["part"] == {"cid": cid, "duration_ms": None}
        assert snapshot["name"] == f"01 CID {cid}"
        assert (snapshot["completed_segments"], snapshot["covered_until_ms"], snapshot["item_count"]) == (4, 1_440_000, 2)
        assert client.get(f"{path}/snapshots/{task_id}/items").json()["items"] == [
            {"id": "id:42", "time_ms": 1250, "text": "Synthetic", "font_size": 25, "color_rgb": 0x12, "mode": 1},
            {"id": "id:43", "time_ms": 720032, "text": "Hi", "font_size": 25, "color_rgb": 0x12, "mode": 1},
        ]

    assert calls == [
        ("view", {"type": 1, "oid": cid}),
        ("seg.so", {"type": 1, "oid": cid, "segment_index": 1}),
        ("seg.so", {"type": 1, "oid": cid, "segment_index": 2}),
        ("seg.so", {"type": 1, "oid": cid, "segment_index": 3}),
        ("seg.so", {"type": 1, "oid": cid, "segment_index": 4}),
    ]


@pytest.mark.parametrize(("code", "error", "retryable"), [
    (429, "bilibili_rate_limited", True),
    (412, "bilibili_access_denied", False),
    (-101, "bilibili_access_denied", False),
    (-404, "bilibili_video_unavailable", False),
    (None, "bilibili_unavailable", True),
])
def test_bilibili_error_classes(code, error, retryable):
    result = platform_error(code)
    assert (result.info.code, result.info.retryable) == (error, retryable)

def test_default_mock_download_builds_complete_snapshot(tmp_path):
    client = open_client(tmp_path)
    with client:
        headers = connect_session(client)
        project = create_project(client, headers)
        path = f"{API}/projects/{project}"
        response = client.post(f"{path}/tasks", headers=headers | {"Idempotency-Key": "synthetic-key"},
                               json={"kind": "download"})
        assert response.status_code == 202
        task = response.json()
        events = client.get(f"{API}/tasks/{task['id']}/events")
        assert events.status_code == 200 and '"state":"succeeded"' in events.text
        snapshot, = client.get(f"{path}/inputs").json()["snapshots"]
        assert snapshot["range_kind"] == "synthetic" and snapshot["state"] == "complete"
        assert (snapshot["segment_count"], snapshot["item_count"], snapshot["covered_until_ms"]) == (
            10, 50, 3_600_000,
        )
