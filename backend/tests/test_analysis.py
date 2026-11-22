import asyncio
import json
from pathlib import Path
import sqlite3
from threading import Event

import httpx
import pytest
from pydantic import SecretStr

from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.adapters.llm import ChatCompletions, SyntheticModel
from danmaku.application.classification.model import ModelFailure
from danmaku.application.sessions import LocalSession, LocalSessions
from danmaku.application.tasks.service import BatchError
from danmaku.domain.analysis import AnalysisOutput, ModelConfig, PROMPT_VERSION
from danmaku.domain.inputs import SourceBatch, SourceItem, VideoPart
from danmaku.domain.workbench import Principal
from test_workbench import API, ORIGIN, create_project, download, event_frames, open_client, connect_session, wait_task

ENDPOINT = "https://model.example.invalid/v1"
SYNTHETIC_KEY = "synthetic-test-key"

# Labels refer to the primary purpose of each human-written synthetic example.
LABELED = [
    ("你已经知道了？", "translation"),
    ("原来你早就晓得啦？", "translation"),
    ("“你已经知道了？”这句配音真自然", "other"),
    ("她是问你早就知道了吗，附带吐槽一下这个停顿", "translation"),
    ("“你已经知道了？”她这样反问是因为心虚吧", "other"),
    ("早就知道了啊", "other"),
    ("[合成标记]普通测试条目", "other"),
]


def source(text, time_ms, index, *, mode=1):
    return SourceItem(platform_id=f"synthetic-{index:04}", text=text, time_ms=time_ms,
                      color=0xFFFFFF, font_size=25, mode=mode)


def app_with_inputs(path, items, duration_ms=12_000, model=None, **options):
    async def execute(record):
        part = VideoPart(bvid=record.source_bvid, page=1, cid="42", duration_ms=duration_ms, title="合成分 P")
        return SourceBatch(part=part, segment_count=1, segment=0, items=items)

    if model is not None:
        options["model_providers"] = {"mock": model, "openai_compatible": model}
    options.setdefault("credential_store", MemoryCredentialStore())
    return open_client(path, execute_batch=execute, **options)


def timestamp(seconds):
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02},000"


