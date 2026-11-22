from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from danmaku.domain.analysis import Classification
from danmaku.persistence.analysis_repository import AnalysisRunRow, ClassificationRow
from danmaku.persistence.input_rows import SourceItemRow
from test_analysis import LABELED, RecordingModel, app_with_inputs, classify, prepare, results, source
from test_workbench import API, create_project, download, open_client, connect_session, wait_task


def get_review(client, endpoint, run, **filters):
    response = client.get(endpoint, params={"run_id": run, **filters})
    assert response.status_code == 200, response.text
    return response.json()


def patch_review(client, endpoint, run, revision, ids, selected, headers):
    return client.patch(endpoint, json={"run_id": run, "expected_revision": revision,
                                        "item_ids": ids, "selected": selected}, headers=headers)


def test_skip_analysis_defaults_to_all_selected_and_persists_review(tmp_path):
    model = RecordingModel()
    samples = [source("合成弹幕", 1000, 0), source("合成小字号弹幕", 2000, 1).model_copy(update={"font_size": 18})]
    with app_with_inputs(tmp_path, samples, model=model) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        snapshot = download(client, project, headers).json()["id"]
        wait_task(client, snapshot, "succeeded")
        project_url = f"{API}/projects/{project}"
        endpoint = f"{project_url}/review"
        body = {"kind": "classify", "snapshot_id": snapshot, "skip_analysis": True}
        response = classify(client, project, body, headers)
        assert response.status_code == 202, response.text
        task = response.json()
        run = task["id"]
        assert task["state"] == "succeeded" and task["checkpoint"]["analysis_skipped"] is True
        assert classify(client, project, body, headers).json() == task
        assert classify(client, project, {"kind": "classify", "snapshot_id": snapshot, "llm_config_version": 0},
                        headers).status_code == 409
        assert client.get(f"{project_url}/inputs").json()["workflow"]["analysis_id"] == run
        analysis = client.get(f"{project_url}/runs/{run}").json()
        assert analysis["strategy"] == "skipped" and analysis["config"] is None
        assert results(client, project, run) == []

        page = get_review(client, endpoint, run, selected=True)
        assert page["selected_count"] == len(page["items"]) == 2
        assert [row["font_size"] for row in page["items"]] == [25, 18]
        assert all(row["selected"] and row["classification"] is None for row in page["items"])
        item = page["items"][1]["item_id"]
        assert patch_review(client, endpoint, run, 0, [item], False, headers).json() == {
            "revision": 1, "changed_count": 1, "selected_count": 1,
        }
        assert client.put(f"{project_url}/workflow/review", json={"run_id": run, "revision": 1},
                          headers=headers).status_code == 200
        preview = client.get(f"{project_url}/send-preview", params={"run_id": run})
        assert preview.status_code == 200 and preview.json()["selected_count"] == 1

    with app_with_inputs(tmp_path, samples, model=model) as client:
        headers = connect_session(client)
        page = get_review(client, endpoint, run, selected=False)
        assert [(row["item_id"], row["selection_source"]) for row in page["items"]] == [(item, "manual")]
        assert classify(client, project, body, headers).json() == task
        reset = client.post(f"{endpoint}/reset", json={"run_id": run, "expected_revision": 1}, headers=headers)
        assert reset.json() == {"revision": 2, "changed_count": 1, "selected_count": 2}
        assert not get_review(client, endpoint, run, selected=False)["items"]
    assert not model.calls


