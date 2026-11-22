import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
import sqlite3
from threading import Barrier, Event

import httpx
import pytest

from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.adapters.synthetic import synthetic_part
from danmaku.application.sessions import LocalSession, LocalSessions
from danmaku.application.tasks.service import Tasks
from danmaku.domain.inputs import ProjectParts
from danmaku.domain.sending import Attempt, Delivery, SendBatch, SendItem, SendResult
from danmaku.domain.workbench import Principal, SendCheckpoint, SendCreate, Task, TaskProgress, TaskRecord
from test_analysis import app_with_inputs, classify, prepare, source
from test_review import get_review, patch_review
from test_workbench import API, create_project, event_frames, open_client, connect_session, wait_task


class ScriptedSender:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def send(self, work):
        self.calls.append(work)
        response = next(self.responses, "confirmed")
        if callable(response):
            return await response()
        if isinstance(response, SendResult):
            return response
        return SendResult(outcome=response, receipt=str(76000 + len(self.calls)) if response == "confirmed" else None)


def make_run(client, headers):
    project, body = prepare(client, headers)
    run = classify(client, project, body, headers).json()["id"]
    wait_task(client, run, "succeeded")
    return project, run


def choose(client, headers, project, run, choices):
    endpoint = f"{API}/projects/{project}/review"
    review = get_review(client, endpoint, run)
    revision = review["revision"]
    for index, selected in choices:
        response = patch_review(client, endpoint, run, revision, [review["items"][index]["item_id"]], selected, headers)
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
    return revision


def submit(client, headers, project, run, revision, key="send", **fields):
    return client.post(f"{API}/projects/{project}/tasks", headers={**headers, "Idempotency-Key": key}, json={
        "kind": "send", "run_id": run, "expected_review_revision": revision, **fields,
    })


def deliveries(client, task_id, **filters):
    response = client.get(f"{API}/tasks/{task_id}/deliveries", params=filters)
    assert response.status_code == 200, response.text
    return response.json()


def control(client, headers, task, action):
    return client.post(f"{API}/tasks/{task['id']}/{action}", headers=headers,
                       json={"expected_version": task["version"]})


def resolve(client, headers, task, delivery, outcome):
    return client.post(f"{API}/tasks/{task['id']}/deliveries/{delivery['id']}/resolve", headers=headers,
                       json={"expected_version": task["version"], "outcome": outcome})


ACCOUNT = "17001"
TARGET = "73"
CONFIRM = {"confirmation": {"account_id": ACCOUNT, "target_cid": TARGET}}


def send_source(text, time_ms, index):
    return source(text, time_ms, index).model_copy(update={"platform_id": str(2000 + index)})


def live_client(path, items, **options):
    def nav(_request):
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": int(ACCOUNT)}})

    return app_with_inputs(path, items, bilibili_transport=httpx.MockTransport(nav), **options)


def configure_live_run(client, headers):
    project, run = make_run(client, headers)
    with client.app.state.tasks.database(write=True) as repository:
        analysis = repository.run(project, run)
        snapshot = repository.snapshot(project, analysis.snapshot_id)
        target = synthetic_part("BV0000000001", 2).model_copy(update={"cid": TARGET, "duration_ms": 12_000})
        repository.save_video_parts(project, ProjectParts(mode="bilibili", source=snapshot.part, target=target))
    if not client.get(f"{API}/settings/bilibili").json()["configured"]:
        assert client.put(f"{API}/settings/bilibili", headers=headers, json={
            "credential": {"sessdata": "synthetic-session", "bili_jct": "synthetic-csrf"},
        }).status_code == 200
    return project, run


def no_send_delay(client, headers):
    assert client.put(f"{API}/settings/sending", headers=headers, json={
        "item_min_seconds": 0, "item_max_seconds": 0, "rate_limit_seconds": 0,
    }).status_code == 200


def seed_legacy_send(client, project, run, revision, *, task_id="synthetic-legacy-batch", state="succeeded"):
    with client.app.state.tasks.database(write=True) as repository:
        task = Task(id=task_id, project_id=project, creation_order=0, kind="send", state=state, version=1,
                    progress=TaskProgress(total=1, done=int(state == "succeeded")),
                    checkpoint=SendCheckpoint(batch_id=task_id))
        account = Principal(id="local-owner")
        details = repository.project(account.id, project)
        repository.save_task(TaskRecord(
            task=task, inputs=SendCreate(kind="send", run_id=run, expected_review_revision=revision),
            created_by=account.id, idempotency_key=task_id, source_bvid=details.source_bvid,
            source_page=details.source_page,
        ))
        receipt = "mock:synthetic-receipt" if state == "succeeded" else None
        item = SendItem(item_id="id:2000", text="合成条目", time_ms=2000, color_rgb=0xFFFFFF, font_size=25, mode=1)
        delivery = Delivery(id="000000000001", item=item, status="confirmed" if receipt else "pending", receipt=receipt)
        target = synthetic_part(details.target_bvid, details.target_page)
        repository.add_send_batch(SendBatch(id=task_id, run_id=run, review_revision=revision, target=target), [delivery])
        if receipt:
            now = datetime(2026, 1, 1, tzinfo=UTC)
            repository.save_attempt(task_id, delivery.id, Attempt(
                number=1, started_at=now, finished_at=now, outcome="confirmed", receipt=receipt,
            ))
    return task_id


