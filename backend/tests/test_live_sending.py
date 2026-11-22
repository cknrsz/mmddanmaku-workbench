import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
import sqlite3
from threading import Event
from time import monotonic, sleep
from urllib.parse import parse_qs

import httpx
import pytest

from danmaku.adapters import bilibili_sender
from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.application.sending.service import assign_accounts
from danmaku.application.sessions import LOCAL_OWNER_ID
from danmaku.application.tasks.service import Tasks
from danmaku.domain.bilibili_login import BilibiliLoginSecret, BilibiliProfile
from danmaku.domain.inputs import SourceBatch, SourceItem, VideoPart
from danmaku.domain.sending import Attempt, BilibiliCredential, LiveSendCooldown, SendItem, SendResult, SendWork
from danmaku.persistence.database import Database
from danmaku.persistence.repository import ProjectSendShareRow, TaskEventRow
from test_analysis import app_with_inputs, classify, upload_subtitle
from test_review import get_review, patch_review
from test_sending import make_run as make_mock_run
from test_sending import control, deliveries, no_send_delay, resolve, seed_legacy_send, submit
from test_workbench import API, PROJECT, connect_session, event_frames, open_client, wait_task

ACCOUNT = "17001"
CID = "72002"
CREDENTIAL = {"sessdata": "synthetic-session-value", "bili_jct": "synthetic-csrf-value"}
SECONDARY = "17002"
SECONDARY_CREDENTIAL = {"sessdata": "synthetic-second-session", "bili_jct": "synthetic-second-csrf"}
CONFIRM = {"mode": "live", "confirmation": {"account_id": ACCOUNT, "target_cid": CID}}
NOTICE = "站内转载，因此会将之前的弹幕一并搬运过来（不含弹幕翻译）"
ADVANCED_RECEIPT = "900000000000000001"


def sample(index, **changes):
    return SourceItem(platform_id=str(2000 + index), text=f"合成条目 {index}", time_ms=1137 + index,
                      color=0xABC123, font_size=18, mode=5).model_copy(update=changes)


class FakeSource:
    def __init__(self, items):
        self.items = items

    async def parts(self, bvid):
        return [VideoPart(bvid=bvid, page=page, cid=cid, duration_ms=15_000, title=f"合成分 P{page}")
                for page, cid in ((1, "72001"), (2, CID))]

    async def fetch(self, part, segment, count):
        return SourceBatch(part=part, segment=segment, segment_count=count, items=self.items)


@pytest.fixture
def live_clock(monkeypatch):
    class Clock:
        now = datetime(2026, 1, 1, tzinfo=UTC)

        def advance(self, seconds):
            self.now += timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr("danmaku.main.Tasks", partial(Tasks, clock=lambda: clock.now))
    return clock


def wait_for_live_cooldown(client, task_id, now):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        task = client.get(f"{API}/tasks/{task_id}").json()
        if task["state"] == "backoff" and datetime.fromisoformat(task["next_attempt_at"]) > now:
            return task
        sleep(0.01)
    raise AssertionError(f"Task did not schedule a future send: {task}")


def make_run(client, headers, *, project=None, suffix="first"):
    endpoint = f"{API}/projects"
    if project is None:
        created = client.post(f"{endpoint}?source=bilibili", json=PROJECT, headers=headers)
        assert created.status_code == 201
        project = created.json()["id"]
    path = f"{endpoint}/{project}"
    downloaded = client.post(f"{path}/tasks", json={"kind": "download", "mode": "bilibili"},
                             headers=headers | {"Idempotency-Key": f"download-{suffix}"})
    snapshot = wait_task(client, downloaded.json()["id"], "succeeded")
    chinese = upload_subtitle(client, project, headers, [(0, 3, "合成对白")], "chinese.srt")
    japanese = upload_subtitle(client, project, headers, [(0, 3, "テスト用の台詞")], "japanese.srt")
    run = classify(client, project, {
        "kind": "classify", "snapshot_id": snapshot["id"], "llm_config_version": 0,
        "subtitle_version_id": chinese, "japanese_subtitle_version_id": japanese,
    }, headers, key=f"analysis-{suffix}")
    assert run.status_code == 202
    return project, wait_task(client, run.json()["id"], "succeeded")["id"]


def account_settings(client, headers, credential=CREDENTIAL):
    return client.put(f"{API}/settings/bilibili", headers=headers,
                      json={"credential": credential})


def save_secondary(client, level):
    client.app.state.bilibili_settings.save_scanned(
        LOCAL_OWNER_ID, BilibiliLoginSecret(**SECONDARY_CREDENTIAL),
        BilibiliProfile(account_id=SECONDARY, level=level), "secondary",
    )


def enable_dual(client, headers):
    response = client.put(f"{API}/settings", headers=headers, json={"sending": {"dual_account_enabled": True}})
    assert response.status_code == 200


def choose(client, headers, project, run, choices):
    endpoint = f"{API}/projects/{project}/review"
    review = get_review(client, endpoint, run)
    revision = review["revision"]
    ordinary = [item for item in review["items"] if item["checkbox_type"] == 0]
    updates = [(ordinary[index]["item_id"], selected) for index, selected in choices]
    updates.extend((item["item_id"], False) for item in review["items"]
                   if item["checkbox_type"] == 4 and item["selected"])
    for item_id, selected in updates:
        response = patch_review(client, endpoint, run, revision, [item_id], selected, headers)
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
    return revision


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(bilibili_sender, "_aid", lambda _: 9001)
    calls = []

    def handle(request):
        assert request.url.host == "api.bilibili.com"
        if request.method == "GET":
            assert request.url.path == "/x/web-interface/nav"
            account = ACCOUNT if "synthetic-second-session" not in request.headers["cookie"] else "17002"
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": int(account)}})
        assert request.method == "POST" and request.url.path == "/x/v2/dm/post"
        calls.append(parse_qs(request.content.decode()))
        if calls[-1]["mode"] == ["7"]:
            return httpx.Response(200, json={"code": 0, "data": {
                "action": "", "animation": "", "colorful_src": None, "dm_content": "",
                "dmid": int(ADVANCED_RECEIPT), "dmid_str": ADVANCED_RECEIPT, "visible": True,
            }})
        return httpx.Response(200, json={"code": 0, "data": {"dmid_str": "76001"}})

    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize("pause_manually", [False, True])
@pytest.mark.parametrize("request_started", [False, True])
def test_offline_send_head_waits_for_reconnection(tmp_path, transport, monkeypatch, pause_manually, request_started):
    gateway, calls = transport
    offline = Event()
    checked_while_offline = Event()
    post_count = 0
    original_send = bilibili_sender.BilibiliSender.send

    async def probe():
        if offline.is_set():
            checked_while_offline.set()
            return False
        return True

    async def disconnect_on_first_post(request):
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            if post_count == 1 and request_started:
                offline.set()
                raise httpx.ReadError("synthetic disconnect", request=request)
        return await gateway.handle_async_request(request)

    async def disconnect_before_post(sender, work):
        if not checked_while_offline.is_set():
            offline.set()
            while sender.gateway.connection.connected is not False:
                await asyncio.sleep(0.01)
        return await original_send(sender, work)

    if not request_started:
        monkeypatch.setattr(bilibili_sender.BilibiliSender, "send", disconnect_before_post)

    options = dict(live_source=FakeSource([sample(0), sample(1), sample(2)]),
                   bilibili_transport=httpx.MockTransport(disconnect_on_first_post),
                   credential_store=MemoryCredentialStore(), connection_probe=probe)
    with open_client(tmp_path, **options) as client:
        endpoint = f"{API}/send-queue/connection"
        assert client.get(endpoint).status_code == 401
        headers = connect_session(client)
        no_send_delay(client, headers)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True), (2, True)])
        response = submit(client, headers, project, run, revision, **CONFIRM)
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]

        assert checked_while_offline.wait(3)
        assert client.get(endpoint).json() is False
        task = wait_task(client, task_id, "backoff")
        assert task["progress"]["unknown"] == int(request_started) and post_count == int(request_started)
        rows = deliveries(client, task_id)["items"]
        assert [row["status"] for row in rows] == [
            "unknown" if request_started else "pending", "pending", "pending",
        ]
        assert len(rows[0]["attempts"]) == int(request_started)
        if pause_manually:
            assert control(client, headers, task, "pause").status_code == 202

        offline.clear()
        deadline = monotonic() + 3
        while client.get(endpoint).json() is not True and monotonic() < deadline:
            sleep(0.01)
        assert client.get(endpoint).json() is True
        if pause_manually:
            paused = wait_task(client, task_id, "paused")
            assert post_count == int(request_started)
            assert control(client, headers, paused, "resume").status_code == 202

        task = wait_task(client, task_id, "needs_review" if request_started else "succeeded")
        assert task["progress"]["done"] == 3 - int(request_started)
        assert task["progress"]["unknown"] == int(request_started)
        assert post_count == 3 and len(calls) == 3 - int(request_started)