def upload_subtitle(client, project, headers, rows, filename, *, format="srt"):
    srt = "\n\n".join(
        f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{value}"
        for index, (start, end, value) in enumerate(rows, start=1)
    )
    response = client.post(
        f"{API}/projects/{project}/subtitles", params={"format": format, "filename": filename},
        content=srt.encode(), headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201 and response.json()["filename"] == filename
    return response.json()["subtitle_version_id"]


def prepare(client, headers, cues=None):
    project = create_project(client, headers)
    snapshot = download(client, project, headers).json()["id"]
    wait_task(client, snapshot, "succeeded")
    if cues is None:
        cues = [(0, 3, "你已经知道了？"), (3, 5, "また後でね。")]
    chinese = upload_subtitle(client, project, headers, cues, "chinese.srt")
    japanese = upload_subtitle(client, project, headers,
                               [(0, 3, "もう知っていたの？"), (3, 5, "また後でね。")], "japanese.srt")
    return project, {"kind": "classify", "snapshot_id": snapshot, "subtitle_version_id": chinese,
                     "japanese_subtitle_version_id": japanese, "llm_config_version": 0}


def test_downloads_and_analyses_overlap_across_projects(tmp_path):
    first_download_started = Event()
    first_analysis_started = Event()
    release_download = Event()
    release_analysis = Event()

    async def wait_until_released(event):
        while not event.is_set():
            await asyncio.sleep(0.01)

    async def execute(record):
        if record.idempotency_key == "slow-download":
            first_download_started.set()
            await wait_until_released(release_download)
        part = VideoPart(bvid=record.source_bvid, page=1, cid="42", duration_ms=12_000, title="合成分 P")
        return SourceBatch(part=part, segment_count=1, segment=0, items=[source("合成条目", 1000, 1)])

    class ParallelModel(SyntheticModel):
        async def generate(self, config, secret, operation, instructions, data):
            if not first_analysis_started.is_set():
                first_analysis_started.set()
                await wait_until_released(release_analysis)
            return await super().generate(config, secret, operation, instructions, data)

    with open_client(tmp_path, execute_batch=execute, model_providers={"mock": ParallelModel()}) as client:
        headers = connect_session(client)
        project_a = create_project(client, headers)
        slow_download = download(client, project_a, headers, key="slow-download").json()["id"]
        assert first_download_started.wait(5)

        project_b, inputs_b = prepare(client, headers)
        slow_analysis = classify(client, project_b, inputs_b, headers).json()["id"]
        assert first_analysis_started.wait(5)
        assert client.get(f"{API}/tasks/{slow_download}").json()["state"] == "running"
        activities = {entry["id"]: entry["activity"] for entry in client.get(f"{API}/projects").json()["items"]}
        assert activities == {
            project_a: {"stage": "download", "running": True, "active": True},
            project_b: {"stage": "classify", "running": True, "active": True},
        }
        conflict = classify(client, project_b, inputs_b, headers, key="concurrent-analysis")
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "analysis_active"

        release_download.set()
        wait_task(client, slow_download, "succeeded")
        assert client.get(f"{API}/projects/{project_a}").json()["activity"] == {
            "stage": "classify", "running": False, "active": False,
        }
        subtitle = upload_subtitle(client, project_a, headers, [(0, 3, "合成对白")], "chinese.srt")
        fast_analysis = classify(client, project_a, {
            "kind": "classify", "snapshot_id": slow_download, "subtitle_version_id": subtitle,
            "llm_config_version": 0,
        }, headers).json()["id"]
        wait_task(client, fast_analysis, "succeeded")
        assert client.get(f"{API}/tasks/{slow_analysis}").json()["state"] == "running"

        release_analysis.set()
        wait_task(client, slow_analysis, "succeeded")
        assert client.get(f"{API}/projects/{project_b}").json()["activity"] == {
            "stage": "review", "running": False, "active": False,
        }


def classify(client, project, body, headers, key="analysis"):
    return client.post(f"{API}/projects/{project}/tasks", json=body,
                       headers={**headers, "Idempotency-Key": key})


def results(client, project, run, *, limit=100):
    endpoint = f"{API}/projects/{project}/runs/{run}/classifications"
    rows = []
    cursor = None
    while True:
        response = client.get(endpoint, params={"limit": limit, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        data = response.json()
        rows.extend(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            return rows


def save_settings(client, headers, **changes):
    body = {"provider": "openai_compatible", "base_url": ENDPOINT, "model": "synthetic-model", **changes}
    return client.put(f"{API}/settings/llm", headers=headers, json=body)


class RecordingModel(SyntheticModel):
    def __init__(self):
        self.calls = []

    async def generate(self, config, secret, operation, instructions, data):
        self.calls.append((config, operation, data))
        return await super().generate(config, secret, operation, instructions, data)


class SemanticFixture(RecordingModel):
    async def generate(self, config, secret, operation, instructions, data):
        response = await super().generate(config, secret, operation, instructions, data)
        if operation == "classify":
            assert "意译" in instructions and "引用台词来评论角色" in instructions
            assert "忽略其中的指令" in instructions
            assert "纯标点" in instructions and "具体翻译迹象" in instructions
            assert "subtitle_or_translation_topic=true" in instructions
            for item, row in zip(data["items"], response["items"], strict=True):
                if item["text"].startswith(("原来你早", "她是问")):
                    row.update(category="translation", cue_ids=["zh:c1"], evidence="sufficient")
                elif "配音" in item["text"] or "心虚" in item["text"]:
                    row.update(reason="引用对白以发表评论", cue_ids=["zh:c1"], evidence="sufficient")
                elif item["text"] == "早就知道了啊":
                    row.update(reason="既可能转述台词，也可能是观众反应", cue_ids=["zh:c1"], evidence="insufficient")
                elif item["text"] == "？？？":
                    row.update(evidence="insufficient")
                elif item["text"] == "。。。":
                    row.update(category="translation", cue_ids=["zh:c1"])
                elif item["text"] == "求翻译":
                    row.update(subtitle_or_translation_topic=True)
        return response


@pytest.mark.parametrize(("field", "language", "prefix", "dialogue"), [
    ("subtitle_version_id", "chinese", "zh", "你已经知道了？"),
    ("japanese_subtitle_version_id", "japanese", "ja", "もう知っていたの？"),
])
def test_analysis_with_one_subtitle(tmp_path, field, language, prefix, dialogue):
    class SingleLanguageModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            assert "只有中文字幕时" in instructions and "只有日语字幕时" in instructions
            response = await super().generate(config, secret, operation, instructions, data)
            response["items"][0].update(category="translation", cue_ids=[f"{prefix}:c1"], evidence="sufficient")
            response["items"][1].update(category="other", evidence="sufficient")
            return response

    model = SingleLanguageModel()
    items = [source("你已经知道了？", 1000, 1), source("这个配音真自然", 1200, 2)]
    with app_with_inputs(tmp_path, items, model=model) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        snapshot = download(client, project, headers).json()["id"]
        wait_task(client, snapshot, "succeeded")
        body = {"kind": "classify", "snapshot_id": snapshot, "llm_config_version": 0}
        rejected = classify(client, project, body, headers)
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "subtitle_required"

        version = upload_subtitle(client, project, headers, [(0, 3, dialogue)], f"{language}.srt")
        response = classify(client, project, {**body, field: version}, headers)
        assert response.status_code == 202
        run_id = response.json()["id"]
        unused_field = "japanese_subtitle_version_id" if language == "chinese" else "subtitle_version_id"
        repeated = classify(client, project, {**body, field: version, unused_field: None}, headers)
        assert repeated.status_code == 202 and repeated.json()["id"] == run_id
        wait_task(client, run_id, "succeeded")

        assert len(model.calls) == 1
        _, _, data = model.calls[0]
        assert data[f"{language}_subtitles"] == [
            {"id": f"{prefix}:c1", "start_ms": 0, "end_ms": 3000, "text": dialogue},
        ]
        unused_language = "japanese" if language == "chinese" else "chinese"
        assert data[f"{unused_language}_subtitles"] == []
        endpoint = f"{API}/projects/{project}"
        run = client.get(f"{endpoint}/runs/{run_id}").json()
        assert run[field] == version and run[unused_field] is None
        assert run["strategy"] == "single_subtitle" and run["prompt_version"] == PROMPT_VERSION
        review = client.get(f"{endpoint}/review", params={"run_id": run_id}).json()
        assert [cue["id"] for cue in review["items"][0]["evidence_cues"]] == [f"{prefix}:c1"]
        preview = client.get(f"{endpoint}/send-preview", params={"run_id": run_id}).json()
        assert [cue["plaintext"] for cue in preview["cues"]] == [dialogue]
        assert client.delete(f"{endpoint}/subtitles/{version}", headers=headers).status_code == 409


def test_bilingual_classification_semantics_and_model_version(tmp_path):
    fixture = SemanticFixture()
    labeled = [*LABELED, ("？？？", "other"), ("。。。", "other"), ("求翻译", "other")]
    items = [source(text, 1000, index) for index, (text, _) in enumerate(labeled)]
    client = app_with_inputs(tmp_path, items, model=fixture)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        response = classify(client, project, body, headers)
        assert response.status_code == 202
        first = response.json()["checkpoint"]["run_id"]
        assert response.headers["Location"] == f"{API}/tasks/{first}"
        assert classify(client, project, body, headers).json()["id"] == first
        assert classify(client, project, {**body, "analyze_harm": True}, headers).status_code == 422
        changed = {**body, "japanese_subtitle_version_id": body["subtitle_version_id"]}
        assert classify(client, project, changed, headers).status_code == 409
        assert classify(client, project, changed, headers, key="same-subtitle").status_code == 422
        final = wait_task(client, first, "succeeded")
        assert final["progress"]["done"] == len(items)
        assert len(fixture.calls) == 1
        _, operation, data = fixture.calls[0]
        assert operation == "classify" and set(data) == {"japanese_subtitles", "chinese_subtitles", "items"}
        assert [cue["id"] for cue in data["japanese_subtitles"]] == ["ja:c1", "ja:c2"]
        assert data["japanese_subtitles"][0]["text"] == "もう知っていたの？"
        assert [cue["id"] for cue in data["chinese_subtitles"]] == ["zh:c1", "zh:c2"]
        assert data["chinese_subtitles"][0]["text"] == "你已经知道了？"
        assert {row["id"] for row in data["items"]} == {f"d{i}" for i in range(1, len(items) + 1)}
        original = results(client, project, first, limit=3)
        assert [row["category"] for row in original] == [label for _, label in labeled]
        assert original[5]["diagnostic"] == "needs_review"
        assert all("harm_assessment" not in row for row in original)
        review = client.get(f"{API}/projects/{project}/review", params={"run_id": first}).json()
        by_text = {row["text"]: row for row in review["items"]}
        for text in ("？？？", "。。。", "[合成标记]普通测试条目"):
            row = by_text[text]
            assert row["classification"]["category"] == "other"
            assert row["classification"]["diagnostic"] is None and row["selected"]
        assert not by_text["早就知道了啊"]["selected"]
        request = by_text["求翻译"]
        assert request["classification"]["diagnostic"] == "subtitle_or_translation_topic" and not request["selected"]

        config = save_settings(client, headers, provider="mock", base_url=None, model="synthetic-alternate").json()
        second = classify(client, project, {**body, "llm_config_version": config["version"]},
                          headers, key="second").json()["id"]
        wait_task(client, second, "succeeded")
        assert second != first
        runs = {run["id"]: run for run in client.get(f"{API}/projects/{project}/runs").json()["items"]}
        assert runs[first]["config"]["version"] == 0 and runs[first]["strategy"] == "bilingual"
        assert runs[second]["config"]["model"] == "synthetic-alternate"
        assert runs[second]["japanese_subtitle_version_id"] == body["japanese_subtitle_version_id"]
        assert [operation for _, operation, _ in fixture.calls] == ["classify", "classify"]

        assert results(client, project, first) == original
        detail = client.get(f"{API}/projects/{project}").json()
        assert set(detail["run_ids"]) == {first, second}
        tasks = client.get(f"{API}/projects/{project}/tasks?kind=classify").json()["items"]
        assert {task["id"] for task in tasks} == {first, second}
        assert event_frames(client.get(f"{API}/tasks/{first}/events"))[-1][2] == final


def test_fixed_subtitle_track_checks_model_disagreement(tmp_path):
    class TrackModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            await super().generate(config, secret, operation, instructions, data)
            assert "多个译稿" in instructions and "subtitle_track_cue_id" in instructions
            return {"items": [
                {"id": item["id"], "category": "translation", "evidence": "sufficient", "cue_ids": ["zh:c3"]}
                if item["text"] == "我会留下照看仪器" else
                {"id": item["id"], "category": "other", "evidence": "sufficient"}
                for item in data["items"]
            ]}

    cues = [
        (1, 2, "明天把仪器带过来。"),
        (4, 5, "（检查通道安全吗？）"),
        (7, 8, "我会留下照看仪器"),
        (70, 72, "记得关好控制室的门。"),
    ]
    items = [
        source("明天把仪器带过来", 950, 0, mode=4),
        source("(检查通道安全吗)", 4100, 1, mode=4),
        source("我会留下照看仪器", 7300, 2, mode=4),
        source("我会留下照看仪器！", 7400, 3),
        source("（检查通道安全吗？）这句配音很自然", 4200, 4, mode=4),
        *(source("记得关好控制室的门", 70100 + i, 5 + i, mode=4) for i in range(3)),
    ]
    model = TrackModel()
    with app_with_inputs(tmp_path, items, duration_ms=80_000, model=model) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers, cues)
        run = classify(client, project, body, headers).json()["id"]
        wait_task(client, run, "succeeded")
        [(_, _, data)] = model.calls
        assert [item["subtitle_track_cue_id"] for item in data["items"] if "subtitle_track_cue_id" in item] == [
            "zh:c1", "zh:c2", "zh:c3",
        ]
        review = client.get(f"{API}/projects/{project}/review", params={"run_id": run}).json()["items"]
        rows = {row["item_id"]: row for row in review}
        for index, cue_id in enumerate(("zh:c1", "zh:c2")):
            row = rows[f"id:synthetic-{index:04}"]
            assert row["classification"]["diagnostic"] == "needs_review" and not row["selected"]
            assert [cue["id"] for cue in row["evidence_cues"]] == [cue_id]
        explicit = rows["id:synthetic-0002"]
        assert explicit["classification"]["category"] == "translation"
        assert explicit["classification"]["diagnostic"] is None and not explicit["selected"]
        for index in range(3, len(items)):
            row = rows[f"id:synthetic-{index:04}"]
            assert row["classification"]["diagnostic"] is None and row["selected"]


@pytest.mark.parametrize("format, srt_content, expected_cues", [
    ("ass", False, ["c1", "c3", "c4", "c5"]),
    ("txt", False, ["c1", "c3", "c4", "c5"]),
    ("srt", True, ["c1", "c2", "c3", "c4", "c5"]),
    ("txt", True, ["c1", "c2", "c3", "c4", "c5"]),
])
def test_only_chinese_ass_dialogue_is_deduplicated_for_model(tmp_path, format, srt_content, expected_cues):
    ass = r"""[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Main,,0,0,0,,合成台词
Dialogue: 1,0:00:01.00,0:00:03.00,Alt,,0,0,0,,{\i1}合成台词{\i0}
Dialogue: 0,0:00:01.00,0:00:03.00,Main,,0,0,0,,另一行台词
Dialogue: 0,0:00:01.00,0:00:04.00,Main,,0,0,0,,合成台词
Dialogue: 0,0:00:02.00,0:00:04.00,Main,,0,0,0,,合成台词
"""
    model = RecordingModel()
    with app_with_inputs(tmp_path, [source("合成台词", 1000, 0)], model=model) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        snapshot = download(client, project, headers).json()["id"]
        wait_task(client, snapshot, "succeeded")

        def upload_ass(content, format, filename):
            response = client.post(
                f"{API}/projects/{project}/subtitles", params={"format": format, "filename": filename},
                content=content.encode(), headers={**headers, "Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 201 and response.json()["cue_count"] == 5
            return response.json()["subtitle_version_id"]

        japanese = upload_ass(ass.replace("合成台词", "架空の台詞"), "ass", "japanese.ass")
        if srt_content:
            chinese = upload_subtitle(client, project, headers, [
                (1, 3, "合成台词"), (1, 3, "合成台词"), (1, 3, "另一行台词"),
                (1, 4, "合成台词"), (2, 4, "合成台词"),
            ], f"chinese.{format}", format=format)
        else:
            chinese = upload_ass(ass, format, f"chinese.{format}")
        body = {"kind": "classify", "snapshot_id": snapshot, "subtitle_version_id": chinese,
                "japanese_subtitle_version_id": japanese, "llm_config_version": 0}
        task = classify(client, project, body, headers).json()["id"]
        wait_task(client, task, "succeeded")
        [(_, _, data)] = model.calls
        assert [cue["id"] for cue in data["japanese_subtitles"]] == [f"ja:c{i}" for i in range(1, 6)]
        assert [cue["id"] for cue in data["chinese_subtitles"]] == [f"zh:{cue}" for cue in expected_cues]
        assert results(client, project, task)[0]["cue_ids"] == ["zh:c1"]


def test_offline_model_against_human_labeled_samples(tmp_path):
    items = [source(text, 1000, index) for index, (text, _) in enumerate(LABELED)]
    client = app_with_inputs(tmp_path, items)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        wait_task(client, task, "succeeded")
        rows = results(client, project, task)
        decisions = [row["category"] for row in rows]
        comparison = list(zip(decisions, (label for _, label in LABELED), strict=True))
        wrongly_excluded = sum(predicted == "translation" and label == "other" for predicted, label in comparison)
        wrongly_retained = sum(predicted == "other" and label == "translation" for predicted, label in comparison)
        assert (wrongly_excluded, wrongly_retained) == (0, 2)


def test_long_video_is_submitted_whole_in_one_call(tmp_path):
    mock = RecordingModel()
    duration = 8 * 3600
    cues = sorted([(step, step + 12, f"合成对白 {step}") for step in range(0, duration, 330)]
                  + [(58, 62, "跨越时间窗的台词")])
    items = [source(f"合成弹幕 {index}", 59_000 if index < 40 else
                    60_000 if index < 80 else min((index - 79) * 700, duration - 1) * 1000,
                    index) for index in range(345)]
    client = app_with_inputs(tmp_path, items, duration * 1000, mock)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers, cues)
        task = classify(client, project, body, headers).json()["id"]
        final = wait_task(client, task, "succeeded")
        assert final["progress"]["done"] == final["progress"]["total"] == len(items)
        [(_, operation, data)] = mock.calls
        assert operation == "classify"
        assert [cue["text"] for cue in data["chinese_subtitles"]] == [cue[2] for cue in cues]
        assert len(data["japanese_subtitles"]) == 2
        assert [item["text"] for item in data["items"]] == [item.text for item in items]
        assert {row["id"] for row in results(client, project, task, limit=31)} == {
            f"id:{item.platform_id}" for item in items
        }


def test_context_limit_does_not_split_retry_or_save_partial_results(tmp_path):
    class LimitedModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            await super().generate(config, secret, operation, instructions, data)
            raise ModelFailure("llm_context_limit")

    mock = LimitedModel()
    items = [source("你已经知道了？", 1000, 0), source("合成超限", 1000, 1)]
    client = app_with_inputs(tmp_path, items, model=mock)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        final = wait_task(client, task, "failed")
        assert final["error"]["code"] == "llm_context_limit"
        assert final["progress"]["done"] == 0
        assert len(mock.calls) == 1 and results(client, project, task) == []


def test_classification_accepts_echoed_json_object_type(tmp_path):
    class EchoModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            response = await super().generate(config, secret, operation, instructions, data)
            assert "JSON 顶层只能包含 items" in instructions
            return {"type": "json_object", **response}

    mock = EchoModel()
    client = app_with_inputs(tmp_path, [source("合成弹幕", 1000, 0)], model=mock)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        wait_task(client, task, "succeeded")
        assert len(mock.calls) == len(results(client, project, task)) == 1


@pytest.mark.parametrize("malformation", ["missing", "duplicate", "extra", "category", "cue", "type", "top_level"])
def test_malformed_response_preserves_atomic_results(tmp_path, malformation):
    class BrokenModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            response = await super().generate(config, secret, operation, instructions, data)
            if operation != "classify" or data["items"][0]["time_ms"] != 1000:
                return response
            rows = response["items"]
            if malformation == "missing":
                rows.pop()
            elif malformation in {"duplicate", "extra"}:
                rows.append({**rows[0], "id": rows[0]["id"] if malformation == "duplicate" else "invented"})
            elif malformation == "category":
                rows[0]["category"] = "invalid"
            elif malformation == "type":
                response["type"] = "unexpected"
            elif malformation == "top_level":
                response.update(type="json_object", unexpected=True)
            else:
                rows[0]["cue_ids"] = ["invented"]
                response["type"] = "json_object"
            return response

    mock = BrokenModel()
    items = [source("合成弹幕", 1000, 0), source("合成弹幕", 61_000, 1)]
    client = app_with_inputs(tmp_path, items, 65_000, mock)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        final = wait_task(client, task, "failed")
        assert final["error"]["code"] == "llm_invalid_response"
        assert final["progress"]["done"] == 0
        assert len(mock.calls) == 1 and results(client, project, task) == []


@pytest.mark.parametrize("settled", [True, False])
@pytest.mark.parametrize("single_language", [False, True])
def test_pause_and_restart_replay_only_uncommitted_request(tmp_path, settled, single_language):
    entered, release = Event(), Event()
    items = [source(f"合成 {index}", 1000, index) for index in range(21)]

    class BlockingModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            response = await super().generate(config, secret, operation, instructions, data)
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return response

    model = BlockingModel()
    client = app_with_inputs(tmp_path, items, model=model)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        if single_language:
            del body["subtitle_version_id"]
        task = classify(client, project, body, headers).json()["id"]
        assert entered.wait(4)
        assert results(client, project, task) == []
        if settled:
            running = client.get(f"{API}/tasks/{task}").json()
            pause = client.post(f"{API}/tasks/{task}/pause", json={"expected_version": running["version"]}, headers=headers)
            assert pause.json()["state"] == "pausing"
            release.set()
            paused = wait_task(client, task, "paused")
            assert paused["checkpoint"]["completed_batches"] == 1
            assert len(results(client, project, task)) == len(items)

    resumed = RecordingModel()
    client = app_with_inputs(tmp_path, items, model=resumed)
    with client:
        headers = connect_session(client)
        recovered = wait_task(client, task, "paused")
        assert recovered["checkpoint"]["completed_batches"] == int(settled)
        if not settled:
            assert recovered["error"]["code"] == "interrupted"
        client.post(f"{API}/tasks/{task}/resume", json={"expected_version": recovered["version"]}, headers=headers)
        wait_task(client, task, "succeeded")
        assert len(resumed.calls) == (0 if settled else 1)
        if not settled:
            assert resumed.calls[0][2] == model.calls[0][2]
        assert len(results(client, project, task)) == len(items)


def test_credential_binding_and_pause_then_resume_after_rotation(tmp_path):
    class RequiresKey(SyntheticModel):
        async def generate(self, config, secret, operation, instructions, data):
            if secret != SecretStr(SYNTHETIC_KEY):
                raise ModelFailure("llm_credential_rejected")
            assert config.base_url == ENDPOINT
            return await super().generate(config, secret, operation, instructions, data)

    client = app_with_inputs(tmp_path, [source("合成测试", 1000, 0)], model=RequiresKey())
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        assert client.get(f"{API}/settings/llm").json()["provider"] == "mock"
        blank = save_settings(client, headers).json()
        assert not blank["configured"]
        task = classify(client, project, {**body, "llm_config_version": blank["version"]}, headers).json()["id"]
        paused = wait_task(client, task, "paused")
        assert paused["error"]["code"] == "llm_credential_missing"

        rejected_key = "synthetic-rejected-key"
        response = save_settings(client, headers, api_key=rejected_key)
        assert response.status_code == 200 and rejected_key not in response.text
        client.post(f"{API}/tasks/{task}/resume", json={"expected_version": paused["version"]}, headers=headers)
        rejected = wait_task(client, task, "paused")
        assert rejected["error"]["code"] == "llm_credential_rejected"

        valid = save_settings(client, headers, api_key=SYNTHETIC_KEY)
        assert valid.status_code == 200 and SYNTHETIC_KEY not in valid.text
        other = save_settings(client, headers, base_url="https://other.example.invalid/v1").json()
        assert not other["configured"]
        client.post(f"{API}/tasks/{task}/resume", json={"expected_version": rejected["version"]}, headers=headers)
        wait_task(client, task, "succeeded")
        run = client.get(f"{API}/projects/{project}/runs/{task}").json()
        assert run["config"]["version"] == blank["version"]
        for file in Path(tmp_path).glob("workbench.sqlite3*"):
            if file.name.endswith("-shm"):
                continue
            assert SYNTHETIC_KEY.encode() not in file.read_bytes()
            assert rejected_key.encode() not in file.read_bytes()


def test_model_settings_freeze_extra_requirements_and_delete_key(tmp_path):
    instructions_seen = []

    class CapturingModel(SyntheticModel):
        async def generate(self, config, secret, operation, instructions, data):
            instructions_seen.append(instructions)
            assert config.reasoning_effort == "max" and not config.thinking_enabled
            assert secret == SecretStr(SYNTHETIC_KEY)
            return await super().generate(config, secret, operation, instructions, data)

    requirements = "合成要求：留意补充译文。"
    with app_with_inputs(tmp_path, [source("合成测试", 1000, 0)], model=CapturingModel()) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        settings = f"{API}/settings/llm"
        assert client.get(settings).json()["extra_requirements"] == ""
        payload = {
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "thinking_enabled": False, "reasoning_effort": "max", "extra_requirements": requirements,
        }
        saved = save_settings(client, headers, **payload, api_key=SYNTHETIC_KEY)
        assert saved.status_code == 200 and SYNTHETIC_KEY not in saved.text
        assert saved.json()["configured"] and client.get(settings).json() == saved.json()
        changed = save_settings(client, headers, **{**payload, "extra_requirements": ""})
        assert changed.json()["configured"]
        task = classify(client, project, {**body, "llm_config_version": saved.json()["version"]}, headers).json()["id"]
        wait_task(client, task, "succeeded")
        assert len(instructions_seen) == 1 and instructions_seen[0].endswith(requirements)

        assert save_settings(client, headers, **payload, api_key=SYNTHETIC_KEY, remove_api_key=True).status_code == 422
        deleted = save_settings(client, headers, **payload, remove_api_key=True)
        assert deleted.status_code == 200 and not deleted.json()["configured"]
        assert not client.get(settings).json()["configured"]
        task = classify(client, project, {**body, "llm_config_version": saved.json()["version"]},
                        headers, key="without-key").json()["id"]
        assert wait_task(client, task, "paused")["error"]["code"] == "llm_credential_missing"


def test_project_reasoning_effort_freezes_run_config_and_preserves_global_default(tmp_path):
    seen = []

    class CapturingModel(SyntheticModel):
        async def generate(self, config, secret, operation, instructions, data):
            seen.append(config.reasoning_effort)
            return await super().generate(config, secret, operation, instructions, data)

    with app_with_inputs(tmp_path, [source("合成测试", 1000, 0)], model=CapturingModel()) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        other, other_body = prepare(client, headers)
        override = {**body, "reasoning_effort": "max"}
        rejected = classify(client, project, override, headers)
        assert rejected.status_code == 422 and "reasoning_effort_unsupported" in rejected.text
        settings = f"{API}/settings/llm"
        assert not client.get(settings).json()["can_set_reasoning_effort"]
        model = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"}
        disabled = save_settings(client, headers, **model, thinking_enabled=False, api_key=SYNTHETIC_KEY).json()
        assert not disabled["can_set_reasoning_effort"]
        rejected = classify(client, project, {**override, "llm_config_version": disabled["version"]}, headers)
        assert rejected.status_code == 422 and "reasoning_effort_unsupported" in rejected.text
        saved = save_settings(client, headers, **model, reasoning_effort="low").json()
        assert saved["can_set_reasoning_effort"]
        override["llm_config_version"] = saved["version"]
        run = classify(client, project, override, headers).json()["id"]
        wait_task(client, run, "succeeded")
        assert classify(client, project, override, headers).json()["id"] == run
        assert client.get(f"{API}/projects/{project}/runs/{run}").json()["config"]["reasoning_effort"] == "max"
        assert client.get(f"{API}/projects/{project}/inputs").json()["workflow"]["reasoning_effort"] == "max"
        other_run = classify(client, other, {**other_body, "llm_config_version": saved["version"]}, headers).json()["id"]
        wait_task(client, other_run, "succeeded")
        assert client.get(f"{API}/projects/{other}/inputs").json()["workflow"]["reasoning_effort"] is None
        assert client.get(settings).json()["reasoning_effort"] == "low"
        reset = classify(client, project, {**override, "reasoning_effort": None}, headers, key="reset").json()["id"]
        wait_task(client, reset, "succeeded")
        assert client.get(f"{API}/projects/{project}/inputs").json()["workflow"]["reasoning_effort"] is None
        assert seen == ["max", "low", "low"]


def test_analysis_inputs_and_access_are_project_scoped(tmp_path):
    sessions = LocalSessions()
    client = app_with_inputs(tmp_path, [], sessions=sessions)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        different, foreign_input = prepare(client, headers)
        write = {**headers, "Idempotency-Key": "bad"}
        assert classify(client, different, body, headers).status_code == 404
        assert classify(client, project, {**body, "subtitle_version_id": foreign_input["subtitle_version_id"]},
                        headers).status_code == 404
        assert classify(client, project, {**body, "llm_config_version": 200}, headers).status_code == 404
        assert client.post(f"{API}/projects/{project}/tasks", json=body,
                           headers={**write, "X-CSRF-Token": "wrong"}).status_code == 403
        invalid = save_settings(client, headers, base_url="http://outside.example.invalid", api_key=SYNTHETIC_KEY)
        assert invalid.status_code == 422 and SYNTHETIC_KEY not in invalid.text
        assert save_settings(client, {"Origin": ORIGIN}, api_key=SYNTHETIC_KEY).status_code == 403
        assert classify(client, project, {**body, "japanese_subtitle_version_id":
                        foreign_input["japanese_subtitle_version_id"]}, headers).status_code == 404
        task = classify(client, project, body, headers).json()["id"]
        assert wait_task(client, task, "succeeded")["progress"]["done"] == 0
        assert client.get(f"{API}/projects/{different}/runs/{task}").status_code == 404
        assert client.get(f"{API}/projects/{different}/runs/{task}/classifications").status_code == 404
        sessions.sessions["foreign"] = LocalSession(Principal(id="other-test-owner"), "foreign-csrf", float("inf"))
        client.cookies.clear()
        client.cookies.set("danmaku_session", "foreign")
        assert client.get(f"{API}/projects/{project}/runs").status_code == 404
        assert client.get(f"{API}/projects/{project}/runs/{task}/classifications").status_code == 404


def test_sqlite_v2_upgrade_preserves_old_tasks_and_creates_backup(tmp_path):
    app = app_with_inputs(tmp_path, [])
    with app:
        headers = connect_session(app)
        project, body = prepare(app, headers)
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        for table in ("classifications", "analysis_batches", "analysis_runs", "llm_credentials", "llm_configs"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_version SET version = 2")
    app = app_with_inputs(tmp_path, [])
    with app:
        headers = connect_session(app)
        assert app.get(f"{API}/tasks/{body['snapshot_id']}").json()["state"] == "succeeded"
        task = classify(app, project, body, headers).json()["id"]
        wait_task(app, task, "succeeded")
    with sqlite3.connect(tmp_path / "workbench.sqlite3.v2.bak") as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (2,)


@pytest.mark.parametrize("endpoint, model, thinking, effort, expected", [
    (ENDPOINT, "synthetic-model", True, "high", {}),
    (ENDPOINT, "deepseek-flash", True, "high", {}),
    ("https://api.deepseek.com", "deepseek-flash", True, "low",
     {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
    ("https://api.deepseek.com", "deepseek-flash", False, "max", {"thinking": {"type": "disabled"}}),
])
def test_real_adapter_contract_with_simulated_transport(endpoint, model, thinking, effort, expected):
    replies = iter([
        httpx.Response(429, headers={"Retry-After": "17"}, json={"error": {
            "type": "rate_limit_error", "code": 123, "param": "model",
            "message": f"Synthetic limit for {SYNTHETIC_KEY}: 合成弹幕",
        }}),
        httpx.ConnectError("synthetic-private"),
        httpx.Response(401, json={"raw": "synthetic-private"}),
        httpx.Response(403),
        httpx.Response(503),
        httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}),
        httpx.Response(200, json={"choices": [{"finish_reason": "length"}]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "broken JSON"}}]}),
        httpx.Response(302, headers={"Location": "https://other.example.invalid/"}),
        httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": '{"items":[]}'}},
        ]}),
    ])
    requests = []

    def transport(request):
        requests.append(request)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    config = ModelConfig(provider="openai_compatible", base_url=endpoint, model=model, version=1,
                         thinking_enabled=thinking, reasoning_effort=effort)
    secret = SecretStr(SYNTHETIC_KEY)
    data = {"items": [{"text": "合成弹幕"}]}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            provider = ChatCompletions(client)
            for code in [
                "llm_rate_limited", "llm_network", "llm_credential_rejected", "llm_credential_rejected",
                "llm_unavailable", "llm_context_limit", "llm_context_limit", "llm_invalid_response",
                "llm_request_rejected",
            ]:
                with pytest.raises(ModelFailure) as error:
                    await provider.generate(config, secret, "classify", "仅输出 JSON", data)
                assert error.value.code == code and str(error.value) == code
                if code == "llm_rate_limited":
                    assert error.value.http_status == 429
                    assert error.value.detail == (
                        "code=123\ntype=rate_limit_error\nparam=model\nmessage=Synthetic limit for ［已隐藏］: ［已隐藏］"
                    )
                if code == "llm_network":
                    assert error.value.http_status is None
                    assert "ConnectError" in error.value.detail and "synthetic-private" not in error.value.detail
            assert await provider.generate(config, secret, "classify", "仅输出 JSON", data) == {"items": []}

    asyncio.run(exercise())
    assert len(requests) == 10
    for request in requests:
        assert str(request.url) == f"{endpoint}/chat/completions"
        assert request.headers["authorization"] == f"Bearer {SYNTHETIC_KEY}"
        body = json.loads(request.content)
        assert body["model"] == config.model
        assert body["response_format"] == {"type": "json_object"}
        assert {"max_tokens", "max_completion_tokens", "max_output_tokens"}.isdisjoint(body)
        assert {key: body[key] for key in ("thinking", "reasoning_effort") if key in body} == expected
        assert "tools" not in body and SYNTHETIC_KEY not in request.content.decode()
        assert body["messages"][0]["role"] == "system"
        assert json.loads(body["messages"][1]["content"]) == data