def test_delete_project_with_active_and_completed_tasks(tmp_path):
    entered, release = Event(), Event()

    async def send():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome="confirmed", receipt="76001")

    sessions = LocalSessions()
    client = live_client(tmp_path, [send_source("合成弹幕", 1137, 0)],
                             live_sender=ScriptedSender(send), sessions=sessions)
    with client:
        headers = connect_session(client)
        project, run = configure_live_run(client, headers)
        other = create_project(client, headers)
        path = f"{API}/projects/{project}"
        revision = choose(client, headers, project, run, [(0, True)])
        batch = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        try:
            assert entered.wait(5)
            assert client.delete(path).status_code == 403
            conflict = client.delete(path, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "project_tasks_active"
            assert client.get(path).json()["activity"] == {"stage": "send", "running": True, "active": True}
            completion = client.patch(f"{path}/completion", json={"completed": True}, headers=headers)
            assert completion.status_code == 409 and completion.json()["error"]["code"] == "project_tasks_active"
        finally:
            release.set()
        wait_task(client, batch, "succeeded")
        summary = client.get(path).json()
        assert summary["completed"] is True
        assert summary["activity"] == {"stage": "send", "running": False, "active": False}
        sessions.sessions["foreign-session"] = LocalSession(
            Principal(id="foreign-owner"), "foreign-csrf", float("inf"),
        )
        response = client.delete(path, headers={
            "Cookie": "danmaku_session=foreign-session", "Origin": headers["Origin"],
            "X-CSRF-Token": "foreign-csrf",
        })
        assert response.status_code == 404
        assert client.delete(path, headers=headers).status_code == 204
        assert client.delete(path, headers=headers).status_code == 404
        assert client.get(path).status_code == 404
        assert client.get(f"{API}/tasks/{batch}").status_code == 404
        assert client.get(f"{API}/tasks/{run}/events").status_code == 404
        assert [item["id"] for item in client.get(f"{API}/projects").json()["items"]] == [other]


@pytest.mark.parametrize(("outcome", "pause_first"), [
    ("confirmed", False), ("rejected", False), ("unknown", False), ("confirmed", True),
])
def test_send_priority_swaps_positions_after_in_flight_settles(tmp_path, outcome, pause_first):
    entered, release = Event(), Event()
    priority_entered, priority_release = Event(), Event()

    async def hold_send():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome=outcome, receipt="76001" if outcome == "confirmed" else None)

    async def hold_priority_send():
        priority_entered.set()
        while not priority_release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome="confirmed", receipt="76002")

    sender = ScriptedSender(hold_send, hold_priority_send)
    items = [send_source("合成弹幕甲", 1137, 0), send_source("合成弹幕乙", 1144, 1)]
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        runs = []
        for index in range(3):
            project, run = configure_live_run(client, headers)
            revision = choose(client, headers, project, run, [(0, True), (1, index == 0)])
            runs.append((project, run, revision))

        active_url = f"{API}/send-queue/active"
        assert client.get(active_url).json() is None
        first = submit(client, headers, *runs[0], **CONFIRM).json()
        try:
            assert entered.wait(5)
            middle, last = [submit(client, headers, *entry, **CONFIRM).json() for entry in runs[1:]]
            if pause_first:
                running = wait_task(client, first["id"], "running")
                assert control(client, headers, running, "pause").json()["state"] == "pausing"
            assert client.get(active_url).json() == first["id"]
            path = f"{API}/tasks/{last['id']}/send-priority"
            command = {"expected_version": last["version"]}
            assert client.post(path, json=command).status_code == 403
            accepted = client.post(path, headers=headers, json=command)
            assert accepted.status_code == 202
            assert accepted.json()["state"] == "queued"
            assert client.get(active_url).json() is None
            assert client.post(path, headers=headers, json=command).json()["error"]["code"] == "version_conflict"
            middle_path = f"{API}/tasks/{middle['id']}/send-priority"
            assert client.post(middle_path, headers=headers, json={
                "expected_version": middle["version"],
            }).json()["error"]["code"] == "send_priority_unavailable"
            assert [work.task_id for work in sender.calls] == [first["id"]]
            with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
                positions = connection.execute("SELECT task_id FROM send_queue ORDER BY position").fetchall()
            assert positions == [(task["id"],) for task in (first, middle, last)]
        finally:
            release.set()

        if outcome == "unknown":
            try:
                assert priority_entered.wait(5)
                active = wait_task(client, first["id"], "running")
                assert active["checkpoint"]["next_send_task_id"] is None
                assert active["progress"]["unknown"] == 1
            finally:
                priority_release.set()
            wait_task(client, first["id"], "needs_review")
            wait_task(client, middle["id"], "succeeded")
            wait_task(client, last["id"], "succeeded")
            assert [work.task_id for work in sender.calls] == [task["id"] for task in (first, first, middle, last)]
            return

        try:
            assert priority_entered.wait(5)
            original = wait_task(client, first["id"], "paused" if pause_first else "queued")
            assert original["progress"]["done"] == 1
            assert deliveries(client, first["id"])["items"][0]["attempts"][0]["outcome"] == outcome
            project = client.get(f"{API}/projects/{first['project_id']}").json()
            assert not project["activity"]["running"]
            assert project["send_queue"] == (None if pause_first else {"position": 2, "total": 2})
        finally:
            priority_release.set()
        wait_task(client, last["id"], "succeeded")
        wait_task(client, middle["id"], "succeeded")
        if pause_first:
            assert wait_task(client, first["id"], "paused")["progress"]["done"] == 1
            assert [work.task_id for work in sender.calls] == [task["id"] for task in (first, last, middle)]
            return
        finished = wait_task(client, first["id"], "succeeded")
        assert finished["progress"]["done"] == 2
        assert finished["progress"]["failed"] == 0
        expected = [first, last, middle, first]
        if outcome == "rejected":
            expected.append(first)
        assert [work.task_id for work in sender.calls] == [task["id"] for task in expected]


@pytest.mark.parametrize("pause_first", [False, True])
def test_send_priority_during_cooldown_preserves_account_deadline(tmp_path, pause_first):
    sender = ScriptedSender()
    items = [send_source("合成弹幕甲", 1137, 0), send_source("合成弹幕乙", 1144, 1)]
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        assert client.put(f"{API}/settings/sending", headers=headers, json={
            "item_min_seconds": 60, "item_max_seconds": 60, "rate_limit_seconds": 60,
        }).status_code == 200
        runs = []
        for _ in range(2):
            project, run = configure_live_run(client, headers)
            revision = choose(client, headers, project, run, [(0, True), (1, True)])
            runs.append((project, run, revision))

        first = submit(client, headers, *runs[0], **CONFIRM).json()
        cooling = wait_task(client, first["id"], "backoff")
        assert cooling["progress"]["done"] == 1
        if pause_first:
            assert control(client, headers, cooling, "pause").json()["state"] == "paused"
        last = submit(client, headers, *runs[1], **CONFIRM).json()
        assert client.get(f"{API}/send-queue/active").json() == first["id"]
        response = client.post(f"{API}/tasks/{last['id']}/send-priority", headers=headers, json={
            "expected_version": last["version"],
        })
        assert response.status_code == 202
        queued = wait_task(client, first["id"], "paused" if pause_first else "queued")
        assert queued["checkpoint"]["waiting_after_priority"]
        promoted = wait_task(client, last["id"], "backoff")
        assert queued["next_attempt_at"] == promoted["next_attempt_at"] == cooling["next_attempt_at"]
        assert [work.task_id for work in sender.calls] == [first["id"]]


