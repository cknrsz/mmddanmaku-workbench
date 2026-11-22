import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID

from fastapi.testclient import TestClient

from danmaku.application.projects import project_activity
from danmaku.application.sessions import LocalSession, LocalSessions
from danmaku.domain.workbench import Principal, ProjectTaskState
from danmaku.main import create_app

API = "/api/v1"
ORIGIN = "http://127.0.0.1:8000"
PROJECT = {
    "name": "合成项目", "source_bvid": "BV0000000000", "target_bvid": "BV0000000001",
    "source_page": 1, "target_page": 2,
}


async def connected_probe():
    return True


def open_client(path: Path, **options):
    if not (options.get("live_sender") or options.get("bilibili_transport")):
        options.setdefault("connection_probe", connected_probe)
    app = create_app(data_dir=path, poll_interval=0.01, **options)
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))


def connect_session(client):
    origin = str(client.base_url).rstrip("/")
    response = client.post(f"{API}/session", headers={"Origin": origin, "X-Danmaku-Local": "1"})
    assert response.status_code == 201
    if "set-cookie" in response.headers:
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}


def create_project(client, headers, **changes):
    response = client.post(f"{API}/projects", json={**PROJECT, **changes}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def download(client, project, headers, key="synthetic-key"):
    return client.post(f"{API}/projects/{project}/tasks", json={"kind": "download"},
                       headers={**headers, "Idempotency-Key": key})


def wait_task(client, task_id, state, *, after_version=0):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        response = client.get(f"{API}/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["state"] == state and task["version"] > after_version:
            return task
        if task["state"] in {"succeeded", "failed"}:
            raise AssertionError(f"Unexpected terminal state {task}")
        sleep(0.01)
    raise AssertionError(f"Task did not reach {state}: {task}")


def event_frames(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    result = []
    for frame in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":"))
        if "id" in fields:
            result.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return result


def test_local_session_origin_loopback_csrf_and_expiry(tmp_path):
    now = 0.0
    sessions = LocalSessions(clock=lambda: now)
    client = open_client(tmp_path, sessions=sessions)
    with client:
        assert client.get(f"{API}/projects").status_code == 401
        for bad in ({}, {"Origin": ORIGIN}, {"Origin": "http://127.0.0.1:5173", "X-Danmaku-Local": "1"},
                    {"Origin": ORIGIN, "X-Danmaku-Local": "1", "Sec-Fetch-Site": "cross-site"}):
            assert client.post(f"{API}/session", headers=bad).status_code == 403
        remote = TestClient(client.app, base_url=ORIGIN, client=("192.0.2.1", 50000))
        assert remote.post(f"{API}/session", headers={"Origin": ORIGIN, "X-Danmaku-Local": "1"}).status_code == 403
        headers = connect_session(client)
        assert connect_session(client) == headers
        assert client.get(f"{API}/session").json() == {"csrf_token": headers["X-CSRF-Token"]}
        for bad in (
            {}, {"Origin": ORIGIN}, {"Origin": "http://outside.invalid", "X-CSRF-Token": headers["X-CSRF-Token"]},
            {**headers, "X-CSRF-Token": "wrong"},
        ):
            assert client.post(f"{API}/projects", headers=bad, json=PROJECT).status_code == 403
        assert client.get(f"{API}/projects").json()["items"] == []
        token = client.cookies.get("danmaku_session")
        assert client.delete(f"{API}/session", headers=headers).status_code == 204
        assert client.get(f"{API}/session", headers={"Cookie": f"danmaku_session={token}"}).status_code == 401
        headers = connect_session(client)
        now += sessions.session_seconds
        assert client.get(f"{API}/session").status_code == 401
        assert connect_session(client) != headers

    with client:
        assert client.get(f"{API}/session").status_code == 401
        assert connect_session(client) != headers


def test_project_pagination_validation_and_owner_scope(tmp_path, monkeypatch):
    project_ids = iter(UUID(int=value) for value in (20, 30, 10))
    monkeypatch.setattr("danmaku.application.projects.uuid4", lambda: next(project_ids))
    sessions = LocalSessions()
    client = open_client(tmp_path, sessions=sessions)
    with client:
        headers = connect_session(client)
        first = create_project(client, headers)
        second = create_project(client, headers, name="另一个合成项目")
        detail = client.get(f"{API}/projects/{first}").json()
        assert detail == {
            "id": first, "creation_order": 1, **PROJECT, "completed": False,
            "activity": {"stage": "download", "running": False, "active": False},
            "send_queue": None, "send_needs_review": False, "send_partial_failure": False,
            "snapshot_ids": [], "subtitle_version_ids": [], "run_ids": [],
        }
        first_page = client.get(f"{API}/projects", params={"limit": 1}).json()
        cursor = first_page["next_cursor"]
        third = create_project(client, headers, name="最新合成项目")
        assert client.delete(f"{API}/projects/{second}", headers=headers).status_code == 204
        second_page = client.get(f"{API}/projects", params={"limit": 1, "cursor": cursor}).json()
        assert [first_page["items"][0]["id"], second_page["items"][0]["id"]] == [second, first]
        assert second_page["next_cursor"] is None
        assert [item["id"] for item in client.get(f"{API}/projects").json()["items"]] == [third, first]

        synthetic_value = "synthetic-invalid-field"
        rejected = client.post(f"{API}/projects", headers=headers, json={**PROJECT, "source_page": synthetic_value})
        assert rejected.status_code == 422
        assert synthetic_value not in rejected.text
        task_id = download(client, first, headers).json()["id"]

        # Local sessions share one owner. Inject a second principal at the session boundary.
        other = LocalSession(Principal(id="other-synthetic-owner"), "other-csrf", float("inf"))
        sessions.sessions["other-session"] = other
        client.cookies.clear()
        client.cookies.set("danmaku_session", "other-session")
        foreign_headers = {"Origin": ORIGIN, "X-CSRF-Token": other.csrf_token}
        assert client.get(f"{API}/projects").json()["items"] == []
        assert client.get(f"{API}/projects", params={"cursor": cursor}).status_code == 400
        assert client.patch(f"{API}/projects/{first}/completion", headers=foreign_headers,
                            json={"completed": True}).status_code == 404
        for path in (f"/projects/{first}", f"/projects/{first}/tasks",
                     f"/tasks/{task_id}", f"/tasks/{task_id}/events"):
            assert client.get(API + path).status_code == 404
        assert download(client, first, foreign_headers).status_code == 404
        for action in ("pause", "resume"):
            response = client.post(f"{API}/tasks/{task_id}/{action}", json={"expected_version": 1},
                                   headers=foreign_headers)
            assert response.status_code == 404


def test_legacy_projects_keep_insertion_order_on_upgrade(tmp_path, monkeypatch):
    project_ids = iter(UUID(int=value) for value in (20, 30, 10))
    monkeypatch.setattr("danmaku.application.projects.uuid4", lambda: next(project_ids))
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        first, second = [create_project(client, headers) for _ in range(2)]

    database = sqlite3.connect(tmp_path / "workbench.sqlite3")
    try:
        with database:
            database.execute("DROP TABLE project_creation_order")
            database.execute("UPDATE schema_version SET version = 15")
    finally:
        database.close()

    with open_client(tmp_path) as client:
        headers = connect_session(client)
        third = create_project(client, headers)
        items = client.get(f"{API}/projects").json()["items"]
        assert [(item["id"], item["creation_order"]) for item in items] == [(third, 3), (second, 2), (first, 1)]
    assert (tmp_path / "workbench.sqlite3.v15.bak").is_file()


def test_project_tasks_paginate_newest_first(tmp_path, monkeypatch):
    task_ids = iter(UUID(int=value) for value in (30, 10, 20, 40))
    monkeypatch.setattr("danmaku.application.tasks.service.uuid4", lambda: next(task_ids))
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        url = f"{API}/projects/{project}/tasks"
        oldest, middle, newest = [download(client, project, headers, key).json()
                                  for key in ("oldest", "middle", "newest")]
        assert oldest["creation_order"] < middle["creation_order"] < newest["creation_order"]

        first_page = client.get(url, params={"limit": 1, "kind": "download"}).json()
        assert [task["id"] for task in first_page["items"]] == [newest["id"]]
        latest = download(client, project, headers, "latest").json()

        older_page = client.get(url, params={"kind": "download", "cursor": first_page["next_cursor"]}).json()
        assert [task["id"] for task in older_page["items"]] == [middle["id"], oldest["id"]]
        assert older_page["next_cursor"] is None
        assert client.get(url, params={"cursor": first_page["next_cursor"]}).status_code == 400

        replayed = download(client, project, headers, "oldest").json()
        assert (replayed["id"], replayed["creation_order"]) == (oldest["id"], oldest["creation_order"])
        assert [task["id"] for task in client.get(url).json()["items"]] == [
            latest["id"], newest["id"], middle["id"], oldest["id"],
        ]


def test_project_activity_preserves_active_flag_when_falling_back_to_send_stage():
    task = ProjectTaskState(
        kind="send", state="cancelling", live=True, send_started=True, send_complete=False,
        send_needs_review=True, send_partial_failure=False, creation_order=1,
    )
    assert project_activity({task}, False).model_dump() == {"stage": "send", "running": False, "active": True}


def test_project_completion_override_persists_and_can_be_removed(tmp_path):
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        project = create_project(client, headers)
        url = f"{API}/projects/{project}/completion"
        assert client.patch(url, json={"completed": True}).status_code == 403
        assert client.patch(url, headers=headers, json={"completed": "true"}).status_code == 422
        for _ in range(2):
            response = client.patch(url, headers=headers, json={"completed": True})
            assert response.status_code == 200
            assert response.json()["completed"] is True
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        assert client.get(f"{API}/projects").json()["items"][0]["completed"] is True
        assert client.patch(url, headers=headers, json={"completed": False}).json()["completed"] is False
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        assert client.get(f"{API}/projects/{project}").json()["completed"] is False
        assert client.delete(f"{API}/projects/{project}", headers=headers).status_code == 204


def test_concurrent_creation_and_pause_commit_once_and_sse_replays(tmp_path):
    release = Event()

    async def batch(_task):
        while not release.is_set():
            await asyncio.sleep(0.01)

    client = open_client(tmp_path, execute_batch=batch)
    with client:
        headers = connect_session(client)
        project = create_project(client, headers)
        assert client.post(f"{API}/projects/{project}/tasks", json={"kind": "download"},
                           headers=headers).status_code == 422
        barrier = Barrier(2, timeout=3)

        def submit():
            barrier.wait()
            return download(client, project, headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = [future.result() for future in (pool.submit(submit), pool.submit(submit))]
        task_id = left.json()["id"]
        assert left.status_code == right.status_code == 202
        assert left.headers["Location"] == f"{API}/tasks/{task_id}"
        assert right.json()["id"] == task_id
        assert len(client.get(f"{API}/projects/{project}/tasks").json()["items"]) == 1

        running = wait_task(client, task_id, "running")
        barrier = Barrier(2, timeout=3)

        def pause():
            barrier.wait()
            return client.post(f"{API}/tasks/{task_id}/pause", headers=headers,
                               json={"expected_version": running["version"]})

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = [future.result() for future in (pool.submit(pause), pool.submit(pause))]
        assert left.status_code == right.status_code == 202
        assert left.json() == right.json()
        assert left.json()["version"] == running["version"] + 1
        assert left.json()["state"] == "pausing"
        assert left.json()["progress"]["done"] == 0

        release.set()
        paused = wait_task(client, task_id, "paused")
        assert client.get(f"{API}/projects/{project}").json()["activity"] == {
            "stage": "download", "running": False, "active": False,
        }
        assert paused["progress"]["done"] == 5
        assert paused["checkpoint"] == {"completed_batches": 1}
        stale = client.post(f"{API}/tasks/{task_id}/resume", headers=headers,
                            json={"expected_version": running["version"]})
        assert stale.status_code == 409
        resumed = client.post(f"{API}/tasks/{task_id}/resume", headers=headers,
                              json={"expected_version": paused["version"]})
        assert resumed.status_code == 202
        final = wait_task(client, task_id, "succeeded")
        assert final["progress"]["done"] == 50

        events_url = f"{API}/tasks/{task_id}/events"
        frames = event_frames(client.get(events_url, params={"after": running["version"]}))
        assert [seq for seq, _, _ in frames] == list(range(running["version"] + 1, final["version"] + 1))
        assert [frame[2]["state"] for frame in frames].count("pausing") == 1
        assert frames[-1] == (final["version"], "task.updated", final)
        replay = client.get(events_url, params={"after": 0},
                            headers={"Last-Event-ID": str(final["version"] - 1)})
        assert event_frames(replay) == [frames[-1]]
        assert event_frames(client.get(events_url, params={"after": final["version"] + 1})) == [
            (final["version"], "reset", final),
        ]
        assert client.get(events_url, params={"after": "-1"}).status_code == 400


def test_restart_preserves_progress_and_resumes_from_checkpoint(tmp_path):
    second_started = Event()

    async def interrupted_batch(record):
        if record.task.checkpoint.completed_batches == 1:
            second_started.set()
            await asyncio.Event().wait()

    client = open_client(tmp_path, execute_batch=interrupted_batch)
    with client:
        headers = connect_session(client)
        old_cookie = client.cookies.get("danmaku_session")
        project = create_project(client, headers)
        first_id = download(client, project, headers).json()["id"]
        assert second_started.wait(3)
        running = wait_task(client, first_id, "running")
        queued_id = download(client, project, headers, "other-key").json()["id"]
        assert client.get(f"{API}/tasks/{queued_id}").json()["state"] == "queued"

    batches = []

    async def record_batch(record):
        batches.append((record.task.id, record.task.checkpoint.completed_batches))

    restarted = open_client(tmp_path, execute_batch=record_batch)
    with restarted:
        assert restarted.get(f"{API}/session", headers={
            "Cookie": f"danmaku_session={old_cookie}",
        }).status_code == 401
        headers = connect_session(restarted)
        paused = wait_task(restarted, first_id, "paused")
        assert paused["version"] == running["version"] + 1
        assert paused["checkpoint"] == running["checkpoint"] == {"completed_batches": 1}
        assert paused["error"]["code"] == "interrupted"
        assert download(restarted, project, headers).json()["id"] == first_id
        wait_task(restarted, queued_id, "succeeded")
        response = restarted.post(f"{API}/tasks/{first_id}/resume", headers=headers,
                                  json={"expected_version": paused["version"]})
        assert response.status_code == 202
        assert wait_task(restarted, first_id, "succeeded")["progress"]["done"] == 50
        assert [batch for task, batch in batches if task == first_id] == list(range(1, 10))