@pytest.mark.parametrize("ending, failure", [
    ("done", None),
    ("truncated", "llm_invalid_response"),
    ("length", "llm_context_limit"),
])
def test_deepseek_stream_publishes_text_before_final_json(ending, failure):
    def frame(delta, finish=None):
        value = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"

    published = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            first = frame({"reasoning_content": "合成思考"})
            split = first.index("合".encode()) + 1
            yield first[:split]
            yield first[split:]
            assert published == [("reasoning", "合成思考")]
            yield frame({"content": '{"items":'})
            assert published[-1] == ("output", '{"items":')
            yield frame({"content": "[]}"})
            yield frame({}, "length" if ending == "length" else "stop")
            if ending != "truncated":
                yield b"data: [DONE]\n\n"

    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=Stream())

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = ChatCompletions(client)
            config = ModelConfig(provider="openai_compatible", base_url="https://api.deepseek.com",
                                 model="deepseek-flash", version=1)

            def publish(section, text):
                published.append((section, text))

            generate = provider.generate_stream(config, SecretStr(SYNTHETIC_KEY), "classify", "合成指令", {}, publish)
            if failure:
                with pytest.raises(ModelFailure) as error:
                    await generate
                assert error.value.code == failure
            else:
                assert await generate == {"items": []}

    asyncio.run(exercise())
    assert published == [("reasoning", "合成思考"), ("output", '{"items":'), ("output", "[]}")]
    assert len(requests) == 1 and requests[0]["stream"] is True
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}