def test_repost_notice_review_and_live_delivery(tmp_path, transport):
    gateway, calls = transport
    client = open_client(tmp_path, live_source=FakeSource([]), bilibili_transport=gateway,
                         credential_store=MemoryCredentialStore())
    with client:
        headers = connect_session(client)
        no_send_delay(client, headers)
        project, run = make_run(client, headers)
        path = f"{API}/projects/{project}"
        review = f"{path}/review"
        page = get_review(client, review, run, selected=True, q=NOTICE)
        notice, = page["items"]
        assert page["selected_count"] == 1
        assert (notice["text"], notice["time_ms"], notice["mode"], notice["checkbox_type"]) == (NOTICE, 500, 5, 4)
        assert notice["selected"] and notice["classification"] is None

        deselected = patch_review(client, review, run, page["revision"], [notice["item_id"]], False, headers)
        assert deselected.status_code == 200 and deselected.json()["selected_count"] == 0
        page = get_review(client, review, run, selected=False)
        assert page["items"][0]["selection_source"] == "manual"
        selected = patch_review(client, review, run, page["revision"], [notice["item_id"]], True, headers)
        revision = selected.json()["revision"]
        preview = client.get(f"{path}/send-preview", params={"run_id": run, "mode": "live"}).json()
        assert preview["selected_count"] == 1 and preview["unstable_count"] == 0
        assert preview["items"][0]["checkbox_type"] == 4

        assert account_settings(client, headers).status_code == 200
        batch = submit(client, headers, project, run, revision, **CONFIRM)
        batch_id = batch.json()["id"]
        wait_task(client, batch_id, "succeeded")
        assert deliveries(client, batch_id)["items"][0]["item"]["checkbox_type"] == 4
        request, = calls
        assert (request["msg"], request["progress"], request["mode"], request["checkbox_type"]) == (
            [NOTICE], ["500"], ["5"], ["4"],
        )

        _, next_run = make_run(client, headers, project=project, suffix="notice-repeat")
        duplicate = submit(client, headers, project, next_run, revision, key="notice-repeat", **CONFIRM)
        duplicate_id = duplicate.json()["id"]
        wait_task(client, duplicate_id, "succeeded")
        assert deliveries(client, duplicate_id)["items"][0]["error_code"] == "duplicate_live_delivery"
        assert len(calls) == 1


@pytest.mark.parametrize("downloaded_notice", [False, True])
def test_repost_notice_backfills_saved_snapshots_without_duplicating_downloaded_text(tmp_path, downloaded_notice):
    items = [sample(0, text=NOTICE)] if downloaded_notice else []
    client = open_client(tmp_path, live_source=FakeSource(items))
    with client:
        headers = connect_session(client)
        project, run = make_run(client, headers)
        initial = get_review(client, f"{API}/projects/{project}/review", run, q=NOTICE)
        assert len(initial["items"]) == 1 and initial["items"][0]["checkbox_type"] == (0 if downloaded_notice else 4)

    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM source_items WHERE source_key = ?", ("notice:repost",))
        connection.execute("UPDATE schema_version SET version = 8")

    with open_client(tmp_path) as client:
        connect_session(client)
        page = get_review(client, f"{API}/projects/{project}/review", run, q=NOTICE)
        assert page == initial
    assert database_path.with_suffix(".sqlite3.v8.bak").is_file()


def test_startup_check_validates_saved_bilibili_account_without_changing_settings(tmp_path):
    calls = []
    logged_in = {"code": 0, "data": {"isLogin": True, "mid": int(ACCOUNT)}}
    payload = logged_in

    def handle(request):
        assert request.method == "GET" and request.url == bilibili_sender.NAV_URL
        assert f"SESSDATA={CREDENTIAL['sessdata']}" in request.headers["cookie"]
        calls.append(request.method)
        if payload is None:
            raise httpx.ConnectError("synthetic outage", request=request)
        return httpx.Response(200, json=payload)

    store = MemoryCredentialStore()
    endpoint = f"{API}/settings/bilibili/check-credential"
    with open_client(tmp_path, credential_store=store, bilibili_transport=httpx.MockTransport(handle)) as client:
        assert client.post(endpoint).status_code == 401
        headers = connect_session(client)
        assert client.post(endpoint, headers={"Origin": headers["Origin"]}).status_code == 403
        assert client.post(endpoint, headers=headers).json() == {"status": "unconfigured"}
        assert not calls

        saved = account_settings(client, headers)
        assert saved.status_code == 200
        assert client.post(endpoint, headers=headers).json() == {"status": "valid"}

        for payload in (
            {"code": -101},
            {"code": 0, "data": {"isLogin": False}},
            {"code": 0, "data": {"isLogin": True, "mid": 17002}},
        ):
            assert client.post(endpoint, headers=headers).json() == {"status": "renewal_required"}

        for payload, status, error in (
            (None, 503, "bilibili_unavailable"),
            ({"code": 0}, 503, "bilibili_response_invalid"),
            ({"code": -412}, 403, "bilibili_access_denied"),
        ):
            response = client.post(endpoint, headers=headers)
            assert response.status_code == status
            assert response.json()["error"]["code"] == error

        assert client.get(f"{API}/settings/bilibili").json() == saved.json()
        assert len(calls) == 8
        with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
            reference = connection.execute("SELECT json_extract(data, '$.secret_ref') FROM bilibili_accounts").fetchone()[0]
        store.delete(reference)
        assert client.post(endpoint, headers=headers).json() == {"status": "renewal_required"}
        assert len(calls) == 8


def test_live_advanced_danmaku_preserves_payload_and_confirms_receipt(tmp_path, transport):
    gateway, calls = transport
    payload = '[0.5, 0.5, "1-1", 4.5, "合成高级弹幕", 0, 0, 0, 0, 500, 0, true, "sans-serif", 1]'
    source = FakeSource([sample(0, mode=7, text=payload)])
    with open_client(tmp_path, live_source=source, bilibili_transport=gateway,
                     credential_store=MemoryCredentialStore()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        no_send_delay(client, headers)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run}).json()
        assert preview["unsupported_count"] == 0
        assert preview["items"][0]["text"] == payload

        response = submit(client, headers, project, run, revision, **CONFIRM)
        assert response.status_code == 202
        task_id = response.json()["id"]
        wait_task(client, task_id, "succeeded")
        [delivery] = deliveries(client, task_id)["items"]
        assert delivery["status"] == "confirmed"
        assert delivery["receipt"] == f"bilibili:{ADVANCED_RECEIPT}"
        [request] = calls
        assert request["mode"] == ["7"]
        assert request["msg"] == [payload]


def test_explicit_live_confirmation_effects_and_cross_snapshot_deduplication(tmp_path, transport):
    gateway, calls = transport
    store = MemoryCredentialStore()
    items = [sample(0, font_size=23), sample(1, mode=6, font_size=24, color="special"),
             sample(2, platform_id=None), sample(3)]
    client = open_client(tmp_path, live_source=FakeSource(items), bilibili_transport=gateway, credential_store=store)
    with client:
        headers = connect_session(client)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True), (2, True), (3, False)])
        capabilities = client.get(f"{API}/send-capabilities").json()
        assert set(capabilities) == {"live"}
        assert capabilities["live"]["modes"] == [1, 4, 5, 7]
        assert capabilities["live"]["font_sizes"] == [18, 25, 36] and not capabilities["live"]["platform_verified"]
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run, "mode": "live"}).json()
        assert preview["live_target"] and preview["unsupported_count"] == preview["unstable_count"] == 1
        assert preview["items"][0]["font_size"] == 25 and not preview["items"][0]["effect_differences"]
        assert submit(client, headers, project, run, revision, mode="live").status_code == 422
        unbound = submit(client, headers, project, run, revision, **CONFIRM)
        assert unbound.json()["error"]["code"] == "bilibili_credential_missing"
        saved = account_settings(client, headers)
        assert saved.json() == {"configured": True, "version": 1, "account_id": ACCOUNT}
        assert all(secret not in saved.text for secret in CREDENTIAL.values())
        for field, value, code in (("target_cid", "99", "target_confirmation_mismatch"),
                                   ("account_id", "99", "bilibili_account_changed")):
            wrong = CONFIRM["confirmation"] | {field: value}
            response = submit(client, headers, project, run, revision, mode="live", confirmation=wrong)
            assert response.status_code == 409 and response.json()["error"]["code"] == code
        with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
            connection.execute("UPDATE bilibili_accounts SET data=json_set(data, '$.enabled', json('false'))")
        assert submit(client, headers, project, run, revision, mode="mock").status_code == 422
        assert client.get(f"{API}/projects/{project}/send-preview",
                          params={"run_id": run, "mode": "mock"}).status_code == 422
        no_send_delay(client, headers)
        assert calls == []

        created = submit(client, headers, project, run, revision, **CONFIRM)
        assert created.status_code == 202
        task_id = created.json()["id"]
        assert wait_task(client, task_id, "failed")["progress"]["failed"] == 2
        page = deliveries(client, task_id)
        assert page["batch"]["account_id"] == ACCOUNT and page["batch"]["mode"] == "live"
        assert page["items"][0]["status"] == "confirmed" and page["items"][0]["receipt"] == "bilibili:76001"
        assert [row["status"] for row in page["items"][1:]] == ["failed", "failed"]
        assert [row["error_code"] for row in page["items"][1:]] == ["unsupported_effect", "unstable_source"]
        assert len(calls) == 1
        request = calls[0]
        assert request == {key: [value] for key, value in {
            "type": "1", "oid": CID, "aid": "9001", "bvid": PROJECT["target_bvid"], "msg": items[0].text,
            "progress": "1137", "color": str(0xABC123), "fontsize": "25", "pool": "0", "mode": "5", "plat": "1",
            "csrf": CREDENTIAL["bili_jct"], "csrf_token": CREDENTIAL["bili_jct"],
        }.items()}
        _, next_run = make_run(client, headers, project=project, suffix="next")
        revision = choose(client, headers, project, next_run, [(0, True), (1, False), (2, False), (3, False)])
        duplicate = submit(client, headers, project, next_run, revision, key="new-snapshot", **CONFIRM).json()
        wait_task(client, duplicate["id"], "succeeded")
        assert deliveries(client, duplicate["id"])["items"][0]["error_code"] == "duplicate_live_delivery"
        assert len(calls) == 1

    with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
        sql = "\n".join(connection.iterdump())
        assert all(secret not in sql for secret in CREDENTIAL.values())