@pytest.mark.parametrize("outcome", ["confirmed", "rejected", "unknown"])
def test_cancel_delivery_releases_queue_and_deduplicates_only_confirmed(tmp_path, outcome):
    entered, release = Event(), Event()

    async def hold_send():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome=outcome, receipt="76001" if outcome == "confirmed" else None)

    items = [send_source("合成弹幕甲", 1137, 0), send_source("合成弹幕乙", 1144, 1)]
    sender = ScriptedSender(hold_send)
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        queued_project, queued_run = configure_live_run(client, headers)
        queued_revision = choose(client, headers, queued_project, queued_run, [(0, True), (1, True)])
        inputs_url = f"{API}/projects/{project}/inputs"
        assert client.get(inputs_url).json()["send_in_queue"] is False
        send = partial(submit, client, headers, project, run, revision, **CONFIRM)
        keys = ("first-a", "first-b")
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(send, keys))
        for index, reply in enumerate(replies):
            if reply.status_code == 503:
                assert reply.json()["error"]["code"] == "database_busy"
                replies[index] = send(keys[index])
        assert sorted(reply.status_code for reply in replies) == [202, 409]
        created = next(reply for reply in replies if reply.status_code == 202)
        first = created.json()["id"]
        try:
            assert entered.wait(5)
            preview_url = f"{API}/projects/{project}/send-preview"
            assert client.get(preview_url, params={"run_id": run}).json()["duplicate_count"] == 0
            assert client.get(inputs_url).json()["send_in_queue"] is True
            denied = next(reply for reply in replies if reply.status_code == 409)
            assert denied.json()["error"]["code"] == "send_active"
            assert send(created.request.headers["Idempotency-Key"]).json()["id"] == first
            queue = partial(submit, client, headers, queued_project, queued_run, queued_revision, **CONFIRM)
            queued = queue("cancel-queued").json()
            assert client.get(f"{API}/projects/{queued_project}/inputs").json()["send_in_queue"] is True
            assert queue("queued-conflict").json()["error"]["code"] == "send_active"
            assert client.post(f"{API}/tasks/{queued['id']}/send-priority", headers=headers, json={
                "expected_version": queued["version"],
            }).status_code == 202
            queued = wait_task(client, queued["id"], "queued")
            cancelled = control(client, headers, queued, "send-cancel")
            assert cancelled.status_code == 202
            assert cancelled.json()["state"] == "cancelled"
            assert deliveries(client, queued["id"], status="withdrawn")["total_count"] == 2
            assert wait_task(client, first, "running")["checkpoint"]["next_send_task_id"] is None

            active = wait_task(client, first, "running")
            assert control(client, headers, {**active, "version": active["version"] - 1}, "send-cancel").status_code == 409
            cancelling = control(client, headers, active, "send-cancel")
            assert cancelling.status_code == 202
            assert cancelling.json()["state"] == "cancelling"
            assert control(client, headers, active, "send-cancel").json() == cancelling.json()
            assert deliveries(client, first, status="withdrawn")["total_count"] == 1
            assert send("next-batch").json()["error"]["code"] == "send_active"
        finally:
            release.set()

        cancelled = wait_task(client, first, "cancelled")
        assert cancelled["progress"]["withdrawn"] == (2 if outcome == "rejected" else 1)
        assert cancelled["progress"]["unknown"] == int(outcome == "unknown")
        assert control(client, headers, active, "send-cancel").json() == cancelled
        assert client.get(inputs_url).json()["send_in_queue"] is False
        successor = send("next-batch").json()
        wait_task(client, successor["id"], "succeeded")
        results = deliveries(client, successor["id"])["items"]
        assert [row["status"] for row in results] == [
            "skipped" if outcome == "confirmed" else "confirmed", "confirmed",
        ]
        assert len(sender.calls) == (2 if outcome == "confirmed" else 3)
        assert client.get(preview_url, params={"run_id": run}).json()["duplicate_count"] == 2


def test_withdraw_pending_and_resend_after_restart(tmp_path):
    entered, release = Event(), Event()

    async def hold_send():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome="confirmed", receipt="76001")

    items = [send_source("合成弹幕甲", 1137, 0), send_source("合成弹幕乙", 1144, 1)]
    sender = ScriptedSender(hold_send)
    secrets = MemoryCredentialStore()
    with live_client(tmp_path, items, live_sender=sender, credential_store=secrets) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        path = f"{API}/tasks/{task_id}/deliveries"
        try:
            assert entered.wait(5)
            ledger = deliveries(client, task_id, status="active")
            sending, pending = ledger["items"]
            assert [sending["status"], pending["status"]] == ["in_flight", "pending"]
            version = {"expected_version": ledger["task_version"]}
            assert client.post(f"{path}/{sending['id']}/withdraw", headers=headers, json=version).json()[
                "error"]["code"] == "delivery_withdraw_unavailable"
            assert client.post(f"{path}/{pending['id']}/withdraw", json=version).status_code == 403
            result = client.post(f"{path}/{pending['id']}/withdraw", headers=headers, json=version)
            assert result.status_code == 200
            assert result.json()["progress"] == {
                "done": 1, "total": 2, "failed": 0, "skipped": 0, "unknown": 0, "withdrawn": 1,
            }
            assert client.post(f"{path}/{pending['id']}/withdraw", headers=headers, json=version).json()[
                "error"]["code"] == "version_conflict"
            assert deliveries(client, task_id, status="active")["items"] == [sending]
            assert deliveries(client, task_id, status="withdrawn")["items"][0]["attempts"] == []
        finally:
            release.set()
        finished = wait_task(client, task_id, "succeeded")
        assert finished["progress"]["done"] == 2
        assert len(sender.calls) == 1
        assert client.get(f"{API}/projects/{project}").json()["completed"] is True
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run}).json()
        assert preview["duplicate_count"] == 1

    entered.clear()
    release.clear()
    sender = ScriptedSender(hold_send)
    with live_client(tmp_path, items, live_sender=sender, credential_store=secrets) as client:
        headers = connect_session(client)
        assert client.get(f"{API}/projects/{project}").json()["completed"] is True
        ledger = deliveries(client, task_id, status="withdrawn")
        assert ledger["items"] == [{**pending, "status": "withdrawn"}]
        resend = f"{path}/{pending['id']}/resend"
        version = {"expected_version": ledger["task_version"]}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: client.post(resend, headers=headers, json=version), range(2)))
            assert sorted(result.status_code for result in responses) == [202, 409]
            queued = next(result.json() for result in responses if result.status_code == 202)
            assert queued["id"] == task_id and queued["state"] == "queued"
            assert queued["progress"]["done"] == 1 and queued["progress"]["withdrawn"] == 0
            assert entered.wait(5)
            [resending] = deliveries(client, task_id, status="withdrawn")["items"]
            assert resending["status"] == "in_flight" and resending["resending"]
            assert deliveries(client, task_id, status="active")["items"] == [resending]
        finally:
            release.set()
        finished = wait_task(client, task_id, "succeeded")
        assert finished["progress"]["done"] == 2
        assert client.get(f"{API}/projects/{project}").json()["completed"] is True
        ledger = deliveries(client, task_id, status="withdrawn", track=[pending["id"]])
        assert ledger["items"] == [] and ledger["total_count"] == 0
        [sent] = ledger["tracked_items"]
        assert sent["status"] == "confirmed" and not sent["resending"]
        assert sent["item"] == pending["item"] and len(sender.calls) == len(sent["attempts"]) == 1
        assert sender.calls[0].target_cid == TARGET and sender.calls[0].account_id == ACCOUNT