def test_review_text_persists_and_is_used_for_search_and_preview(tmp_path):
    samples = [source("合成原文", 1000, 0)]
    with app_with_inputs(tmp_path, samples) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        snapshot = download(client, project, headers).json()["id"]
        wait_task(client, snapshot, "succeeded")
        project_url = f"{API}/projects/{project}"
        endpoint = f"{project_url}/review"
        run = classify(client, project, {"kind": "classify", "snapshot_id": snapshot, "skip_analysis": True},
                       headers).json()["id"]
        item = get_review(client, endpoint, run)["items"][0]
        command = {"run_id": run, "expected_revision": 0, "item_id": item["item_id"], "text": "合成改写"}
        assert client.patch(f"{endpoint}/text", json={**command, "text": "  "}, headers=headers).status_code == 422
        response = client.patch(f"{endpoint}/text", json=command, headers=headers)
        assert response.json() == {"revision": 1, "changed_count": 1, "selected_count": 1}
        assert client.patch(f"{endpoint}/text", json=command, headers=headers).status_code == 409

    with app_with_inputs(tmp_path, samples) as client:
        headers = connect_session(client)
        page = get_review(client, endpoint, run, q="合成改写")
        assert page["items"] == [{**item, "text": "合成改写", "text_edited": True}]
        assert not get_review(client, endpoint, run, q="合成原文")["items"]
        preview = client.get(f"{project_url}/send-preview", params={"run_id": run})
        assert preview.status_code == 200, preview.text
        assert preview.json()["items"][0]["text"] == "合成改写"
        command["expected_revision"] = 1
        assert client.patch(f"{endpoint}/text", json=command, headers=headers).json()["changed_count"] == 0
        command["text"] = None
        assert client.patch(f"{endpoint}/text", json=command, headers=headers).json()["revision"] == 2
        assert get_review(client, endpoint, run)["items"] == [item]