@pytest.mark.parametrize(("level", "secondary_ids"), [
    (0, []), (1, ["id:2", "id:6"]), (2, ["id:2", "id:3", "id:5"]), (3, ["id:2", "id:4", "id:6"]),
])
def test_assign_accounts(level, secondary_ids):
    base = SendItem(item_id="id:0", text="合成条目", time_ms=0, color_rgb=0xFFFFFF, font_size=25, mode=1)
    items = [base.model_copy(update={"item_id": f"id:{index}", **changes}) for index, changes in enumerate([
        {"checkbox_type": 4}, {"mode": 5}, {}, {"color_rgb": 123}, {"mode": 4},
        {"color_rgb": 456}, {}, {"mode": 6, "effect_differences": ("unsupported_mode",)}, {},
    ])]
    assigned = assign_accounts(items, {"id:8"}, level)
    assert [row.item_id for row in assigned if row.account_role == "secondary"] == secondary_ids
    assert assigned[0].account_role == "primary"
    assert [row.item_id for row in assigned if row.account_role is None] == ["id:7", "id:8"]


@pytest.mark.parametrize("fallback", ["disable", "logout", None])
def test_dual_live_preview_freezes_roles_and_schedules_each_account(tmp_path, monkeypatch, live_clock, fallback):
    monkeypatch.setattr(bilibili_sender, "_aid", lambda _: 9001)
    primary_entered, release = Event(), Event()
    posts = []
    items = [sample(index, mode=1, color=0xFFFFFF) for index in range(5)]

    async def gateway(request):
        secondary = "SESSDATA=synthetic-second-session" in request.headers["cookie"]
        account = SECONDARY if secondary else ACCOUNT
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": int(account)}})
        if request.url.path == "/login/exit/v2":
            assert secondary
            return httpx.Response(200, json={"code": 0})
        payload = parse_qs(request.content.decode())
        assert payload["csrf"] == [(SECONDARY_CREDENTIAL if secondary else CREDENTIAL)["bili_jct"]]
        text = payload["msg"][0]
        posts.append((account, text))
        if account == ACCOUNT and text == items[0].text:
            primary_entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        if secondary and sum(uid == SECONDARY for uid, _ in posts) == 1:
            return httpx.Response(429, headers={"Retry-After": "11"})
        return httpx.Response(200, json={"code": 0, "data": {"dmid_str": str(76000 + len(posts))}})

    with open_client(tmp_path, live_source=FakeSource(items), bilibili_transport=httpx.MockTransport(gateway),
                     credential_store=MemoryCredentialStore()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        assert client.put(f"{API}/settings/sending", headers=headers, json={
            "item_min_seconds": 5, "item_max_seconds": 5, "rate_limit_seconds": 0,
        }).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(i, True) for i in range(len(items))])
        url = f"{API}/projects/{project}/send-preview"

        def preview(**params):
            response = client.get(url, params={"run_id": run, "limit": 2} | params)
            assert response.status_code == 200
            return response.json()

        save_secondary(client, 3)
        assert preview()["secondary_account_id"] is None
        enable_dual(client, headers)
        save_secondary(client, 0)
        assert {row["account_role"] for row in preview()["items"]} == {"primary"}
        save_secondary(client, 3)
        first = preview()
        second = preview(cursor=first["next_cursor"])
        assert [row["account_role"] for row in first["items"] + second["items"]] == ["primary", "secondary"] * 2
        confirmation = CONFIRM["confirmation"] | {key: first[key] for key in ("secondary_account_id", "secondary_level")}
        save_secondary(client, 2)
        stale = submit(client, headers, project, run, revision, mode="live", confirmation=confirmation)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "send_accounts_changed"
        save_secondary(client, 3)
        created = submit(client, headers, project, run, revision, mode="live", confirmation=confirmation)
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        try:
            assert primary_entered.wait(5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                active = client.get(f"{API}/tasks/{task_id}").json()
                states = active["checkpoint"]["accounts"]
                if states["secondary"]["state"] == "waiting":
                    break
                sleep(0.01)
            assert active["state"] == "running" and states["primary"]["state"] == "sending"
            assert states["secondary"]["error"]["code"] == "send_rate_limited"
        finally:
            release.set()
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        states = waiting["checkpoint"]["accounts"]
        assert datetime.fromisoformat(states["primary"]["next_attempt_at"]) == live_clock.now + timedelta(seconds=5)
        assert datetime.fromisoformat(states["secondary"]["next_attempt_at"]) == live_clock.now + timedelta(seconds=11)
        assert waiting["next_attempt_at"] == states["primary"]["next_attempt_at"]
        secondary_page = deliveries(client, task_id, status="active", account_role="secondary", limit=1)
        assert secondary_page["total_count"] == 2
        [pending] = secondary_page["items"]
        assert pending["item"]["text"] == items[1].text
        last_page = deliveries(client, task_id, account_role="secondary", status="active", limit=1, page=2)
        assert [row["item"]["text"] for row in last_page["items"]] == [items[3].text]
        cursor = secondary_page["next_cursor"]
        assert deliveries(client, task_id, status="active", account_role="secondary", cursor=cursor)["items"] == last_page["items"]
        rejected = client.get(f"{API}/tasks/{task_id}/deliveries", params={
            "status": "active", "account_role": "primary", "cursor": cursor,
        })
        assert rejected.status_code == 400
        primary_page = deliveries(client, task_id, status="confirmed", account_role="primary", track=pending["id"])
        assert primary_page["total_count"] == 1
        assert [row["id"] for row in primary_page["tracked_items"]] == [pending["id"]]
        if fallback == "disable":
            response = client.put(f"{API}/settings", headers=headers, json={"sending": {"dual_account_enabled": False}})
            assert response.status_code == 200
        elif fallback == "logout":
            response = client.delete(f"{API}/bilibili/login/?role=secondary", headers=headers)
            assert response.status_code == 204, response.text
        if fallback:
            switched = wait_task(client, task_id, "backoff", after_version=waiting["version"])
            assert switched["checkpoint"]["primary_only"]
            assert switched["checkpoint"]["accounts"]["secondary"]["state"] == "finished"
            assert deliveries(client, task_id, status="active", account_role="primary")["total_count"] == 4
            assert deliveries(client, task_id, status="active", account_role="secondary")["total_count"] == 0
            assert len(posts) == 2
            if fallback == "disable":
                enable_dual(client, headers)
            for index in (1, 2, 3, 4):
                live_clock.advance(5)
                if index != 4:
                    wait_for_live_cooldown(client, task_id, live_clock.now)
            wait_task(client, task_id, "succeeded")
            assert [post for post in posts if post[0] == ACCOUNT] == [(ACCOUNT, item.text) for item in items]
            assert sum(uid == SECONDARY for uid, _ in posts) == 1
            page = deliveries(client, task_id)
            assert [attempt["account_role"] for attempt in page["items"][1]["attempts"]] == ["secondary", "primary"]
            assert deliveries(client, task_id, status="confirmed", account_role="primary")["total_count"] == 5
            return
        live_clock.advance(5)
        wait_task(client, task_id, "backoff", after_version=waiting["version"])
        assert posts[-1] == (ACCOUNT, items[2].text) and sum(uid == SECONDARY for uid, _ in posts) == 1
        live_clock.advance(6)
        deadline = monotonic() + 5
        while monotonic() < deadline and len(posts) < 5:
            sleep(0.01)
        assert set(posts[3:]) == {(ACCOUNT, items[4].text), (SECONDARY, items[1].text)}
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        live_clock.advance(5)
        finished = wait_task(client, task_id, "succeeded")
        assert {state["state"] for state in finished["checkpoint"]["accounts"].values()} == {"finished"}
        page = deliveries(client, task_id)
        assert page["batch"]["secondary_account_id"] == SECONDARY
        assert [row["item"]["account_role"] for row in page["items"]] == ["primary", "secondary"] * 2 + ["primary"]
        assert all(row["status"] == "confirmed" for row in page["items"])
        assert posts[-1] == (SECONDARY, items[3].text)


@pytest.mark.parametrize("action", ["pause", "send-cancel", "restart", "secondary-rejected"])
def test_dual_send_drains_or_recovers_both_in_flight_items(tmp_path, transport, live_clock, action):
    gateway, _ = transport
    entered = {"primary": Event(), "secondary": Event()}
    release = {"primary": Event(), "secondary": Event()}
    calls = []

    class Sender:
        async def send(self, work):
            calls.append(work)
            entered[work.account_role].set()
            while not release[work.account_role].is_set():
                await asyncio.sleep(0.01)
            if action == "secondary-rejected" and work.account_role == "secondary":
                return SendResult(outcome="blocked", error_code="bilibili_credential_rejected")
            return SendResult(outcome="confirmed", receipt=str(76000 + int(work.delivery_id)))

    options = dict(live_source=FakeSource([sample(i) for i in range(4)]), bilibili_transport=gateway,
                   credential_store=MemoryCredentialStore())
    with open_client(tmp_path, live_sender=Sender(), **options) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        save_secondary(client, 3)
        enable_dual(client, headers)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(i, True) for i in range(4)])
        created = submit(client, headers, project, run, revision, **CONFIRM)
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        assert all(event.wait(5) for event in entered.values())
        if action == "secondary-rejected":
            release["secondary"].set()
            running = wait_task(client, task_id, "running")
            deadline = monotonic() + 5
            while not running["checkpoint"]["primary_only"] and monotonic() < deadline:
                running = wait_task(client, task_id, "running", after_version=running["version"])
            assert running["checkpoint"]["primary_only"]
            assert len(calls) == 2
            release["primary"].set()
            wait_for_live_cooldown(client, task_id, live_clock.now)
            for _ in range(2):
                live_clock.advance(7)
                wait_for_live_cooldown(client, task_id, live_clock.now)
            live_clock.advance(7)
            wait_task(client, task_id, "succeeded")
            assert [work.account_role for work in calls].count("secondary") == 1
            return
        if action != "restart":
            running = wait_task(client, task_id, "running")
            assert len(calls) == 2
            stopping_state = "pausing" if action == "pause" else "cancelling"
            stopping = control(client, headers, running, action).json()
            assert stopping["state"] == stopping_state
            release["primary"].set()
            settling = wait_task(client, task_id, stopping_state, after_version=stopping["version"])
            assert settling["progress"]["done"] == (1 if action == "pause" else 3)
            assert len(calls) == 2
            release["secondary"].set()
            stopped = wait_task(client, task_id, "paused" if action == "pause" else "cancelled")
            assert len(calls) == 2
            assert [row["status"] for row in deliveries(client, task_id)["items"]] == (
                ["confirmed"] * 2 + ["pending" if action == "pause" else "withdrawn"] * 2
            )
            if action == "pause":
                assert control(client, headers, stopped, "resume").status_code == 202
                live_clock.advance(7)
                wait_task(client, task_id, "succeeded")
                assert {work.account_role for work in calls[2:]} == {"primary", "secondary"}
    if action == "restart":
        with open_client(tmp_path, **options) as restarted:
            connect_session(restarted)
            recovered = wait_task(restarted, task_id, "paused")
            assert recovered["progress"]["unknown"] == 2
            assert [row["status"] for row in deliveries(restarted, task_id)["items"]] == ["unknown"] * 2 + ["pending"] * 2
            with restarted.app.state.tasks.database() as repository:
                assert all(repository.live_cooldown(uid).deadline > live_clock.now for uid in (ACCOUNT, SECONDARY))