def test_analysis_console_replays_only_on_opt_in_and_is_session_scoped(tmp_path):
    with app_with_inputs(tmp_path, [source("合成弹幕", 1000, 1)]) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        wait_task(client, task, "succeeded")
        path = f"{API}/tasks/{task}/events"
        assert b"analysis.console" not in client.get(path).content

        response = client.get(path, params={"analysis_console": "true", "after": "999"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        frames = response.text.split("\n\n")
        terminal = [frame for frame in frames if frame.startswith("event: analysis.console\n")]
        assert len(terminal) == 1
        text = json.loads(terminal[0].split("\ndata: ")[1])
        assert text["replace"] is True and text["reasoning"] == ""
        assert json.loads(text["output"])["items"][0]["id"] == "d1"
        assert "event: analysis.console.end\ndata: {}" in frames

        foreign = LocalSession(Principal(id="another-user"), "foreign-csrf", float("inf"))
        client.app.state.sessions.sessions["another-session"] = foreign
        client.cookies.set("danmaku_session", "another-session", domain="127.0.0.1", path="/api/v1")
        assert client.get(path, params={"analysis_console": "true"}).status_code == 404


def test_stop_analysis_closes_model_stream_and_allows_a_new_run(tmp_path):
    started, closed = Event(), Event()

    class HeldStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"synthetic thinking"}}]}\n\n'
            started.set()
            await asyncio.Future()

        async def aclose(self):
            closed.set()

    class StopFixture(SyntheticModel):
        async def generate_stream(self, config, secret, operation, instructions, data, publish):
            if started.is_set():
                return await super().generate_stream(config, secret, operation, instructions, data, publish)
            transport = httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=HeldStream(),
            ))
            async with httpx.AsyncClient(transport=transport) as client:
                model = ChatCompletions(client)
                endpoint = config.model_copy(update={"base_url": "https://api.deepseek.com", "model": "deepseek-flash"})
                return await model.generate_stream(endpoint, SecretStr(SYNTHETIC_KEY), operation, instructions, data, publish)

    items = [source("合成弹幕", 1000, 1)]
    with app_with_inputs(tmp_path, items, model=StopFixture()) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        task = classify(client, project, body, headers).json()["id"]
        assert started.wait(3)
        path = f"{API}/tasks/{task}/stop"
        assert client.post(path).status_code == 403
        assert client.post(f"{API}/tasks/{body['snapshot_id']}/stop", headers=headers).status_code == 404
        response = client.post(path, headers=headers)
        assert response.status_code == 202
        stopped = response.json()
        assert stopped["state"] == "failed" and stopped["error"]["code"] == "analysis_stopped"
        assert stopped["progress"]["done"] == 0 and stopped["checkpoint"]["completed_batches"] == 0
        assert client.post(path, headers=headers).json() == stopped
        assert closed.wait(3)
        assert results(client, project, task) == []
        events = client.get(f"{API}/tasks/{task}/events", params={"analysis_console": "true"})
        assert "synthetic thinking" in events.text and "analysis.console.end" in events.text
        assert event_frames(events)[-1][2] == stopped
        retry = classify(client, project, body, headers, key="new-analysis").json()["id"]
        succeeded = wait_task(client, retry, "succeeded")
        assert client.post(f"{API}/tasks/{retry}/stop", headers=headers).json() == succeeded

    with app_with_inputs(tmp_path, items) as client:
        connect_session(client)
        assert client.get(f"{API}/tasks/{task}").json() == stopped
        assert results(client, project, task) == []