@pytest.mark.parametrize(("origin", "passed", "limited"), [
    ("withdrawn", False, False), ("withdrawn", True, True), ("failed", True, False),
])
def test_resend_restores_delivery_order_and_repairs_legacy_effects(tmp_path, origin, passed, limited):
    entered = [Event(), Event()]
    released = [Event(), Event()]

    async def hold(index):
        entered[index].set()
        while not released[index].is_set():
            await asyncio.sleep(0.01)
        if index == 1 and limited:
            return SendResult(outcome="limited")
        return SendResult(outcome="confirmed", receipt=str(76001 + index))

    index = 1 if passed else 2
    items = [send_source(f"合成弹幕 {number}", 1000 + number * 100, number) for number in range(4)]
    if origin == "failed":
        items[index] = items[index].model_copy(update={"mode": 6, "text": '[0,0,"1-1",4.5,"合成弹幕"]'})
    sender = ScriptedSender(partial(hold, 0), partial(hold, 1))
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(number, True) for number in range(4)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        path = f"{API}/tasks/{task_id}/deliveries"
        delivery_id = f"{index + 1:012d}"
        try:
            assert entered[0].wait(5)
            if origin == "withdrawn":
                version = deliveries(client, task_id)["task_version"]
                withdrawn = client.post(f"{path}/{delivery_id}/withdraw", headers=headers,
                                        json={"expected_version": version})
                assert withdrawn.status_code == 200
            else:
                # Represent a ledger written before mode 7 and font-size normalization were supported.
                with client.app.state.tasks.database(write=True) as repository:
                    row = repository.delivery(task_id, delivery_id)
                    assert row.status == "failed"
                    row.item = row.item.model_copy(update={
                        "mode": 7, "font_size": 24,
                        "effect_differences": ("unsupported_mode", "unsupported_font_size"),
                    })
                    repository.save_delivery(task_id, row)
            if passed:
                released[0].set()
                assert entered[1].wait(5)
            version = deliveries(client, task_id)["task_version"]
            response = client.post(f"{path}/{delivery_id}/resend", headers=headers,
                                   json={"expected_version": version})
            assert response.status_code == 202
            assert response.json()["progress"][origin] == 0
            [resending] = deliveries(client, task_id, status=origin)["items"]
            assert resending["status"] == "pending" and resending["resending"]
            assert resending["item"]["font_size"] == 25
            assert resending["item"]["effect_differences"] == []
            expected = [2, 1, 3] if passed else [0, 1, 2, 3]
            active = deliveries(client, task_id, status="active", limit=1)
            active_rest = deliveries(client, task_id, status="active", cursor=active["next_cursor"])
            assert [int(row["id"]) - 1 for row in active["items"] + active_rest["items"]] == expected
            assert len(sender.calls) == (2 if passed else 1)
        finally:
            for event in released:
                event.set()
        assert wait_task(client, task_id, "succeeded")["progress"]["done"] == 4
        order = [0, 2, 2, 1, 3] if limited else [0, 2, 1, 3] if passed else [0, 1, 2, 3]
        assert [work.item.text for work in sender.calls] == [items[number].text for number in order]
        assert deliveries(client, task_id, status=origin)["total_count"] == 0


def test_withdraw_last_waiting_delivery_finishes_and_resend_honors_account_delay(tmp_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("danmaku.main.Tasks", partial(Tasks, clock=lambda: now))
    sender = ScriptedSender()
    items = [send_source("合成弹幕甲", 1137, 0), send_source("合成弹幕乙", 1144, 1)]
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        assert client.put(f"{API}/settings/sending", headers=headers, json={
            "item_min_seconds": 11, "item_max_seconds": 11, "rate_limit_seconds": 0,
        }).status_code == 200
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, False)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        wait_task(client, first, "succeeded")
        revision = choose(client, headers, project, run, [(0, False), (1, True)])
        second = submit(client, headers, project, run, revision, key="second", **CONFIRM).json()
        second = wait_task(client, second["id"], "backoff")
        ledger = deliveries(client, second["id"], status="active")
        [pending] = ledger["items"]
        path = f"{API}/tasks/{second['id']}/deliveries/{pending['id']}"
        result = client.post(f"{path}/withdraw", headers=headers, json={"expected_version": second["version"]})
        assert result.status_code == 200 and result.json()["state"] == "succeeded"
        assert client.get(f"{API}/projects/{project}").json()["completed"] is True
        result = client.post(f"{path}/resend", headers=headers, json={"expected_version": result.json()["version"]})
        assert result.status_code == 202 and result.json()["state"] == "queued"
        [resending] = deliveries(client, second["id"], status="withdrawn")["items"]
        assert resending["status"] == "pending" and resending["resending"]
        waiting = wait_task(client, second["id"], "backoff")
        assert datetime.fromisoformat(waiting["next_attempt_at"]) == now + timedelta(seconds=11)
        now += timedelta(seconds=11)
        assert wait_task(client, second["id"], "succeeded")["progress"]["done"] == 1
        assert [work.task_id for work in sender.calls] == [first, second["id"]]


