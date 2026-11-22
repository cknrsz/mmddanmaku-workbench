import asyncio
from functools import partial
from datetime import UTC, datetime
from danmaku.application.tasks.service import Tasks
import sqlite3
from threading import Event
from time import time

import httpx
import pytest

from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.application.sending.publication import Preload
from danmaku.application.sessions import LOCAL_OWNER_ID
from danmaku.domain.sending import SendResult
from danmaku.domain.workbench import Principal
from danmaku.persistence.database import Database, SCHEMA_VERSION
from test_live_sending import ACCOUNT, CREDENTIAL, FakeSource, account_settings, choose, sample
from test_sending import ScriptedSender, no_send_delay, submit
from test_workbench import API, connect_session, open_client, wait_task

BVIDS = ("BV0000000010", "BV0000000011", "BV0000000012")
ACTOR = Principal(id=LOCAL_OWNER_ID)


class Platform:
    def __init__(self, clock):
        self.times = {bvid: clock + 120 for bvid in BVIDS}
        self.public = set()
        self.probes = []
        self.detail_checks = 0
        self.logged_in = True
        self.public_http_error = False

    def rows(self):
        for index, (bvid, timestamp) in enumerate(self.times.items(), 1):
            yield {"Archive": {"bvid": bvid, "aid": index, "dtime": timestamp,
                               "title": f"合成稿件 {index}", "state": -40},
                   "Videos": [{"cid": index * 100 + page, "index": page, "duration": 15,
                               "title": f"合成分 P{page}"} for page in (1, 2)]}

    def __call__(self, request):
        path = request.url.path
        if path == "/x/web-interface/nav":
            if not self.logged_in:
                return httpx.Response(200, json={"code": -101})
            return self.ok({"isLogin": True, "mid": int(ACCOUNT)})
        if path == "/x/web-interface/view":
            assert "cookie" not in request.headers
            bvid = request.url.params["bvid"]
            self.probes.append(bvid)
            if self.public_http_error:
                return httpx.Response(503)
            if bvid not in self.public:
                return httpx.Response(200, json={"code": -404})
            row = next(row for row in self.rows() if row["Archive"]["bvid"] == bvid)
            return self.ok({"bvid": bvid, "state": 0,
                            "pages": [{"page": part["index"], "cid": part["cid"]} for part in row["Videos"]]})
        assert request.url.host == "member.bilibili.com"
        assert "SESSDATA=synthetic" in request.headers["Cookie"]
        if path == "/x/web/archives":
            assert request.url.params["status"] == "is_pubing,pubed,not_pubed"
            rows = list(self.rows())
            for row in rows:
                if row["Archive"]["bvid"] == BVIDS[2]:
                    del row["Videos"]
            return self.ok({"arc_audits": rows + [{"Archive": {"state": -30}}]})
        assert path == "/x/vupre/web/archive/view"
        self.detail_checks += 1
        row = next(row for row in self.rows() if row["Archive"]["bvid"] == request.url.params["bvid"])
        return self.ok({"archive": row["Archive"], "videos": row["Videos"]})

    @staticmethod
    def ok(data):
        return httpx.Response(200, json={"code": 0, "data": data})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    class Clock:
        now = int(time())

    clock = Clock()
    monkeypatch.setattr("danmaku.main.Preload", partial(Preload, clock=lambda: clock.now))
    monkeypatch.setattr("danmaku.main.Tasks", partial(Tasks, clock=lambda: datetime.fromtimestamp(clock.now, UTC)))
    monkeypatch.setattr("danmaku.application.projects.time", lambda: clock.now)
    platform = Platform(clock.now)
    credentials = MemoryCredentialStore()
    background = Event()
    run = Preload.run

    async def scheduler(*args):
        if background.is_set():
            await run(*args)
        else:
            await asyncio.Future()

    # Drive the publication scheduler deterministically. The real delivery worker still runs.
    monkeypatch.setattr(Preload, "run", scheduler)

    def client(sender):
        return open_client(tmp_path, bilibili_transport=httpx.MockTransport(platform), credential_store=credentials,
                           live_source=FakeSource([sample(0), sample(1)]), live_sender=sender)

    return clock, platform, client, background