@pytest.mark.parametrize("idle", ["primary", "secondary"])
def test_dual_share_repeats_and_survives_restart(tmp_path, transport, live_clock, idle):
    donor_entered, first_share_entered, release = Event(), Event(), Event()
    donor = "secondary" if idle == "primary" else "primary"
    held_index = 1 if donor == "secondary" else 0
    remaining_index = held_index + 6
    calls = []
    items = [sample(i, mode=1, color=0xFFFFFF) for i in range(8)]

    class Sender:
        async def send(self, work):
            calls.append(work)
            if work.account_role == donor:
                donor_entered.set()
                await asyncio.Future()
            if sum(entry.account_role == idle for entry in calls) == 6:
                first_share_entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return SendResult(outcome="confirmed", receipt=str(76000 + int(work.delivery_id)))

    options = dict(live_source=FakeSource(items), bilibili_transport=transport[0],
                   credential_store=MemoryCredentialStore())
    with open_client(tmp_path, live_sender=Sender(), **options) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        client.app.state.bilibili_settings.save_level(LOCAL_OWNER_ID, "primary", ACCOUNT, 3)
        no_send_delay(client, headers)
        save_secondary(client, 3)
        enable_dual(client, headers)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(i, True) for i in range(8)])
        with client.app.state.tasks.database(write=True) as repository:
            repository.session.add(ProjectSendShareRow(project_id=project))
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        assert donor_entered.wait(5) and first_share_entered.wait(5)
        task = client.get(f"{API}/tasks/{task_id}").json()
        assert task["checkpoint"]["share_count"] == 1
        rows = deliveries(client, task_id)["items"]
        assert rows[held_index]["status"] == "in_flight"
        assert rows[held_index]["attempts"][0]["account_role"] == donor
        assert [row["item"]["text"] for row in rows if row["item"]["account_role"] == donor] == [
            items[held_index].text, items[remaining_index].text,
        ]
        assert rows[remaining_index]["status"] == "pending"
        release.set()

        deadline = monotonic() + 5
        while monotonic() < deadline:
            task = client.get(f"{API}/tasks/{task_id}").json()
            if task["progress"]["done"] == 7:
                break
            sleep(0.01)
        assert task["progress"]["done"] == 7
        assert task["checkpoint"]["share_count"] == 2
        rows = deliveries(client, task_id)["items"]
        assert rows[held_index]["status"] == "in_flight"
        assert rows[held_index]["attempts"][0]["account_role"] == donor
        assert rows[remaining_index]["status"] == "confirmed"
        assert rows[remaining_index]["attempts"][0]["account_role"] == idle
        preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run, "limit": 8}).json()
        assert [row["account_role"] for row in preview["items"]] == [row["item"]["account_role"] for row in rows]

    with open_client(tmp_path, **options) as client:
        connect_session(client)
        task = wait_task(client, task_id, "needs_review")
        assert task["progress"]["unknown"] == 1
        assert task["checkpoint"]["share_count"] == 2
        assert deliveries(client, task_id)["items"][remaining_index]["attempts"][0]["account_role"] == idle