def test_freezes_review_and_preserves_display_attributes(tmp_path):
    entered, release = Event(), Event()

    async def first_attempt():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome="confirmed", receipt="76001")

    sender = ScriptedSender(first_attempt, "rejected", "rejected")
    items = [
        send_source("第一条合成弹幕", 1137, 0).model_copy(update={"color": 0x1A2B3C, "font_size": 18, "mode": 5}),
        send_source("未选合成弹幕", 1144, 1),
        send_source("效果不支持", 1200, 2).model_copy(update={"color": "special", "mode": 6}),
        send_source("明确拒绝", 1200, 3).model_copy(update={"color": 0, "font_size": 36, "mode": 4}),
    ]
    sessions = LocalSessions()
    client = live_client(tmp_path, items, live_sender=sender, sessions=sessions)
    with client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, False), (2, True), (3, True)])
        with client.app.state.tasks.database() as repository:
            target = repository.video_parts(project).target

        preview_url = f"{API}/projects/{project}/send-preview"
        preview = client.get(preview_url, params={"run_id": run, "limit": 1}).json()
        assert preview["target"] == target.model_dump()
        assert preview["source_parts"] == [preview["source"]]
        assert preview["outside_count"] == 0 and preview["selected_count"] == 3 and preview["unsupported_count"] == 1
        assert preview["cues"][0]["plaintext"] == "你已经知道了？"
        assert preview["items"][0]["time_ms"] == 1137
        assert submit(client, headers, project, run, revision - 1, **CONFIRM).json()["error"]["code"] == "revision_conflict"
        assert submit(client, headers, project, run, revision, mode="live").status_code == 422
        other_project = create_project(client, headers, name="另一合成项目")
        assert submit(client, headers, other_project, run, revision, **CONFIRM).status_code == 404
        assert submit(client, {"Origin": headers["Origin"]}, project, run, revision, **CONFIRM).status_code == 403
        created = submit(client, headers, project, run, revision, **CONFIRM)
        assert created.status_code == 202, created.text
        task = created.json()
        task_id = task["id"]
        assert created.headers["Location"] == f"{API}/tasks/{task_id}"
        assert task["kind"] == "send" and task["checkpoint"] == {
            "batch_id": task_id, "started": False, "next_send_task_id": None, "waiting_after_priority": False,
            "accounts": {}, "priority_ready": False, "primary_only": False,
            "secondary_upgrade_id": None, "share_count": 0, "automatic_retry_started": False,
            "publication": None,
            "publication_first_pending": False,
            "publication_retry_at": None,
        }
        assert entered.wait(3)
        first_page = deliveries(client, task_id, limit=1)
        assert first_page["items"][0]["status"] == "in_flight"
        assert first_page["items"][0]["attempts"][0]["outcome"] == "in_flight"
        second_page = deliveries(client, task_id, cursor=first_page["next_cursor"], limit=1)
        assert second_page["items"][0]["item"]["text"] == "效果不支持"
        assert client.get(f"{API}/tasks/{task_id}/deliveries", params={
            "cursor": first_page["next_cursor"], "status": "skipped",
        }).status_code == 400

        updated = choose(client, headers, project, run, [(0, False), (1, True), (3, False)])
        assert submit(client, headers, project, run, revision, **CONFIRM).json()["id"] == task_id
        assert submit(client, headers, project, run, updated, **CONFIRM).json()["error"]["code"] == "idempotency_conflict"
        assert client.get(preview_url, params={"run_id": run, "cursor": preview["next_cursor"]}).status_code == 400
        running = client.get(f"{API}/tasks/{task_id}").json()
        assert control(client, headers, running, "pause").json()["state"] == "pausing"
        release.set()
        paused = wait_task(client, task_id, "paused")
        assert len(sender.calls) == 1 and paused["progress"]["done"] == 2
        assert submit(client, headers, project, run, updated, key="paused-conflict", **CONFIRM).json()["error"]["code"] == "send_active"
        project_url = f"{API}/projects/{project}"
        completion_url = f"{project_url}/completion"
        assert client.get(project_url).json()["activity"]["active"] is False
        marked = client.patch(completion_url, headers=headers, json={"completed": True})
        assert marked.status_code == 200 and marked.json()["completed"] is True
        assert client.patch(completion_url, headers=headers, json={"completed": False}).json()["completed"] is False
        assert control(client, headers, paused, "resume").status_code == 202
        finished = wait_task(client, task_id, "failed")
        assert finished["progress"] == {"done": 3, "total": 3, "failed": 2, "skipped": 0, "unknown": 0, "withdrawn": 0}
        assert [work.target_cid for work in sender.calls] == ["73", "73", "73"]
        assert [(work.item.text, work.item.time_ms, work.item.color_rgb, work.item.font_size, work.item.mode)
                for work in sender.calls] == [("第一条合成弹幕", 1137, 0x1A2B3C, 18, 5), ("明确拒绝", 1200, 0, 36, 4),
                                          ("明确拒绝", 1200, 0, 36, 4)]
        ledger = deliveries(client, task_id)
        assert ledger["batch"]["review_revision"] == revision and ledger["batch"]["mode"] == "live"
        confirmed, unsupported, rejected = ledger["items"]
        assert confirmed["receipt"].startswith("bilibili:") and confirmed["delivered_at"]
        assert confirmed["attempts"][0]["receipt"] == confirmed["receipt"]
        assert unsupported["status"] == "failed" and unsupported["attempts"] == []
        assert unsupported["item"]["effect_differences"] == ["unsupported_mode", "unsupported_color"]
        assert rejected["error_code"] == "delivery_rejected"
        assert rejected["attempts"][0]["resolution"] == "retry"
        status = client.get(project_url).json()
        assert status["send_partial_failure"] and not status["completed"] and not status["activity"]["active"]
        for completed in (True, False):
            updated = client.patch(completion_url, headers=headers, json={"completed": completed})
            assert updated.status_code == 200
            assert updated.json()["completed"] is completed
            assert updated.json()["send_partial_failure"] is True
            assert client.get(project_url).json()["completed"] is completed
        assert deliveries(client, task_id) == ledger
        events = event_frames(client.get(f"{API}/tasks/{task_id}/events"))
        assert [entry[0] for entry in events] == list(range(1, finished["version"] + 1))
        assert events[-1][2] == finished
        assert client.get(f"{API}/projects/{project}/tasks", params={"kind": "send"}).json()["items"] == [finished]

        sessions.sessions["synthetic-other"] = LocalSession(Principal(id="another-owner"), "csrf", float("inf"))
        client.cookies.clear()
        client.cookies.set("danmaku_session", "synthetic-other")
        assert client.get(f"{API}/tasks/{task_id}/deliveries").status_code == 404
        assert client.get(preview_url, params={"run_id": run}).status_code == 404
        assert resolve(client, {**headers, "X-CSRF-Token": "csrf"}, finished, confirmed, "skip").status_code == 404