def create(client, headers, bvid=BVIDS[0], page=1):
    response = client.post(f"{API}/preload/projects", json={"bvid": bvid, "page": page}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def stage(client, headers, project, target_cid, count=1):
    response = client.post(f"{API}/projects/{project}/tasks", json={
        "kind": "download", "mode": "bilibili", "source_bvid": "BV0000000020", "source_page": 1,
    }, headers=headers | {"Idempotency-Key": "source"})
    assert response.status_code == 202, response.text
    snapshot = wait_task(client, response.json()["id"], "succeeded")
    response = client.post(f"{API}/projects/{project}/tasks", json={
        "kind": "classify", "snapshot_id": snapshot["id"], "skip_analysis": True,
    }, headers=headers | {"Idempotency-Key": "analysis"})
    assert response.status_code == 202, response.text
    run = response.json()["id"]
    revision = choose(client, headers, project, run, [(0, True), (1, count == 2)])
    confirmation = {"account_id": ACCOUNT, "target_cid": str(target_cid)}
    response = submit(client, headers, project, run, revision, mode="live", confirmation=confirmation)
    assert response.status_code == 202, response.text
    return response.json(), (project, run, revision, confirmation)


def initialize(client):
    headers = connect_session(client)
    assert account_settings(client, headers, CREDENTIAL).status_code == 200
    no_send_delay(client, headers)
    return headers


def check(client):
    client.portal.call(client.app.state.preload.check_next, ACTOR)


def status(client, project):
    return client.get(f"{API}/projects/{project}").json()["publication_status"]


def test_catalog_marks_existing_parts_and_rejects_duplicate_create(setup):
    clock, platform, open_test_client, _ = setup
    with open_test_client(ScriptedSender()) as client:
        headers = initialize(client)
        url = f"{API}/preload/parts"
        assert len(client.get(url).json()) == 6
        project = create(client, headers, page=2)
        entries = client.get(url).json()
        assert [(item["part"]["bvid"], item["part"]["page"]) for item in entries if item["already_created"]] == [(BVIDS[0], 2)]
        assert client.get(f"{API}/projects/{project}").json()["preload_publish_at"] == clock.now + 120
        duplicate = client.post(f"{API}/preload/projects", json={"bvid": BVIDS[0], "page": 2}, headers=headers)
        assert (duplicate.status_code, duplicate.json()["error"]["message"]) == (409, "项目已创建")
        platform.times.clear()
        assert client.get(url).json() == []


@pytest.mark.parametrize("outcome", ["blocked", "rejected", "limited"])
def test_schedule_change_dispatch_and_first_delivery_retry(setup, outcome):
    clock, platform, open_test_client, _ = setup
    result = SendResult(outcome=outcome, error_code="bilibili_access_denied" if outcome == "blocked" else None)
    sender = ScriptedSender(result)
    with open_test_client(sender) as client:
        headers = initialize(client)
        project = create(client, headers)
        task, _ = stage(client, headers, project, 101)
        original = platform.times[BVIDS[0]]
        platform.times[BVIDS[0]] += 60
        clock.now = original - 11
        check(client)
        assert platform.detail_checks == 0
        clock.now += 1
        check(client)
        assert platform.detail_checks == 1
        assert client.get(f"{API}/projects/{project}").json()["preload_publish_at"] == original + 60
        clock.now = original + 50
        check(client)
        assert platform.detail_checks == 2
        clock.now = original + 64
        check(client)
        assert not sender.calls
        clock.now += 1
        check(client)
        retry = wait_task(client, task["id"], "backoff")
        deadline = int(datetime.fromisoformat(retry["next_attempt_at"]).timestamp())
        if outcome == "limited":
            assert deadline != clock.now + 60
            assert retry["checkpoint"]["publication_retry_at"] is None
        else:
            assert deadline == clock.now + 60
        clock.now = deadline - 1
        check(client)
        assert len(sender.calls) == 1
        clock.now = deadline
        wait_task(client, task["id"], "succeeded")
        assert [work.delivery_id for work in sender.calls] == [sender.calls[0].delivery_id] * 2
        assert not platform.probes


def test_exhausted_first_delivery_returns_batch_to_manual(setup):
    clock, platform, open_test_client, _ = setup
    blocked = SendResult(outcome="blocked", error_code="bilibili_access_denied")
    sender = ScriptedSender(blocked, "rejected", blocked)
    with open_test_client(sender) as client:
        headers = initialize(client)
        task, (project, run, revision, confirmation) = stage(client, headers, create(client, headers), 101, count=2)
        clock.now = platform.times[BVIDS[0]] + 5
        check(client)
        check(client)
        version = 0
        for attempts in (1, 2):
            waiting = wait_task(client, task["id"], "backoff", after_version=version)
            version = waiting["version"]
            assert int(datetime.fromisoformat(waiting["next_attempt_at"]).timestamp()) == clock.now + 60
            clock.now += 59
            check(client)
            assert len(sender.calls) == attempts
            clock.now += 1
        final = wait_task(client, task["id"], "cancelled")
        assert final["checkpoint"]["publication"] == "returned"
        assert final["progress"]["withdrawn"] == 2
        assert len(sender.calls) == 3
        assert len({work.delivery_id for work in sender.calls}) == 1
        assert not client.get(f"{API}/projects/{project}/inputs").json()["send_in_queue"]
        manual = submit(client, headers, project, run, revision, key="manual", mode="live", confirmation=confirmation)
        assert manual.status_code == 202
        wait_task(client, manual.json()["id"], "succeeded")


@pytest.mark.parametrize("send", [True, False])
def test_missed_publications_wait_for_decision_and_keep_schedule_order(setup, send):
    clock, platform, open_test_client, _ = setup
    sender = ScriptedSender()
    # The earliest publication is created second, and shares its time with the third project.
    platform.times[BVIDS[0]] += 40
    with open_test_client(sender) as client:
        headers = initialize(client)
        projects = [create(client, headers, bvid) for bvid in BVIDS]
        tasks = [stage(client, headers, project, (index + 1) * 100 + 1)[0]
                 for index, project in enumerate(projects)]
        assert client.get(f"{API}/preload/overdue").json() is None
    clock.now = platform.times[BVIDS[1]] + 1
    platform.public.update(BVIDS)
    with open_test_client(sender) as client:
        headers = connect_session(client)
        overdue_url = f"{API}/preload/overdue"
        assert client.get(overdue_url).json() == tasks[1]["id"]
        assert status(client, projects[1]) == "blocked"
        assert status(client, projects[0]) == "waiting"
        check(client)
        assert not sender.calls
        if send:
            platform.logged_in = False
            decision = {"task_id": tasks[1]["id"], "send": True}
            assert client.post(overdue_url, json=decision, headers=headers).json()["error"]["code"] == "bilibili_credential_rejected"
            assert client.get(overdue_url).json() == tasks[1]["id"]
            platform.logged_in = True
        assert client.post(overdue_url, json={"task_id": tasks[1]["id"], "send": send},
                           headers=headers).status_code == 204
        assert client.get(overdue_url).json() is None
        if send:
            for index in (1, 2, 0):
                clock.now = max(clock.now, platform.times[BVIDS[index]] + 5)
                check(client)
                check(client)
                wait_task(client, tasks[index]["id"], "succeeded")
            assert [work.task_id for work in sender.calls] == [tasks[i]["id"] for i in (1, 2, 0)]
        else:
            for task in tasks:
                final = wait_task(client, task["id"], "cancelled")
                assert final["checkpoint"]["publication"] == "returned"
            assert not sender.calls


def test_scheduled_inserts_at_delivery_boundary_and_manual_priority_requires_confirmation(setup):
    clock, platform, open_test_client, _ = setup
    ordinary_started, release_ordinary = Event(), Event()
    scheduled_started, release_scheduled = Event(), Event()

    async def block(started, release):
        started.set()
        assert await asyncio.to_thread(release.wait, 15)
        return SendResult(outcome="confirmed", receipt="76001")

    sender = ScriptedSender(partial(block, ordinary_started, release_ordinary),
                            partial(block, scheduled_started, release_scheduled))
    with open_test_client(sender) as client:
        headers = initialize(client)
        scheduled = create(client, headers)
        planned, _ = stage(client, headers, scheduled, 101)
        other_plan, _ = stage(client, headers, create(client, headers, BVIDS[1]), 201)
        normal = client.post(f"{API}/projects?source=bilibili", headers=headers, json={
            "target_bvid": "BV0000000021", "target_page": 1,
        }).json()["id"]
        ordinary, _ = stage(client, headers, normal, 72001, count=2)
        assert ordinary_started.wait(5)
        try:
            clock.now = platform.times[BVIDS[0]] + 5
            check(client)
            check(client)
            head = client.get(f"{API}/tasks/{ordinary['id']}").json()
            assert head["checkpoint"]["next_send_task_id"] == planned["id"]
            assert status(client, scheduled) == "blocked"
            release_ordinary.set()
            assert scheduled_started.wait(5)
            check(client)
            assert not platform.probes
            queued = client.get(f"{API}/tasks/{ordinary['id']}").json()
            priority = f"{API}/tasks/{ordinary['id']}/send-priority"
            body = {"expected_version": queued["version"]}
            warning = client.post(priority, json=body, headers=headers)
            assert warning.status_code == 409 and warning.json()["error"]["code"] == "preload_priority_confirmation"
            assert client.post(priority, json=body | {"confirmed_preload_task_id": planned["id"]},
                               headers=headers).status_code == 202
        finally:
            release_ordinary.set()
            release_scheduled.set()
        wait_task(client, ordinary["id"], "succeeded")
        wait_task(client, planned["id"], "succeeded")
        check(client)
        wait_task(client, other_plan["id"], "succeeded")
        assert [work.task_id for work in sender.calls] == [
            ordinary["id"], planned["id"], ordinary["id"], other_plan["id"],
        ]


def test_backend_dispatches_due_job_without_an_open_ui(setup):
    clock, platform, open_test_client, background = setup
    sender = ScriptedSender()
    background.set()
    with open_test_client(sender) as client:
        headers = initialize(client)
        task, _ = stage(client, headers, create(client, headers), 101)
        platform.public.add(BVIDS[0])
        clock.now = platform.times[BVIDS[0]] + 5
        wait_task(client, task["id"], "succeeded")
        assert [work.task_id for work in sender.calls] == [task["id"]]


def test_checked_schedule_survives_restart(setup):
    clock, platform, open_test_client, _ = setup
    sender = ScriptedSender()
    with open_test_client(sender) as client:
        headers = initialize(client)
        task, _ = stage(client, headers, create(client, headers), 101)
        clock.now = platform.times[BVIDS[0]] - 10
        check(client)
        assert platform.detail_checks == 1
    with open_test_client(sender) as client:
        connect_session(client)
        assert client.get(f"{API}/preload/overdue").json() is None
        clock.now += 15
        check(client)
        wait_task(client, task["id"], "succeeded")
        assert platform.detail_checks == 1
        assert not platform.probes


def test_publication_schema_upgrade_preserves_projects(tmp_path):
    path = tmp_path / "workbench.sqlite3"
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        project = client.post(f"{API}/projects", json={"target_bvid": BVIDS[0], "target_page": 1},
                              headers=headers).json()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE publications")
        connection.execute("UPDATE schema_version SET version=24")
    database = Database(path)
    try:
        database.initialize()
        with database() as repository:
            assert repository.project(ACTOR.id, project["id"]).target_bvid == BVIDS[0]
            assert repository.publication(project["id"]) is None
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
        assert path.with_name("workbench.sqlite3.v24.bak").is_file()
    finally:
        database.close()