@pytest.mark.parametrize(("secondary_wait", "expected_role"), [(5, "secondary"), (10, "primary")])
def test_dual_share_last_pending_item_only_when_it_can_send_sooner(
    tmp_path, transport, live_clock, secondary_wait, expected_role,
):
    with open_client(tmp_path, live_source=FakeSource([sample(0, mode=1, color=0xFFFFFF)]),
                     bilibili_transport=transport[0], credential_store=MemoryCredentialStore()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        client.app.state.bilibili_settings.save_level(LOCAL_OWNER_ID, "primary", ACCOUNT, 3)
        save_secondary(client, 3)
        enable_dual(client, headers)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        with client.app.state.tasks.database(write=True) as repository:
            repository.save_live_cooldown(ACCOUNT, LiveSendCooldown(live_clock.now + timedelta(seconds=10)))
            repository.save_live_cooldown(SECONDARY, LiveSendCooldown(live_clock.now + timedelta(seconds=secondary_wait)))

        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        assert waiting["checkpoint"]["share_count"] == int(expected_role == "secondary")
        assert datetime.fromisoformat(waiting["next_attempt_at"]) == live_clock.now + timedelta(seconds=secondary_wait)
        live_clock.advance(secondary_wait)
        finished = wait_task(client, task_id, "succeeded")
        assert finished["checkpoint"]["share_count"] == waiting["checkpoint"]["share_count"]
        [sent] = deliveries(client, task_id)["items"]
        assert [attempt["account_role"] for attempt in sent["attempts"]] == [expected_role]


@pytest.mark.parametrize("restart", [False, True])
def test_dual_idle_account_starts_next_batch_after_its_cooldown(tmp_path, transport, live_clock, restart):
    head_entered, release_head, next_entered, release_next = (Event() for _ in range(4))
    calls = []
    items = [sample(0), sample(1), sample(2, mode=1, color=0xFFFFFF), sample(3, mode=1, color=0xFFFFFF)]

    class Sender:
        async def send(self, work):
            calls.append(work)
            if work.item.text == items[0].text:
                head_entered.set()
                while not release_head.is_set():
                    await asyncio.sleep(0.01)
            if work.item.text == items[3].text:
                next_entered.set()
                while not release_next.is_set():
                    await asyncio.sleep(0.01)
            return SendResult(outcome="confirmed", receipt=str(76000 + int(work.delivery_id)))

    options = dict(live_source=FakeSource(items), bilibili_transport=transport[0],
                   credential_store=MemoryCredentialStore())
    with open_client(tmp_path, live_sender=Sender(), **options) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        save_secondary(client, 1)
        enable_dual(client, headers)
        project, run = make_run(client, headers)
        following_project, following_run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(i, i < 3) for i in range(4)])
        following_revision = choose(client, headers, following_project, following_run, [(i, i in (1, 3)) for i in range(4)])
        head = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        assert head_entered.wait(5)
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                task = client.get(f"{API}/tasks/{head}").json()
                if task["checkpoint"]["accounts"]["secondary"]["state"] == "finished":
                    break
                sleep(0.01)
            assert task["progress"]["done"] == 1
            created = submit(client, headers, following_project, following_run, following_revision, **CONFIRM)
            assert created.status_code == 202, created.text
            following = created.json()["id"]
            assert client.get(f"{API}/tasks/{following}").json()["state"] == "queued"
            assert not next_entered.is_set()
            live_clock.advance(7)
            assert next_entered.wait(5)
            assert calls[-1].task_id == following and calls[-1].account_role == "secondary"
            head_rows = deliveries(client, head)["items"]
            assert [(row["status"], row["item"]["account_role"]) for row in head_rows] == [
                ("in_flight", "primary"), ("pending", "primary"), ("confirmed", "secondary"),
            ]
            if not restart:
                release_next.set()
                requeued = wait_task(client, following, "queued", after_version=1)
                assert requeued["checkpoint"]["started"] and requeued["progress"]["done"] == 1
        finally:
            if not restart:
                release_head.set()
                release_next.set()
        if not restart:
            wait_for_live_cooldown(client, head, live_clock.now)
            live_clock.advance(7)
            wait_task(client, head, "succeeded")
            wait_for_live_cooldown(client, following, live_clock.now)
            live_clock.advance(7)
            wait_task(client, following, "succeeded")
            assert [(work.task_id, work.account_role) for work in calls[2:]] == [
                (following, "secondary"), (head, "primary"), (following, "primary"),
            ]
            return

    with open_client(tmp_path, **options) as client:
        connect_session(client)
        for task_id, role in ((head, "primary"), (following, "secondary")):
            assert wait_task(client, task_id, "paused")["progress"]["unknown"] == 1
            [unknown] = deliveries(client, task_id, status="unknown")["items"]
            assert unknown["attempts"][0]["account_role"] == role


def test_primary_only_can_be_selected_at_submission_or_during_delivery(tmp_path, transport, live_clock):
    gateway, _ = transport
    entered = {role: Event() for role in ("primary", "secondary")}
    release = Event()
    calls = []

    class Sender:
        async def send(self, work):
            calls.append(work)
            entered[work.account_role].set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return SendResult(outcome="confirmed", receipt=str(78000 + int(work.delivery_id)))

    items = [sample(i, mode=1, color=0xFFFFFF) for i in range(4)]
    with open_client(tmp_path, live_source=FakeSource(items), bilibili_transport=gateway,
                     credential_store=MemoryCredentialStore(), live_sender=Sender()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        save_secondary(client, 3)
        enable_dual(client, headers)
        project, run = make_run(client, headers, suffix="manual-switch")
        revision = choose(client, headers, project, run, [(i, True) for i in range(len(items))])
        active = submit(client, headers, project, run, revision, **CONFIRM)
        assert active.status_code == 202, active.text
        task_id = active.json()["id"]
        try:
            assert all(event.wait(5) for event in entered.values())
            queued_project, queued_run = make_run(client, headers, suffix="primary-choice")
            queued_revision = choose(client, headers, queued_project, queued_run,
                                     [(i, True) for i in range(len(items))])
            confirmation = CONFIRM["confirmation"] | {
                "primary_only": True, "secondary_account_id": None, "secondary_level": None,
            }
            queued = submit(client, headers, queued_project, queued_run, queued_revision, confirmation=confirmation)
            assert queued.status_code == 202, queued.text
            page = deliveries(client, queued.json()["id"])
            assert page["batch"]["secondary_account_id"] is None
            assert {row["item"]["account_role"] for row in page["items"]} == {"primary"}

            endpoint = f"{API}/tasks/{task_id}/send-primary-only"
            switched = client.post(endpoint, headers=headers)
            assert switched.status_code == 202, switched.text
            state = switched.json()
            assert state["checkpoint"]["primary_only"]
            assert state["checkpoint"]["accounts"]["secondary"]["state"] == "sending"
            assert client.post(endpoint, headers=headers).json()["version"] == state["version"]
            preview = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run}).json()
            assert preview["secondary_account_id"] is None
            assert [row["account_role"] for row in preview["items"]] == [
                "primary", "secondary", "primary", "primary",
            ]
        finally:
            release.set()
        wait_for_live_cooldown(client, task_id, live_clock.now)
        live_clock.advance(7)
        wait_for_live_cooldown(client, task_id, live_clock.now)
        live_clock.advance(7)
        wait_task(client, task_id, "succeeded")
        rows = deliveries(client, task_id)["items"]
        assert [row["attempts"][-1]["account_role"] for row in rows] == [
            "primary", "secondary", "primary", "primary",
        ]