def test_upgrade_reclassifies_legacy_preflight_skips(tmp_path):
    invalid = send_source("unsupported", 1000, 0).model_copy(update={"mode": 6})
    sender = ScriptedSender()
    with live_client(tmp_path, [invalid], live_sender=sender) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        failed = wait_task(client, task_id, "failed")

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
        connection.execute("UPDATE schema_version SET version = 23")
        connection.execute(
            "UPDATE send_deliveries SET status = 'skipped', data = json_set(data, '$.status', 'skipped') "
            "WHERE batch_id = ?", (task_id,),
        )
        connection.execute(
            "UPDATE tasks SET state = 'succeeded', error = NULL, "
            "progress = json_set(progress, '$.failed', 0, '$.skipped', 1) WHERE id = ?", (task_id,),
        )

    with live_client(tmp_path, [invalid], live_sender=sender) as client:
        connect_session(client)
        task = client.get(f"{API}/tasks/{task_id}").json()
        assert task["state"] == "failed" and task["version"] == failed["version"] + 1
        assert task["progress"] == failed["progress"]
        assert client.get(f"{API}/projects/{project}").json()["send_partial_failure"]
        assert deliveries(client, task_id, status="skipped")["total_count"] == 0
        assert deliveries(client, task_id, status="failed")["total_count"] == 1
        assert not sender.calls


def test_outside_items_are_listed_and_block_creation(tmp_path):
    sender = ScriptedSender()
    items = [send_source("超出片长", 12_000, 0), send_source("合成有效条目", 1000, 1)]
    client = live_client(tmp_path, items, live_sender=sender)
    with client:
        headers = connect_session(client)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run}).json()
        assert preview["selected_count"] == 2 and preview["outside_count"] == 1
        assert [item["time_ms"] for item in preview["items"]] == [12_000]
        assert submit(client, headers, project, run, revision, **CONFIRM).json()["error"]["code"] == "outside_video"
        revision = choose(client, headers, project, run, [(0, False), (1, False)])
        assert submit(client, headers, project, run, revision, **CONFIRM).json()["error"]["code"] == "empty_selection"
        assert not sender.calls


def test_real_snapshot_requires_a_resolved_platform_target(tmp_path):
    sender = ScriptedSender()
    client = live_client(tmp_path, [send_source("合成条目", 1000, 0)], live_sender=sender)
    with client:
        headers = connect_session(client)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        with client.app.state.tasks.database(write=True) as repository:
            record = repository.run(project, run)
            snapshot = repository.snapshot(project, record.snapshot_id)
            snapshot.mode = "bilibili"
            repository.save_snapshot(snapshot)
            repository.save_video_parts(project, ProjectParts(
                mode="mock", source=snapshot.part, target=synthetic_part("BV0000000001", 2),
            ))
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run})
        assert preview.status_code == 409 and preview.json()["error"]["code"] == "target_part_missing"
        assert submit(client, headers, project, run, revision, **CONFIRM).json()["error"]["code"] == "target_part_missing"
        assert not sender.calls


def test_v9_upgrade_preserves_historical_mock_batches_and_releases_queue(tmp_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("danmaku.main.Tasks", partial(Tasks, clock=lambda: now))
    sender = ScriptedSender()
    store = MemoryCredentialStore()
    with live_client(tmp_path, [send_source("合成条目", 2000, 0)], live_sender=sender, credential_store=store) as client:
        headers = connect_session(client)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        historical = seed_legacy_send(client, project, run, revision)
        old = seed_legacy_send(client, project, run, revision, task_id="synthetic-paused-batch", state="paused")
        assert submit(client, headers, project, run, revision, mode="mock").status_code == 422

    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE send_queue")
        connection.execute("UPDATE schema_version SET version=9")
        connection.execute("UPDATE tasks SET inputs=json_remove(inputs, '$.mode') WHERE id=?", (old,))

    with open_client(tmp_path, live_sender=sender, credential_store=store) as client:
        headers = connect_session(client)
        retired = client.get(f"{API}/tasks/{old}").json()
        assert retired["state"] == "failed" and retired["error"]["code"] == "send_mode_removed"
        assert control(client, headers, retired, "resume").status_code == 409
        assert deliveries(client, old)["items"][0]["attempts"] == []
        previous = deliveries(client, historical)
        assert previous["batch"]["mode"] == "mock" and previous["items"][0]["receipt"] == "mock:synthetic-receipt"
        real = submit(client, headers, project, run, revision, **CONFIRM)
        assert real.status_code == 202
        wait_task(client, real.json()["id"], "succeeded")
        assert [call.task_id for call in sender.calls] == [real.json()["id"]]
    assert database_path.with_suffix(".sqlite3.v9.bak").is_file()


def test_safe_rate_limit_retries_have_a_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("danmaku.application.sending.service.LIVE_FAILURE_COOLDOWN_SECONDS", 0)
    limited = SendResult(outcome="limited", retry_after_seconds=0)
    sender = ScriptedSender(*([limited] * 8))
    client = live_client(tmp_path, [send_source("合成条目", 2000, 0)], live_sender=sender)
    with client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        task = wait_task(client, task_id, "failed")
        assert task["progress"]["failed"] == task["progress"]["done"] == 1
        row = deliveries(client, task_id, status="failed")["items"][0]
        assert row["error_code"] == "send_rate_limited"
        assert len(row["attempts"]) == len(sender.calls) == 8


@pytest.mark.parametrize("outcome", ["confirmed", "skip", "retry"])
def test_timeout_allows_following_items_and_requires_versioned_reconciliation(tmp_path, monkeypatch, outcome):
    async def timeout():
        await asyncio.Event().wait()

    monkeypatch.setattr("danmaku.application.sending.service.SEND_TIMEOUT_SECONDS", 0.05)
    sender = ScriptedSender(timeout)
    items = [send_source("合成条目甲", 2000, 0), send_source("合成条目乙", 3000, 1)]
    client = live_client(tmp_path, items, live_sender=sender)
    with client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        review = wait_task(client, task_id, "needs_review")
        unknown = deliveries(client, task_id, status="unknown")["items"][0]
        assert review["progress"]["unknown"] == 1 and review["progress"]["done"] == 1
        assert unknown["attempts"][0]["outcome"] == "unknown"
        project_status = client.get(f"{API}/projects/{project}").json()
        assert project_status["send_needs_review"] and not project_status["completed"]
        assert not project_status["activity"]["running"]
        assert client.get(f"{API}/projects/{project}/inputs").json()["send_in_queue"] is False
        assert [call.item.text for call in sender.calls] == ["合成条目甲", "合成条目乙"]
        assert resolve(client, {}, review, unknown, outcome).status_code == 403
        barrier = Barrier(2)

        def reconcile(_):
            barrier.wait(timeout=3)
            return resolve(client, headers, review, unknown, outcome)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(reconcile, range(2)))
        assert sorted(response.status_code for response in responses) == [200, 409]
        settled = next(response.json() for response in responses if response.status_code == 200)
        assert settled["status"] == {"confirmed": "confirmed", "skip": "skipped", "retry": "pending"}[outcome]
        assert settled["attempts"][0]["resolution"] == outcome
        assert settled["attempts"][0]["resolved_by"] and settled["attempts"][0]["resolved_at"]
        assert settled["receipt"] is None
        done = wait_task(client, task_id, "succeeded")
        assert done["progress"]["unknown"] == 0 and done["progress"]["done"] == 2
        assert done["progress"]["skipped"] == int(outcome == "skip")
        expected = ["合成条目甲", "合成条目乙"]
        if outcome == "retry":
            expected.append("合成条目甲")
        assert [call.item.text for call in sender.calls] == expected
        project_status = client.get(f"{API}/projects/{project}").json()
        assert project_status["completed"] and not project_status["send_needs_review"]