def test_review_filters_evidence_and_run_isolation(tmp_path):
    samples = [source(text, 2000, index) for index, (text, _) in enumerate(LABELED)]
    samples[5] = samples[5].model_copy(update={"font_size": 18})
    client = app_with_inputs(tmp_path, samples)
    with client:
        headers = connect_session(client)
        project, body = prepare(client, headers)
        endpoint = f"{API}/projects/{project}/review"
        run = classify(client, project, body, headers).json()["id"]
        wait_task(client, run, "succeeded")

        repository_factory = client.app.state.review.database
        with repository_factory(write=True) as repository:
            inputs = repository.analysis_input(run)
            keys = [item.source_key for item in repository.analysis_source_items(inputs.snapshot_id)]
            cue = repository.cues(inputs.subtitle_version_id)[0]
            cases = [
                Classification(id=keys[0], category="translation", reason="合成理由", cue_ids=[f"zh:{cue.id}", "ja:c1"]),
                Classification(id=keys[1], category="other"),
                Classification(id=keys[2], category="other", diagnostic="needs_review"),
                Classification(id=keys[3], category="other", diagnostic="needs_review"),
                Classification(id=keys[4], category="other", diagnostic="needs_review"),
                Classification(id=keys[5], category="other"),
                Classification(id=keys[6], diagnostic="invalid_response"),
            ]
            for case in cases:
                repository.session.get(ClassificationRow, (inputs.id, case.id)).result = case.model_dump()

        page = get_review(client, endpoint, run)
        assert page["revision"] == 0 and page["selected_count"] == 1
        assert [row["selected"] for row in page["items"]] == [False, True, False, False, False, False, False]
        assert page["items"][5]["font_size"] == 18
        first = page["items"][0]
        assert first["classification"]["reason"] == "合成理由"
        assert [item["id"] for item in first["evidence_cues"]] == [f"zh:{cue.id}", "ja:c1"]
        assert {item["id"][:2] for item in first["nearby_cues"]} == {"ja", "zh"}
        assert first["evidence_cues"][0]["plaintext"] == cue.plaintext
        filtered = get_review(client, endpoint, run, category="other",
                              selected=False, q=LABELED[3][0][:4])
        assert [row["item_id"] for row in filtered["items"]] == [keys[3]]
        assert filtered["selected_count"] == 1
        assert [row["item_id"] for row in get_review(client, endpoint, run, selected=True)["items"]] == [keys[1]]
        assert not get_review(client, endpoint, run, q="%")["items"]
        assert client.get(endpoint, params={"run_id": run, "q": "x" * 201}).status_code == 422
        assert patch_review(client, endpoint, run, 0, [keys[0]], True, {}).status_code == 403
        assert patch_review(client, endpoint, run, 0, [keys[5]], True, headers).json() == {
            "revision": 1, "changed_count": 1, "selected_count": 2,
        }
        assert patch_review(client, endpoint, run, 1, [keys[5], keys[5]], True, headers).json() == {
            "revision": 1, "changed_count": 0, "selected_count": 2,
        }
        assert patch_review(client, endpoint, run, 1, [keys[1], "missing"], False, headers).status_code == 404

        rerun = classify(client, project, body, headers, key="alternate").json()["id"]
        wait_task(client, rerun, "succeeded")
        new = get_review(client, endpoint, rerun)
        assert new["revision"] == 1
        assert all(row["selection_source"] == "suggestion" for row in new["items"])
        old = get_review(client, endpoint, run)
        assert old["items"][5]["selected"] and old["items"][5]["selection_source"] == "manual"
        assert old["items"][0]["classification"] == first["classification"]
        assert old["items"][1]["selected"]

        other_project = create_project(client, headers, name="另一个合成项目")
        other_endpoint = f"{API}/projects/{other_project}/review"
        assert client.get(other_endpoint, params={"run_id": run}).status_code == 404
        assert patch_review(client, other_endpoint, run, 1, [keys[0]], False, headers).status_code == 404

        reset = client.post(f"{endpoint}/reset", json={"run_id": run, "expected_revision": 1}, headers=headers)
        assert reset.status_code == 200, reset.text
        assert reset.json() == {"revision": 2, "changed_count": 1, "selected_count": 1}
        small = get_review(client, endpoint, run)["items"][5]
        assert not small["selected"] and small["selection_source"] == "suggestion"

        path = f"{API}/settings"
        assert client.get(path).json()["review"] == {"uncheck_small_by_default": True}
        update = {"review": {"uncheck_small_by_default": False}}
        assert client.put(path, headers=headers, json=update).json()["review"] == update["review"]
        assert client.put(path, headers=headers, json=update).status_code == 200
        page = get_review(client, endpoint, run, selected=True)
        assert page["revision"] == 3
        assert [row["item_id"] for row in page["items"]] == [keys[1], keys[5]]
        assert patch_review(client, endpoint, run, 2, [keys[5]], False, headers).status_code == 409
        assert patch_review(client, endpoint, run, 3, [keys[5]], False, headers).status_code == 200
        manual = get_review(client, endpoint, run)["items"][5]
        assert not manual["selected"] and manual["selection_source"] == "manual"