def test_upgrade_single_account_queue_waits_for_receipt_and_refreshes_previews(tmp_path, transport, live_clock):
    gateway, _ = transport
    entered, release = Event(), Event()
    calls = []

    class Sender:
        async def send(self, work):
            calls.append(work)
            if len(calls) == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return SendResult(outcome="confirmed", receipt=str(77000 + len(calls)))

    items = [sample(i, mode=1, color=0xFFFFFF) for i in range(5)]
    with open_client(tmp_path, live_source=FakeSource(items), bilibili_transport=gateway,
                     credential_store=MemoryCredentialStore(), live_sender=Sender()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        save_secondary(client, 3)
        batches = []
        try:
            for suffix in ("running", "queued"):
                project, run = make_run(client, headers, suffix=suffix)
                revision = choose(client, headers, project, run, [(i, True) for i in range(len(items))])
                created = submit(client, headers, project, run, revision, **CONFIRM)
                assert created.status_code == 202, created.text
                batches.append((project, run, created.json()["id"]))
                if suffix == "running":
                    assert entered.wait(5)

            active, queued = batches
            endpoint = f"{API}/send-queue/dual-upgrade"
            assert client.get(endpoint).json() == 0
            enable_dual(client, headers)
            assert client.get(endpoint).json() == 2

            def preview(batch):
                project, run, _ = batch
                response = client.get(f"{API}/projects/{project}/send-preview", params={"run_id": run})
                assert response.status_code == 200, response.text
                return response.json()

            # Existing tasks still have a single account until the user accepts.
            assert all(preview(batch)["secondary_account_id"] is None for batch in batches)
            response = client.post(endpoint, headers=headers, json={"secondary_account_id": SECONDARY})
            assert response.status_code == 202 and response.json() == 2, response.text
            repeat = client.post(endpoint, headers=headers, json={"secondary_account_id": SECONDARY})
            assert repeat.status_code == 202 and repeat.json() == 0
            assert len(calls) == 1
            assert preview(active)["secondary_account_id"] is None
            assert preview(queued)["secondary_account_id"] is None
            before = deliveries(client, active[2])
            assert before["items"][0]["status"] == "in_flight"
        finally:
            release.set()

        deadline = monotonic() + 5
        while len(calls) < 2 and monotonic() < deadline:
            sleep(0.01)
        assert [(call.task_id, call.account_role) for call in calls] == [
            (active[2], "primary"), (active[2], "secondary"),
        ]
        assert preview(queued)["secondary_account_id"] == SECONDARY
        assert [row["account_role"] for row in preview(queued)["items"]] == [
            "primary", "secondary", "primary", "secondary", "primary",
        ]
        changed = preview(active)
        assert changed["secondary_account_id"] == SECONDARY
        assert [row["account_role"] for row in changed["items"]] == [
            "primary", "primary", "secondary", "primary", "secondary",
        ]
        done = deliveries(client, active[2])["items"][0]
        assert done["status"] == "confirmed" and done["attempts"][0]["account_role"] == "primary"


def test_mock_snapshot_can_test_a_resolved_bilibili_target(tmp_path, transport):
    gateway, calls = transport
    client = app_with_inputs(tmp_path, [sample(0)], live_source=FakeSource([]), bilibili_transport=gateway)
    with client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        mock_project, mock_run = make_mock_run(client, headers)
        revision = choose(client, headers, mock_project, mock_run, [(0, True)])
        denied = submit(client, headers, mock_project, mock_run, revision, **CONFIRM)
        assert denied.json()["error"]["code"] == "bilibili_target_required" and calls == []

        project = client.post(f"{API}/projects?source=bilibili", json=PROJECT, headers=headers).json()["id"]
        task = client.post(f"{API}/projects/{project}/tasks", json={"kind": "download"},
                           headers=headers | {"Idempotency-Key": "mock-source"}).json()
        wait_task(client, task["id"], "succeeded")
        # The completed synthetic snapshot supplies the sample, while the target stays the resolved Bilibili part.
        chinese = upload_subtitle(client, project, headers, [(0, 3, "合成对白")], "chinese.srt")
        japanese = upload_subtitle(client, project, headers, [(0, 3, "テスト用の台詞")], "japanese.srt")
        run = classify(client, project, {"kind": "classify", "snapshot_id": task["id"],
                                        "subtitle_version_id": chinese, "japanese_subtitle_version_id": japanese,
                                        "llm_config_version": 0}, headers).json()
        wait_task(client, run["id"], "succeeded")
        revision = choose(client, headers, project, run["id"], [(0, True)])
        batch = submit(client, headers, project, run["id"], revision, **CONFIRM).json()
        wait_task(client, batch["id"], "succeeded")
        assert len(calls) == 1 and calls[0]["oid"] == [CID]


def test_crash_with_only_unknown_releases_queue_and_recovers_other_batch_paused(tmp_path, transport, live_clock):
    gateway, calls = transport
    store = MemoryCredentialStore()
    entered = Event()

    class Interrupted:
        async def send(self, _work):
            entered.set()
            await asyncio.Future()

    options = dict(live_source=FakeSource([sample(0)]), bilibili_transport=gateway, credential_store=store)
    client = open_client(tmp_path, live_sender=Interrupted(), **options)
    with client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()
        assert entered.wait(5)
        second_project, second_run = make_run(client, headers)
        second_revision = choose(client, headers, second_project, second_run, [(0, True)])
        queued = submit(client, headers, second_project, second_run, second_revision, **CONFIRM).json()
        assert wait_task(client, queued["id"], "queued")["next_attempt_at"] is None
        assert not deliveries(client, queued["id"])["items"][0]["attempts"]

    with open_client(tmp_path, **options) as restarted:
        headers = connect_session(restarted)
        review = wait_task(restarted, first["id"], "needs_review")
        assert review["progress"]["unknown"] == 1 and calls == []
        waiting = wait_task(restarted, queued["id"], "paused")
        assert waiting["error"]["code"] == "interrupted"
        assert restarted.get(f"{API}/send-queue/interrupted").json() == 1
        assert control(restarted, headers, waiting, "resume").status_code == 202
        assert restarted.get(f"{API}/send-queue/interrupted").json() == 0
        wait_for_live_cooldown(restarted, queued["id"], live_clock.now)
        assert calls == []
        live_clock.advance(7)
        assert wait_task(restarted, queued["id"], "succeeded")["progress"]["done"] == 1
        row = deliveries(restarted, first["id"])["items"][0]
        assert resolve(restarted, headers, review, row, "skip").status_code == 200
        wait_task(restarted, first["id"], "succeeded")
        duplicate = submit(restarted, headers, project, run, revision, key="duplicate", **CONFIRM).json()
        assert duplicate["progress"]["skipped"] == 0
        live_clock.advance(7)
        wait_task(restarted, duplicate["id"], "succeeded")
        assert deliveries(restarted, duplicate["id"])["items"][0]["status"] == "confirmed"
        assert len(calls) == 2


def test_credentials_pause_and_recheck_frozen_account(tmp_path, transport, live_clock):
    gateway, calls = transport
    store = MemoryCredentialStore()
    client = open_client(tmp_path, credential_store=store, bilibili_transport=gateway,
                         live_source=FakeSource([sample(0)]))
    with client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        # Simulate losing the OS-stored credential after a previous successful bind.
        with sqlite3.connect(tmp_path / "workbench.sqlite3") as connection:
            reference = connection.execute("SELECT json_extract(data, '$.secret_ref') FROM bilibili_accounts").fetchone()[0]
        store.delete(reference)
        assert not client.get(f"{API}/settings/bilibili").json()["configured"]
        task = submit(client, headers, project, run, revision, **CONFIRM).json()
        paused = wait_task(client, task["id"], "paused")
        assert paused["error"]["code"] == "bilibili_credential_missing"
        assert deliveries(client, task["id"])["items"][0]["attempts"][0]["outcome"] == "blocked"
        assert account_settings(client, headers, CREDENTIAL | {"sessdata": "synthetic-second-session"}).status_code == 200
        live_clock.advance(7)
        assert control(client, headers, paused, "resume").status_code == 202
        next_paused = wait_task(client, task["id"], "paused")
        assert next_paused["error"]["code"] == "bilibili_account_changed"
        assert calls == []
        assert account_settings(client, headers).status_code == 200
        live_clock.advance(7)
        assert control(client, headers, next_paused, "resume").status_code == 202
        wait_task(client, task["id"], "succeeded")
        assert len(calls) == 1


def test_resume_interrupted_sends_preserves_cooldown_queue_and_manual_pause(tmp_path, monkeypatch, live_clock):
    monkeypatch.setattr(bilibili_sender, "_aid", lambda _: 9001)
    calls = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": int(ACCOUNT)}})
        calls.append(parse_qs(request.content.decode())["msg"][0])
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"code": 0, "data": {"dmid_str": str(76000 + len(calls))}})

    options = dict(live_source=FakeSource([sample(0), sample(1), sample(2), sample(3)]),
                   credential_store=MemoryCredentialStore(),
                   bilibili_transport=httpx.MockTransport(handle))
    count_url = f"{API}/send-queue/interrupted"
    resume_url = f"{API}/send-queue/resume-interrupted"
    client = open_client(tmp_path, **options)
    with client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        inputs = [make_run(client, headers) for _ in range(3)]

        def queue(inputs, choices):
            project, run = inputs
            revision = choose(client, headers, project, run, choices)
            return submit(client, headers, project, run, revision, **CONFIRM).json()["id"]

        first = queue(inputs[0], [(0, True), (1, True)])
        wait_for_live_cooldown(client, first, live_clock.now)
        live_clock.advance(7)
        deadline = wait_for_live_cooldown(client, first, live_clock.now)
        assert deadline["error"]["code"] == "send_rate_limited"
        assert deadline["progress"]["done"] == 1
        second = queue(inputs[1], [(2, True)])
        third = queue(inputs[2], [(3, True)])
        paused = control(client, headers, wait_task(client, third, "queued"), "pause").json()
        assert paused["state"] == "paused"
        assert client.get(count_url).json() == 0
    with open_client(tmp_path, **options) as restarted:
        assert restarted.get(count_url).status_code == 401
        headers = connect_session(restarted)
        for task in (first, second):
            assert wait_task(restarted, task, "paused")["error"]["code"] == "interrupted"
        assert restarted.get(count_url).json() == 2
        assert restarted.post(resume_url).status_code == 403
        response = restarted.post(resume_url, headers=headers)
        assert response.status_code == 202 and response.json() == 2
        assert restarted.post(resume_url, headers=headers).json() == 0
        assert restarted.get(count_url).json() == 0
        restored = wait_task(restarted, first, "backoff")
        for field in ("next_attempt_at", "progress", "error", "checkpoint"):
            assert restored[field] == deadline[field]
        assert wait_task(restarted, second, "queued")["next_attempt_at"] is None
        assert wait_task(restarted, third, "paused") == paused
        assert calls == ["合成条目 0", "合成条目 1"]
        live_clock.now = datetime.fromisoformat(restored["next_attempt_at"])
        wait_task(restarted, first, "succeeded")
        wait_for_live_cooldown(restarted, second, live_clock.now)
        live_clock.advance(7)
        wait_task(restarted, second, "succeeded")
        assert calls == ["合成条目 0", "合成条目 1", "合成条目 1", "合成条目 2"]
        assert wait_task(restarted, third, "paused") == paused