def test_manual_confirmation_prioritizes_projects_waiting_batch(tmp_path):
    entered, release = Event(), Event()

    async def hold_send():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SendResult(outcome="confirmed", receipt="76002")

    sender = ScriptedSender("unknown", hold_send)
    items = [send_source("合成条目甲", 2000, 0), send_source("合成条目乙", 3000, 1)]
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, False)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()
        review = wait_task(client, first["id"], "needs_review")
        unknown = deliveries(client, first["id"], status="unknown")["items"][0]

        head_project, head_run = configure_live_run(client, headers)
        head_revision = choose(client, headers, head_project, head_run, [(0, True), (1, True)])
        head = submit(client, headers, head_project, head_run, head_revision, **CONFIRM).json()
        try:
            assert entered.wait(5)
            revision = choose(client, headers, project, run, [(0, False), (1, True)])
            response = submit(client, headers, project, run, revision, key="after-review", **CONFIRM)
            assert response.status_code == 202, response.text
            waiting = response.json()
            detail = client.get(f"{API}/projects/{project}").json()
            assert detail["send_needs_review"] and not detail["completed"]
            assert resolve(client, headers, review, unknown, "confirmed").status_code == 200
            current = wait_task(client, head["id"], "running")
            assert current["checkpoint"]["next_send_task_id"] == waiting["id"]
            detail = client.get(f"{API}/projects/{project}").json()
            assert not detail["send_needs_review"] and not detail["completed"]
        finally:
            release.set()
        wait_task(client, waiting["id"], "succeeded")
        wait_task(client, head["id"], "succeeded")
        assert [call.task_id for call in sender.calls] == [
            first["id"], head["id"], waiting["id"], head["id"],
        ]
        assert client.get(f"{API}/projects/{project}").json()["completed"]