@pytest.mark.parametrize("claimed", [False, True])
def test_stop_analysis_blocks_work_before_request_or_settlement(tmp_path, monkeypatch, claimed):
    with app_with_inputs(tmp_path, [source("合成弹幕", 1000, 1)]) as client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        tasks = client.app.state.tasks
        claim = tasks.claim
        monkeypatch.setattr(tasks, "claim", lambda sending: None)
        task = classify(client, project, body, headers).json()["id"]
        if claimed:
            assert claim().task.id == task
            batch = tasks.next_batch(task)
        assert client.post(f"{API}/tasks/{task}/stop", headers=headers).status_code == 202
        assert claim() is None and tasks.next_batch(task) is None
        if claimed:
            with pytest.raises(BatchError) as error:
                client.portal.call(client.app.state.analysis.execute, batch)
            assert error.value.info.code == "analysis_stopped"
            assert tasks.settle(batch, None, AnalysisOutput()) is None
        assert client.get(f"{API}/tasks/{task}").json()["error"]["code"] == "analysis_stopped"
        assert results(client, project, task) == []


def test_provider_failures_end_analysis_and_allow_manual_retry(tmp_path):
    class TransientModel(RecordingModel):
        async def generate(self, config, secret, operation, instructions, data):
            result = await super().generate(config, secret, operation, instructions, data)
            if len(self.calls) == 1:
                raise ModelFailure("llm_rate_limited", http_status=429)
            if len(self.calls) == 2:
                raise ModelFailure("llm_dns", detail="gaierror errno=11001")
            return result

    model = TransientModel()
    client = app_with_inputs(tmp_path, [source("合成字幕", 1000, 0)], model=model)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        for attempt, (code, detail) in enumerate([
            ("llm_rate_limited", "HTTP 429"), ("llm_dns", "gaierror errno=11001"),
        ], start=1):
            task = classify(client, project, body, headers, key=f"attempt-{attempt}").json()["id"]
            failed = wait_task(client, task, "failed")
            assert failed["error"]["code"] == code and detail in failed["error"]["message"]
            assert not failed["error"]["retryable"] and failed["next_attempt_at"] is None
            assert len(model.calls) == attempt and results(client, project, task) == []
        fresh = classify(client, project, body, headers, key="manual-retry").json()
        assert fresh["id"] != task and fresh["error"] is None
        wait_task(client, fresh["id"], "succeeded")
        assert len(model.calls) == 3 and model.calls[0][2] == model.calls[1][2] == model.calls[2][2]
        assert len(results(client, project, fresh["id"])) == 1