@pytest.mark.parametrize("seconds", [180, 19])
def test_live_frequency_limit_retries_same_item_after_configured_wait(tmp_path, monkeypatch, live_clock, seconds):
    monkeypatch.setattr(bilibili_sender, "_aid", lambda _: 9001)
    calls = []
    replies = iter([
        {"code": 36703, "message": "您发送弹幕的频率过快"},
        {"code": 1000},
        {"code": 0, "data": {"dmid_str": "76001"}},
        {"code": 0, "data": {"dmid_str": "76002"}},
    ])

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": int(ACCOUNT)}})
        calls.append(parse_qs(request.content.decode())["progress"][0])
        return httpx.Response(200, json=next(replies))

    with open_client(tmp_path, live_source=FakeSource([sample(0), sample(1)]),
                     bilibili_transport=httpx.MockTransport(handle), credential_store=MemoryCredentialStore()) as client:
        headers = connect_session(client)
        settings = f"{API}/settings/sending"
        assert client.get(settings).json() == {
            "item_min_seconds": 5, "item_max_seconds": 7,
            "rate_limit_seconds": 180,
        }
        if seconds != 180:
            assert client.put(settings, headers=headers, json={"rate_limit_seconds": seconds}).status_code == 200
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]

        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        deadline = datetime.fromisoformat(waiting["next_attempt_at"])
        assert deadline == live_clock.now + timedelta(seconds=seconds)
        assert waiting["error"]["code"] == "send_rate_limited"
        assert waiting["progress"]["done"] == 0
        first, second = deliveries(client, task_id)["items"]
        assert first["status"] == "pending"
        assert first["attempts"][0]["bilibili_error"] == {
            "http_status": 200, "code": 36703, "message": "您发送弹幕的频率过快",
        }
        assert second["attempts"] == []
        paused = control(client, headers, waiting, "pause").json()
        assert paused["state"] == "paused"
        resumed = control(client, headers, paused, "resume")
        assert resumed.status_code == 202
        assert resumed.json()["state"] == "backoff"
        assert resumed.json()["error"] == waiting["error"]
        assert resumed.json()["next_attempt_at"] == waiting["next_attempt_at"]
        assert control(client, headers, paused, "resume").json() == resumed.json()
        live_clock.advance(seconds - 1)
        assert calls == ["1137"]

        live_clock.now = deadline
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        assert calls == ["1137", "1137"]
        first, second = deliveries(client, task_id)["items"]
        assert first["status"] == "failed"
        assert [attempt["outcome"] for attempt in first["attempts"]] == ["limited", "rejected"]
        assert second["attempts"] == []

        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        finished = wait_task(client, task_id, "succeeded")
        assert finished["progress"]["done"] == 2 and finished["progress"]["failed"] == 0
        assert calls == ["1137", "1137", "1138", "1137"]


def test_successful_live_sends_wait_between_items_and_across_batches(tmp_path, transport, live_clock):
    gateway, calls = transport
    options = dict(live_source=FakeSource([sample(index) for index in range(4)]),
                   bilibili_transport=gateway, credential_store=MemoryCredentialStore())
    settings = f"{API}/settings/sending"
    timing = {"item_min_seconds": 11, "item_max_seconds": 11, "rate_limit_seconds": 180}
    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.put(settings, headers=headers, json={**timing, "item_max_seconds": 10}).status_code == 422
        assert client.put(settings, headers=headers, json=timing).json() == timing
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True), (2, False), (3, False)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        waiting = wait_for_live_cooldown(client, first, live_clock.now)
        deadline = datetime.fromisoformat(waiting["next_attempt_at"])
        assert deadline == live_clock.now + timedelta(seconds=11)
        assert waiting["error"] is None and waiting["progress"]["done"] == len(calls) == 1

    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.get(settings).json() == timing
        paused = wait_task(client, first, "paused")
        assert paused["next_attempt_at"] == waiting["next_attempt_at"]
        resumed = control(client, headers, paused, "resume")
        assert resumed.status_code == 202
        live_clock.now = deadline - timedelta(milliseconds=1)
        assert deliveries(client, first)["items"][1]["attempts"] == []
        live_clock.now = deadline
        wait_task(client, first, "succeeded")
        events = event_frames(client.get(f"{API}/tasks/{first}/events", params={"after": resumed.json()["version"]}))
        assert [snapshot["state"] for _, _, snapshot in events] == ["running", "running", "succeeded"]

        rate_endpoint = f"{API}/send-queue/rate"
        database = client.app.state.tasks.database
        with database(write=True) as repository:
            for index in range(98):
                # The gap between attempts is deliberately larger than the recorded wait.
                started_at = live_clock.now - timedelta(minutes=99 - index)
                limited = index == 1
                repository.save_attempt(first, "000000000001", Attempt(
                    number=index + 2, started_at=started_at, finished_at=started_at + timedelta(seconds=2),
                    outcome="limited" if limited else "confirmed",
                ))
                if limited:
                    deadline_at = started_at + timedelta(seconds=37)
                    repository.session.add(TaskEventRow(task_id=first, seq=1000, snapshot={
                        "next_attempt_at": deadline_at.isoformat().replace("+00:00", "Z"),
                        "error": {"code": "send_rate_limited"},
                    }))
        revision = choose(client, headers, project, run, [(0, False), (1, False), (2, True), (3, True)])
        second = submit(client, headers, project, run, revision, key="next-batch", **CONFIRM).json()["id"]
        waiting = wait_for_live_cooldown(client, second, live_clock.now)
        deadline = datetime.fromisoformat(waiting["next_attempt_at"])
        assert deadline == live_clock.now + timedelta(seconds=11)
        assert waiting["error"] is None and len(calls) == 2
        assert client.get(rate_endpoint).json() == {"task_id": second, "items_per_hour": 3600 / 11}
        live_clock.now = deadline
        waiting = wait_for_live_cooldown(client, second, live_clock.now)
        assert len(calls) == 3
        assert client.get(rate_endpoint).json() == {"task_id": second, "items_per_hour": 99 * 3600 / (99 * 11 + 35)}

        with database(write=True) as repository:
            for index in range(901):
                started_at = live_clock.now - timedelta(minutes=1000 - index)
                repository.save_attempt(first, "000000000001", Attempt(
                    number=index + 100, started_at=started_at, finished_at=started_at,
                    outcome="confirmed", pacing_wait_seconds=600 if index == 0 else 11,
                ))
        assert client.get(rate_endpoint).json() == {"task_id": second, "items_per_hour": 999 * 3600 / (999 * 11 + 35)}
        assert control(client, headers, waiting, "pause").status_code == 202
        paused = wait_task(client, second, "paused")
        assert client.get(rate_endpoint).json() is None
        resumed = control(client, headers, paused, "resume")
        assert resumed.status_code == 202 and resumed.json()["state"] == "backoff"
        assert client.get(rate_endpoint).json() == {"task_id": second, "items_per_hour": 999 * 3600 / (999 * 11 + 35)}
        assert len(calls) == 3
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        wait_task(client, second, "succeeded")
        assert len(calls) == 4
        assert client.get(rate_endpoint).json() is None