@pytest.mark.parametrize("cancel", [False, True])
def test_restart_after_adapter_acceptance_does_not_replay_unknown(tmp_path, cancel):
    accepted = Event()

    async def lost_receipt():
        accepted.set()
        await asyncio.Event().wait()

    sender = ScriptedSender("confirmed", lost_receipt)
    client = live_client(tmp_path, [send_source("甲", 1000, 0), send_source("乙", 2000, 1), send_source("丙", 3000, 2)],
                                    live_sender=sender)
    with client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True), (2, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        assert accepted.wait(3)
        assert [row["status"] for row in deliveries(client, task_id)["items"]] == ["confirmed", "in_flight", "pending"]
        if cancel:
            active = wait_task(client, task_id, "running")
            assert control(client, headers, active, "send-cancel").json()["state"] == "cancelling"
    # Lifespan cancellation stops the worker before it can commit a response.
    for restart in range(2):
        client = open_client(tmp_path, live_sender=sender)
        with client:
            headers = connect_session(client)
            state = "cancelled" if cancel else "paused"
            paused = wait_task(client, task_id, state)
            assert paused["progress"] == {
                "done": 1 + int(cancel), "total": 3, "failed": 0, "skipped": 0, "unknown": 1, "withdrawn": int(cancel),
            }
            assert [call.item.text for call in sender.calls] == ["甲", "乙"]
            unknown = deliveries(client, task_id, status="unknown")["items"][0]
            assert len(unknown["attempts"]) == 1 and unknown["attempts"][0]["outcome"] == "unknown"
            if restart == 1:
                if cancel:
                    assert resolve(client, headers, paused, unknown, "confirmed").status_code == 200
                    resolved = wait_task(client, task_id, "cancelled")
                    assert resolved["progress"]["done"] == 3 and resolved["progress"]["unknown"] == 0
                    assert [call.item.text for call in sender.calls] == ["甲", "乙"]
                else:
                    assert control(client, headers, paused, "resume").status_code == 202
                    review = wait_task(client, task_id, "needs_review")
                    assert review["progress"]["unknown"] == 1
                    assert resolve(client, headers, review, unknown, "confirmed").status_code == 200
                    assert wait_task(client, task_id, "succeeded")["progress"]["done"] == 3
                    assert [call.item.text for call in sender.calls] == ["甲", "乙", "丙"]


def test_v18_upgrade_recovers_pending_items_skipped_by_old_reservations(tmp_path):
    entered = Event()

    async def hold_send():
        entered.set()
        await asyncio.Event().wait()

    items = [send_source("合成弹幕甲", 1000, 0), send_source("合成弹幕乙", 2000, 1)]
    with live_client(tmp_path, items, live_sender=ScriptedSender("confirmed", hold_send)) as client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        assert entered.wait(5)
        # Seed the overlapping batch that version 18 allowed for this project.
        queued = "synthetic-v18-queued"
        with client.app.state.tasks.database(write=True) as repository:
            original = repository.task(first)
            task = original.task.model_copy(update={
                "id": queued, "creation_order": 0, "version": 1, "state": "queued",
                "checkpoint": SendCheckpoint(batch_id=queued),
                "progress": TaskProgress(total=2, done=1, skipped=1),
            })
            batch = repository.send_batch(first).model_copy(update={"id": queued})
            rows = [
                Delivery(id=row.id, item=row.item, status="skipped" if row.status == "confirmed" else "pending",
                         error_code="duplicate_live_delivery" if row.status == "confirmed" else None)
                for row in repository.deliveries(first, None, "", 2)
            ]
            repository.save_task(original.model_copy(update={"task": task, "idempotency_key": queued}))
            repository.add_send_batch(batch, rows)
        first_rows = deliveries(client, first)["items"]
        assert [row["status"] for row in first_rows] == ["confirmed", "in_flight"]
        pending, = deliveries(client, queued, status="pending")["items"]

    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO live_send_keys VALUES (?, ?, ?)", (project, TARGET, "id:2001"))
        connection.execute(
            """UPDATE send_deliveries SET status = 'skipped',
                   data = json_set(data, '$.status', 'skipped', '$.error_code', 'duplicate_live_delivery')
               WHERE batch_id = ? AND id = ?""", (queued, pending["id"]),
        )
        connection.execute("UPDATE tasks SET progress = json_set(progress, '$.done', 2, '$.skipped', 2) WHERE id = ?",
                           (queued,))
        connection.execute("UPDATE schema_version SET version = 18")

    with open_client(tmp_path) as client:
        connect_session(client)
        paused = wait_task(client, queued, "paused")
        assert paused["progress"]["done"] == paused["progress"]["skipped"] == 1
        assert deliveries(client, queued, status="pending")["items"] == [pending]
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run}).json()
        assert preview["duplicate_count"] == 1


def test_v4_upgrade_preserves_review_and_creates_live_ledger(tmp_path):
    sender = ScriptedSender()
    store = MemoryCredentialStore()
    client = live_client(tmp_path, [send_source("合成条目", 2000, 0)], live_sender=sender, credential_store=store)
    with client:
        headers = connect_session(client)
        project, run = configure_live_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        for table in ("send_attempts", "send_deliveries", "send_batches"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_version SET version=4")

    client = open_client(tmp_path, live_sender=sender, credential_store=store)
    with client:
        headers = connect_session(client)
        created = submit(client, headers, project, run, revision, **CONFIRM)
        assert created.status_code == 202, created.text
        wait_task(client, created.json()["id"], "succeeded")
    with sqlite3.connect(path.with_name(path.name + ".v4.bak")) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (4,)
        assert backup.execute("SELECT count(*) FROM review_decisions").fetchone() == (1,)


def test_review_offsets_persist_and_control_delivery_time(tmp_path):
    items = [send_source(f"合成弹幕{i}", timestamp, i) for i, timestamp in enumerate((1000, 2000, 2000))]
    sender = ScriptedSender()
    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        project, run = configure_live_run(client, headers)
        path = f"{API}/projects/{project}"
        endpoint = f"{path}/review"
        revision = choose(client, headers, project, run, [(0, True), (1, True), (2, True)])
        rows = get_review(client, endpoint, run, q="合成弹幕1")["items"]
        assert len(rows) == 1 and rows[0]["offset_ms"] == 0
        command = {"run_id": run, "expected_revision": revision, "item_id": rows[0]["item_id"],
                   "offset_ms": -2100, "apply_to_following": True}
        invalid = client.patch(f"{endpoint}/offset", headers=headers, json={**command, "offset_ms": 1.5})
        assert invalid.status_code == 422
        applied = client.patch(f"{endpoint}/offset", headers=headers, json=command)
        assert applied.json() == {"revision": revision + 1, "changed_count": 2, "selected_count": 3}
        assert client.patch(f"{endpoint}/offset", headers=headers, json=command).status_code == 409
        preview = client.get(f"{path}/send-preview", params={"run_id": run}).json()
        assert preview["outside_count"] == 2
        assert [item["time_ms"] for item in preview["items"]] == [-100, -100]
        blocked = submit(client, headers, project, run, revision + 1, key="outside", **CONFIRM)
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "outside_video"

        command.update(expected_revision=revision + 1, offset_ms=-1500)
        applied = client.patch(f"{endpoint}/offset", headers=headers, json=command)
        assert applied.json()["changed_count"] == 2
        command.update(expected_revision=revision + 2, offset_ms=125, apply_to_following=False)
        applied = client.patch(f"{endpoint}/offset", headers=headers, json=command)
        assert applied.json()["changed_count"] == 1
        assert client.patch(f"{endpoint}/offset", headers=headers, json={
            **command, "expected_revision": revision + 3,
        }).json()["changed_count"] == 0

    with live_client(tmp_path, items, live_sender=sender) as client:
        headers = connect_session(client)
        rows = get_review(client, endpoint, run)["items"]
        assert [(row["time_ms"], row["offset_ms"]) for row in rows] == [(1000, 0), (2000, 125), (2000, -1500)]
        preview = client.get(f"{path}/send-preview", params={"run_id": run}).json()
        assert [item["time_ms"] for item in preview["items"]] == [500, 1000, 2125]
        no_send_delay(client, headers)
        response = submit(client, headers, project, run, revision + 3, **CONFIRM)
        assert response.status_code == 202, response.text
        wait_task(client, response.json()["id"], "succeeded")
        assert [work.item.time_ms for work in sender.calls] == [500, 1000, 2125]