def populate_large_run(client, headers):
    project, body = prepare(client, headers)
    run = classify(client, project, body, headers).json()["id"]
    wait_task(client, run, "succeeded")
    with client.app.state.review.database(write=True) as repository:
        inputs = repository.analysis_input(run)
        original = repository.analysis_source_items(inputs.snapshot_id)[0]
        extra = [
            original.model_copy(update={"source_key": f"id:{index:05}", "time_ms": 2000 + index // 100})
            for index in range(9999)
        ]
        repository.session.add_all([
            SourceItemRow(snapshot_id=inputs.snapshot_id, source_key=item.source_key, time_ms=item.time_ms,
                          raw={}, normalized=item.model_dump()) for item in extra
        ])
        repository.session.flush()
        repository.save_classifications(inputs, [
            Classification(id=item.source_key, category="other") for item in extra
        ])
    return project, run


def test_review_10000_items_concurrent_changes_and_restart(tmp_path):
    client = app_with_inputs(tmp_path, [source("你已经知道了？", 2000, 0)])
    with client:
        headers = connect_session(client)
        project, run = populate_large_run(client, headers)
        endpoint = f"{API}/projects/{project}/review"
        positions = []
        tokens = []
        first_ids = []
        while True:
            page = get_review(client, endpoint, run, limit=100, **({"cursor": tokens[-1]} if tokens else {}))
            assert page["selected_count"] == 9999 and len(page["items"]) == 100
            positions.extend((row["time_ms"], row["item_id"]) for row in page["items"])
            if len(tokens) < 2:
                first_ids.append([row["item_id"] for row in page["items"]])
            if page["next_cursor"] is None:
                break
            tokens.append(page["next_cursor"])
        assert len(positions) == 10_000
        assert positions == sorted(set(positions))
        assert client.get(endpoint, params={"run_id": run, "cursor": tokens[0], "selected": True}).status_code == 400

        barrier = Barrier(2)

        def apply(ids):
            barrier.wait(timeout=5)
            return patch_review(client, endpoint, run, 0, ids, False, headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(apply, first_ids))
        for index, response in enumerate(responses):
            if response.status_code == 503:
                assert response.json()["error"]["code"] == "database_busy"
                assert response.json()["error"]["retryable"]
                responses[index] = patch_review(client, endpoint, run, 0, first_ids[index], False, headers)
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(index for index, response in enumerate(responses) if response.status_code == 200)
        assert responses[1 - winner].json()["error"]["code"] == "revision_conflict"
        assert responses[winner].json()["changed_count"] == 100
        second = patch_review(client, endpoint, run, 1, first_ids[1 - winner][:1], False, headers)
        assert second.json()["revision"] == 2

    client = open_client(tmp_path)
    with client:
        connect_session(client)
        for index, params in enumerate([{}, {"cursor": tokens[0]}]):
            page = get_review(client, endpoint, run, limit=100, **params)
            assert page["revision"] == 2
            expected_count = 100 if index == winner else 1
            assert sum(row["selection_source"] == "manual" for row in page["items"]) == expected_count
            assert all(not row["selected"] for row in page["items"] if row["selection_source"] == "manual")
        with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone() == (101,)


@pytest.mark.parametrize("version", [3, 19, 20, 25])
def test_upgrade_preserves_run_and_creates_backup(tmp_path, monkeypatch, version):
    client = app_with_inputs(tmp_path, [source("你已经知道了？", 2000, 0)])
    with monkeypatch.context() as patch:
        if version < 20:
            patch.setattr(AnalysisRunRow.__table__.c.subtitle_version_id, "nullable", False)
        with client:
            headers = connect_session(client)
            project, body = prepare(client, headers)
            run = classify(client, project, body, headers).json()["id"]
            wait_task(client, run, "succeeded")
            endpoint = f"{API}/projects/{project}/review"
            if version == 19:
                item = get_review(client, endpoint, run)["items"][0]["item_id"]
                assert patch_review(client, endpoint, run, 0, [item], True, headers).status_code == 200
                assert client.post(f"{endpoint}/reset", json={"run_id": run, "expected_revision": 1},
                                   headers=headers).status_code == 200
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        if version == 3:
            connection.execute("DROP TABLE review_decisions")
        connection.execute("DROP TABLE review_offsets")
        connection.execute("DROP TABLE review_texts")
        connection.execute("UPDATE schema_version SET version = ?", (version,))

    client = open_client(tmp_path)
    with client:
        headers = connect_session(client)
        page = get_review(client, endpoint, run)
        assert page["revision"] == (2 if version == 19 else 0)
        row = page["items"][0]
        assert row["offset_ms"] == 0
        assert row["classification"]["category"] == "translation" and not row["selected"]
        skipped = classify(client, project, {"kind": "classify", "snapshot_id": body["snapshot_id"],
                                              "skip_analysis": True}, headers, key="skip")
        assert skipped.status_code == 202, skipped.text
    with sqlite3.connect(Path(str(path) + f".v{version}.bak")) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (version,)
        columns = backup.execute("PRAGMA table_info(analysis_runs)").fetchall()
        assert next(column[3] for column in columns if column[1] == "subtitle_version_id") == (version < 20)