@pytest.mark.parametrize("retry_after", [0, 240])
def test_third_live_limit_keeps_delivery_pending_until_long_cooldown(tmp_path, transport, live_clock, retry_after):
    gateway, _ = transport
    attempts = []

    class LimitedSender:
        async def send(self, work):
            attempts.append(work.attempt)
            return SendResult(outcome="limited", retry_after_seconds=retry_after if work.attempt == 3 else 0)

    with open_client(tmp_path, live_source=FakeSource([sample(0)]), bilibili_transport=gateway,
                     credential_store=MemoryCredentialStore(), live_sender=LimitedSender()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        for _ in range(2):
            waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
            deadline = datetime.fromisoformat(waiting["next_attempt_at"])
            assert (deadline - live_clock.now).total_seconds() == 180
            live_clock.now = deadline

        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        deadline = datetime.fromisoformat(waiting["next_attempt_at"])
        assert (deadline - live_clock.now).total_seconds() == max(180, retry_after)
        assert waiting["error"]["code"] == "send_repeated_failures"
        assert waiting["progress"]["done"] == waiting["progress"]["failed"] == 0
        assert deliveries(client, task_id)["items"][0]["status"] == "pending"
        assert attempts == [1, 2, 3]
        live_clock.now = deadline
        for _ in range(4):
            waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
            live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        finished = wait_task(client, task_id, "failed")
        assert finished["error"]["code"] == "send_partial"
        assert finished["progress"]["done"] == finished["progress"]["failed"] == 1
        assert attempts == list(range(1, 9))


def test_live_rejections_cool_account_before_next_item(tmp_path, transport, live_clock):
    gateway, _ = transport
    attempts = []

    class RejectingSender:
        async def send(self, work):
            attempts.append(work.item.item_id)
            return SendResult(outcome="rejected")

    with open_client(tmp_path, live_source=FakeSource([sample(index) for index in range(4)]),
                     bilibili_transport=gateway, credential_store=MemoryCredentialStore(),
                     live_sender=RejectingSender()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(index, True) for index in range(4)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        for _ in range(2):
            waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
            live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        deadline = datetime.fromisoformat(waiting["next_attempt_at"])
        assert deadline == live_clock.now + timedelta(seconds=60)
        assert waiting["error"]["code"] == "send_repeated_failures"
        assert waiting["progress"]["failed"] == len(attempts) == 3
        live_clock.now = deadline
        for _ in range(4):
            waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
            live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        assert wait_task(client, task_id, "failed")["progress"]["failed"] == len(set(attempts)) == 4
        assert len(attempts) == 8


def test_automatic_retry_preserves_confirmed_receipts(tmp_path, transport, live_clock):
    gateway, _ = transport
    sent = []

    class Sender:
        async def send(self, work):
            sent.append(work.delivery_id)
            if len(sent) == 2:
                return SendResult(outcome="rejected")
            return SendResult(outcome="confirmed", receipt=str(len(sent)))

    with open_client(tmp_path, live_source=FakeSource([sample(index) for index in range(2)]),
                     bilibili_transport=gateway, credential_store=MemoryCredentialStore(),
                     live_sender=Sender()) as client:
        headers = connect_session(client)
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True), (1, True)])
        task_id = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        project_url = f"{API}/projects/{project}"
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        assert client.get(project_url).json()["completed"] is False
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        waiting = wait_for_live_cooldown(client, task_id, live_clock.now)
        assert waiting["checkpoint"]["automatic_retry_started"]
        assert client.get(project_url).json()["completed"] is False
        preview = client.get(f"{project_url}/send-preview", params={"run_id": run, "mode": "live"}).json()
        assert preview["duplicate_count"] == 1
        second_project, second_run = make_run(client, headers)
        second_revision = choose(client, headers, second_project, second_run, [(0, True), (1, False)])
        second = submit(client, headers, second_project, second_run, second_revision, **CONFIRM).json()["id"]
        url = f"{API}/tasks/{task_id}/deliveries"
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        wait_task(client, task_id, "succeeded")
        assert client.get(project_url).json()["completed"] is True
        rows = client.get(url, headers=headers).json()["items"]
        assert sent == [rows[0]["id"], rows[1]["id"], rows[1]["id"]]
        assert [row["receipt"] for row in rows] == ["bilibili:1", "bilibili:3"]
        assert [attempt["outcome"] for attempt in rows[1]["attempts"]] == ["rejected", "confirmed"]
        waiting = wait_for_live_cooldown(client, second, live_clock.now)
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        wait_task(client, second, "succeeded")

    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE project_completion_overrides")
        connection.execute("UPDATE schema_version SET version=7")
    with open_client(tmp_path) as client:
        headers = connect_session(client)
        assert client.get(f"{API}/projects").json()["items"][0]["completed"] is True
        removed = client.patch(f"{project_url}/completion", headers=headers, json={"completed": False})
        assert removed.json()["completed"] is False
        assert client.get(project_url).json()["completed"] is False
    with sqlite3.connect(path.with_name(path.name + ".v7.bak")) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (7,)


def test_automatic_retry_releases_queue_after_restart(tmp_path, transport, live_clock):
    gateway, _ = transport
    sent = []

    class Sender:
        async def send(self, work):
            sent.append(work.task_id)
            if len(sent) <= 2:
                return SendResult(outcome="rejected")
            return SendResult(outcome="confirmed", receipt="76001")

    options = dict(live_source=FakeSource([sample(0)]), bilibili_transport=gateway,
                   credential_store=MemoryCredentialStore(), live_sender=Sender())
    decision_url = f"{API}/send-queue/pending-decision"
    with open_client(tmp_path, **options) as client:
        assert client.get(decision_url).status_code == 401
        headers = connect_session(client)
        assert client.get(decision_url).json() is None
        assert account_settings(client, headers).status_code == 200
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        first = submit(client, headers, project, run, revision, **CONFIRM).json()["id"]
        waiting = wait_for_live_cooldown(client, first, live_clock.now)
        assert waiting["checkpoint"]["automatic_retry_started"]
        assert client.get(decision_url).json() is None
        second_project, second_run = make_run(client, headers)
        second_revision = choose(client, headers, second_project, second_run, [(0, True)])
        second = submit(client, headers, second_project, second_run, second_revision, **CONFIRM).json()["id"]
        assert not deliveries(client, second)["items"][0]["attempts"]

    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.get(decision_url).json() is None
        first_paused = wait_task(client, first, "paused")
        second_paused = wait_task(client, second, "paused")
        assert first_paused["checkpoint"]["automatic_retry_started"]
        assert control(client, headers, second_paused, "resume").status_code == 202
        assert control(client, headers, first_paused, "resume").status_code == 202
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        terminal = wait_task(client, first, "failed")
        assert terminal["progress"]["failed"] == 1
        project_url = f"{API}/projects/{project}"
        detail = client.get(project_url).json()
        assert detail["send_partial_failure"] and not detail["completed"]
        assert detail["activity"] == {"stage": "send", "running": False, "active": False}
        waiting = wait_for_live_cooldown(client, second, live_clock.now)
        live_clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
        wait_task(client, second, "succeeded")
        assert [row["status"] for row in deliveries(client, first)["items"]] == ["failed"]
        assert sent == [first, first, second]


def test_upgrade_v6_keeps_live_rate_limit_deadline(tmp_path):
    path = tmp_path / "workbench.sqlite3"
    deadline = datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1, 6)")
        connection.execute("CREATE TABLE live_send_cooldowns (account_id VARCHAR PRIMARY KEY, deadline VARCHAR NOT NULL)")
        connection.execute("INSERT INTO live_send_cooldowns VALUES (?, ?)", (ACCOUNT, deadline.isoformat()))

    database = Database(path)
    try:
        database.initialize()
        with database() as repository:
            cooldown = repository.live_cooldown(ACCOUNT)
            assert cooldown.deadline == deadline and cooldown.reason == "rate_limit" and cooldown.consecutive_failures == 0
    finally:
        database.close()
    with sqlite3.connect(path.with_name(path.name + ".v6.bak")) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (6,)


def test_upgrade_v5_keeps_stage_five_delivery_records(tmp_path):
    options = dict(live_source=FakeSource([sample(0)]))
    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        project, run = make_run(client, headers)
        revision = choose(client, headers, project, run, [(0, True)])
        task_id = seed_legacy_send(client, project, run, revision)
        previous = deliveries(client, task_id)
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as connection:
        for table in ("live_send_cooldowns", "live_send_keys", "bilibili_accounts"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_version SET version=5")
        row = connection.execute("SELECT id, data FROM send_batches").fetchone()
        connection.execute("UPDATE send_batches SET data=json_remove(?, '$.account_id') WHERE id=?", (row[1], row[0]))
        connection.execute("UPDATE tasks SET inputs=json_remove(inputs, '$.confirmation') WHERE id=?", (task_id,))

    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert deliveries(client, task_id) == previous
        assert submit(client, headers, project, run, revision, mode="mock").status_code == 422
        assert not client.get(f"{API}/settings/bilibili").json()["configured"]
    with sqlite3.connect(path.with_name(path.name + ".v5.bak")) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone() == (5,)
        assert backup.execute("SELECT count(*) FROM send_attempts").fetchone() == (1,)


@pytest.mark.parametrize("status,payload,outcome,error", [
    (429, None, "limited", None),
    (200, {"code": -509}, "limited", None),
    (200, {"code": -111}, "blocked", "bilibili_credential_rejected"),
    (412, None, "blocked", "bilibili_access_denied"),
    (200, {"code": 62002}, "rejected", "bilibili_video_unavailable"),
    (200, {"code": 1000}, "rejected", "bilibili_delivery_rejected"),
    (200, {"code": 0, "data": {}}, "unknown", None),
    (500, None, "unknown", None),
    (302, None, "unknown", None),
])
def test_platform_result_mapping_never_follows_redirects(status, payload, outcome, error, monkeypatch):
    calls = []
    monkeypatch.setattr(bilibili_sender, "_aid", lambda _: 9001)

    def reply(request):
        calls.append(request.url)
        return httpx.Response(status, json=payload, headers={"Location": "https://other.invalid", "Retry-After": "11"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply), follow_redirects=True) as client:
            gateway = bilibili_sender.BilibiliGateway(client)
            return await gateway.post(BilibiliCredential.model_validate(CREDENTIAL), SendWork(
                task_id="synthetic-task", delivery_id="synthetic-delivery", account_ref="synthetic-owner",
                account_id=ACCOUNT, target_cid=CID, target_bvid=PROJECT["target_bvid"], attempt=1, mode="live",
                item=SendItem(item_id="id:2000", time_ms=1137, text="合成条目", color_rgb=1, font_size=18, mode=5),
            ))

    result = asyncio.run(run())
    assert result.outcome == outcome and result.error_code == error and len(calls) == 1
    assert result.retry_after_seconds == (11 if outcome == "limited" else None)
