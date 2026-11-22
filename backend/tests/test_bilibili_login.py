from base64 import b64decode
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from danmaku.adapters import bilibili_login
from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.application.sessions import LOCAL_OWNER_ID
from test_workbench import API, connect_session, open_client

LOGIN = f"{API}/bilibili/login"
UID = "17001"
QR_KEY = "synthetic-qr-key"
TICKET = "synthetic-cross-domain-ticket"
PROFILE = {"account_id": UID, "name": "合成用户", "avatar_url": "https://i0.hdslb.com/synthetic.png", "level": 4}


def ok(data):
    return httpx.Response(200, json={"code": 0, "data": data})


def cookies(suffix, uid=UID):
    return [("Set-Cookie", f"{key}={value}; Path=/; Secure") for key, value in {
        "SESSDATA": f"synthetic-session-{suffix}", "bili_jct": f"synthetic-csrf-{suffix}", "DedeUserID": uid,
    }.items()]


class Platform:
    def __init__(self, *, old_format=False):
        self.old_format = old_format
        self.qr_status = 86101
        self.needs_refresh = False
        self.fail_confirm = False
        self.fail_profile = False
        self.calls = []

    def __call__(self, request):
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/qrcode/generate"):
            assert "cookie" not in request.headers
            return ok({"qrcode_key": QR_KEY,
                       "url": f"https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key={QR_KEY}"})
        if path.endswith("/qrcode/poll"):
            assert request.url.params["qrcode_key"] == QR_KEY and "cookie" not in request.headers
            if self.qr_status:
                return ok({"code": self.qr_status})
            url = f"https://passport.biligame.com/x/passport-login/web/crossDomain?ticket={TICKET}"
            if self.old_format:
                url = ("https://passport.biligame.com/crossDomain?SESSDATA=synthetic%2Cold&"
                       f"bili_jct=synthetic-csrf-old&DedeUserID={UID}")
            return ok({"code": 0, "url": url, "refresh_token": "synthetic-refresh-old"})
        if path.endswith("/crossDomain"):
            assert self.old_format is False and request.url.host == "passport.biligame.com"
            assert request.url.params["ticket"] == TICKET and "cookie" not in request.headers
            return httpx.Response(302, headers=[*cookies("old"), ("Location", "https://example.invalid")])
        if path == "/x/web-interface/nav":
            if self.fail_profile:
                return httpx.Response(503)
            assert "SESSDATA=synthetic" in request.headers["Cookie"]
            return ok({"isLogin": True, "mid": int(UID), "uname": PROFILE["name"], "face": PROFILE["avatar_url"],
                       "level_info": {"current_level": PROFILE["level"]}})
        if path.endswith("/cookie/info"):
            return ok({"refresh": self.needs_refresh})
        if path.startswith("/correspond/1/"):
            assert request.url.host == "www.bilibili.com"
            assert len(path.rsplit("/", 1)[1]) == 256
            return httpx.Response(200, text='<div id="1-name">synthetic-refresh-csrf</div>')
        if path.endswith("/cookie/refresh"):
            assert parse_qs(request.content.decode()) == {
                "csrf": ["synthetic-csrf-old"], "refresh_csrf": ["synthetic-refresh-csrf"],
                "refresh_token": ["synthetic-refresh-old"], "source": ["main_web"],
            }
            assert "SESSDATA=synthetic-session-old" in request.headers["Cookie"]
            return httpx.Response(200, headers=cookies("new"),
                                  json={"code": 0, "data": {"refresh_token": "synthetic-refresh-new"}})
        if path.endswith("/confirm/refresh"):
            assert parse_qs(request.content.decode()) == {
                "csrf": ["synthetic-csrf-new"], "refresh_token": ["synthetic-refresh-old"],
            }
            assert "SESSDATA=synthetic-session-new" in request.headers["Cookie"]
            return httpx.Response(503) if self.fail_confirm else ok(None)
        if path == "/login/exit/v2":
            return ok(None)
        pytest.fail(f"Unexpected upstream path {path}")


@pytest.fixture
def clock(monkeypatch):
    # Keep HTTP integration tests runnable with the project's optional B 站 SDK omitted.
    monkeypatch.setattr(bilibili_login, "_qr_image", lambda _: "data:image/png;base64,c3ludGhldGlj")
    monkeypatch.setattr(bilibili_login, "_correspond_path", lambda: "a" * 256)
    values = [0]

    def attach(client):
        client.app.state.bilibili_login.clock = lambda: values[0]
        return values

    return attach


@pytest.mark.parametrize("old_format", [False, True])
def test_scan_login_restart_logout(tmp_path, clock, old_format, caplog):
    platform = Platform(old_format=old_format)
    store = MemoryCredentialStore()
    options = {"bilibili_transport": httpx.MockTransport(platform), "credential_store": store}
    with open_client(tmp_path, **options) as client:
        now = clock(client)
        assert client.post(f"{LOGIN}/qr").status_code == 401
        headers = connect_session(client)
        assert client.post(f"{LOGIN}/qr").status_code == 403
        created = client.post(f"{LOGIN}/qr", headers=headers)
        assert created.status_code == 201 and created.headers["Cache-Control"] == "no-store"
        assert created.json()["image"].startswith("data:image/png;base64,") and QR_KEY not in created.text
        path = f"{LOGIN}/qr/{created.json()['id']}/poll"
        with caplog.at_level(logging.DEBUG):
            assert client.post(path, headers=headers).json()["status"] == "waiting"
            platform.qr_status = 86090
            assert client.post(path, headers=headers).json()["status"] == "waiting"
            now[0] += 2
            assert client.post(path, headers=headers).json()["status"] == "scanned"
            platform.qr_status = 0
            now[0] += 2
            result = client.post(path, headers=headers)
        assert result.json() == {"status": "authenticated", "login": {
            "status": "valid", "profile": PROFILE, "auto_refresh": True,
        }}
        for sensitive in (QR_KEY, TICKET, "synthetic-session-old", "synthetic-refresh-old"):
            assert sensitive not in caplog.text and sensitive not in result.text
        assert client.post(path, headers=headers).json() == result.json()
        assert platform.calls.count("/x/passport-login/web/crossDomain") == int(not old_format)
        assert client.get(f"{API}/settings/bilibili").json()["version"] == 1

    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.post(f"{LOGIN}/status", headers=headers).json() == result.json()["login"]
        if old_format:
            _, secret = client.app.state.bilibili_settings.saved_login(LOCAL_OWNER_ID)
            assert secret.sessdata.get_secret_value() == "synthetic%2Cold"
        assert client.delete(f"{LOGIN}/", headers=headers).status_code == 204
        assert client.post(f"{LOGIN}/status", headers=headers).json()["status"] == "unconfigured"
        assert client.post(path, headers=headers).json()["status"] == "expired"
        assert client.get(f"{API}/settings/bilibili").json()["configured"] is False
        assert not store._secrets
        assert platform.calls[-1] == "/login/exit/v2"


def test_refresh_persists_before_confirmation_and_retries_after_restart(tmp_path, clock):
    platform = Platform()
    platform.qr_status = 0
    options = {"bilibili_transport": httpx.MockTransport(platform), "credential_store": MemoryCredentialStore()}
    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        attempt = client.post(f"{LOGIN}/qr", headers=headers).json()["id"]
        assert client.post(f"{LOGIN}/qr/{attempt}/poll", headers=headers).json()["status"] == "authenticated"
        platform.needs_refresh = True
        platform.fail_confirm = True
        assert client.post(f"{LOGIN}/status", headers=headers).json()["auto_refresh"] is True
        account, saved = client.app.state.bilibili_settings.saved_login(LOCAL_OWNER_ID)
        assert account.version == 1 and saved.sessdata.get_secret_value() == "synthetic-session-new"
        assert saved.pending_confirmation.get_secret_value() == "synthetic-refresh-old"
        assert len(options["credential_store"]._secrets) == 1

    platform.fail_confirm = False
    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.post(f"{LOGIN}/status", headers=headers).json()["status"] == "valid"
        account, saved = client.app.state.bilibili_settings.saved_login(LOCAL_OWNER_ID)
        assert account.version == 1 and saved.pending_confirmation is None
        assert saved.refresh_token.get_secret_value() == "synthetic-refresh-new"
        assert platform.calls.count("/x/passport-login/web/cookie/refresh") == 1
        assert platform.calls.count("/x/passport-login/web/confirm/refresh") == 2


def test_dual_account_roles_and_setting_are_independent(tmp_path, clock):
    secondary_uid = "17002"

    class DualPlatform(Platform):
        scan_uid = UID
        secondary_level = 0
        secondary_expired = False
        revoked = []

        def __call__(self, request):
            path = request.url.path
            if path.endswith("/crossDomain"):
                return httpx.Response(302, headers=cookies(self.scan_uid, self.scan_uid))
            if path == "/x/web-interface/nav":
                uid = next(uid for uid in (UID, secondary_uid) if f"SESSDATA=synthetic-session-{uid};" in
                           request.headers["Cookie"])
                if uid == secondary_uid and self.secondary_expired:
                    return ok({"isLogin": False})
                return ok({"isLogin": True, "mid": int(uid), "uname": f"合成用户 {uid}",
                           "level_info": {"current_level": self.secondary_level if uid == secondary_uid else 4}})
            if path == "/login/exit/v2":
                self.revoked.append(parse_qs(request.content.decode())["biliCSRF"][0])
            return super().__call__(request)

    platform = DualPlatform()
    platform.qr_status = 0
    store = MemoryCredentialStore()
    options = {"bilibili_transport": httpx.MockTransport(platform), "credential_store": store}
    settings = f"{API}/settings"
    secondary = "?role=secondary"

    with open_client(tmp_path, **options) as client:
        clock(client)
        headers = connect_session(client)

        def start(role=""):
            result = client.post(f"{LOGIN}/qr{role}", headers=headers)
            assert result.status_code == 201
            return f"{LOGIN}/qr/{result.json()['id']}/poll"

        assert client.get(settings).json()["sending"]["dual_account_enabled"] is False
        primary_poll, secondary_poll = start(), start(secondary)
        assert client.post(primary_poll + secondary, headers=headers).json()["status"] == "expired"
        assert client.post(primary_poll, headers=headers).json()["login"]["profile"]["account_id"] == UID
        duplicate = client.post(secondary_poll + secondary, headers=headers)
        assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "bilibili_duplicate_account"
        assert len(store._secrets) == 1

        platform.scan_uid = secondary_uid
        signed_in = client.post(start(secondary) + secondary, headers=headers).json()
        assert signed_in["login"]["profile"]["account_id"] == secondary_uid and len(store._secrets) == 2
        assert signed_in["login"]["profile"]["level"] == 0
        binding, _ = client.app.state.bilibili_settings.saved_login(LOCAL_OWNER_ID, "secondary")
        assert binding.level == 0
        enabled = client.put(settings, headers=headers, json={"sending": {"dual_account_enabled": True}})
        assert enabled.json()["sending"]["dual_account_enabled"] is True
        assert client.put(f"{settings}/sending", headers=headers, json={"rate_limit_seconds": 42}).status_code == 200
        assert client.get(settings).json()["sending"]["dual_account_enabled"] is True
        disabled = client.put(settings, headers=headers, json={"sending": {"dual_account_enabled": False}})
        assert disabled.json()["sending"] == {
            "item_min_seconds": 5, "item_max_seconds": 7, "rate_limit_seconds": 42, "dual_account_enabled": False,
        }
        assert not platform.revoked

    with open_client(tmp_path, **options) as client:
        headers = connect_session(client)
        assert client.get(settings).json()["sending"]["dual_account_enabled"] is False
        platform.secondary_level = 2
        updated = client.post(f"{LOGIN}/status{secondary}", headers=headers).json()
        assert updated["profile"] == signed_in["login"]["profile"] | {"level": 2}
        binding, _ = client.app.state.bilibili_settings.saved_login(LOCAL_OWNER_ID, "secondary")
        assert binding.level == 2
        platform.secondary_expired = True
        assert client.post(f"{LOGIN}/status{secondary}", headers=headers).json()["status"] == "valid"
        assert client.post(f"{LOGIN}/status{secondary}&force=true", headers=headers).json() == {
            "status": "renewal_required", "profile": None, "auto_refresh": False,
        }
        assert client.delete(f"{LOGIN}/{secondary}", headers=headers).status_code == 204
        assert client.post(f"{LOGIN}/status{secondary}&force=true", headers=headers).json()["status"] == "unconfigured"
        assert client.post(f"{LOGIN}/status", headers=headers).json()["profile"]["account_id"] == UID
        assert len(store._secrets) == 1 and platform.revoked == [f"synthetic-csrf-{secondary_uid}"]


def test_qr_expiry_cancellation_and_profile_failure_recovery(tmp_path, clock):
    platform = Platform()
    with open_client(tmp_path, bilibili_transport=httpx.MockTransport(platform),
                     credential_store=MemoryCredentialStore()) as client:
        now = clock(client)
        headers = connect_session(client)
        cancelled = client.post(f"{LOGIN}/qr", headers=headers).json()["id"]
        assert client.delete(f"{LOGIN}/qr/{cancelled}", headers=headers).status_code == 204
        assert client.post(f"{LOGIN}/qr/{cancelled}/poll", headers=headers).json()["status"] == "expired"
        expired = client.post(f"{LOGIN}/qr", headers=headers).json()["id"]
        now[0] += 181
        assert client.post(f"{LOGIN}/qr/{expired}/poll", headers=headers).json()["status"] == "expired"
        assert platform.calls.count("/x/passport-login/web/qrcode/poll") == 0

        platform.qr_status, platform.fail_profile = 0, True
        attempt = client.post(f"{LOGIN}/qr", headers=headers).json()["id"]
        path = f"{LOGIN}/qr/{attempt}/poll"
        assert client.post(path, headers=headers).status_code == 503
        assert client.get(f"{API}/settings/bilibili").json()["configured"] is False
        now[0] += 2
        platform.fail_profile = False
        assert client.post(path, headers=headers).json()["status"] == "authenticated"
        assert platform.calls.count("/x/passport-login/web/qrcode/poll") == 1


def test_optional_qr_and_rsa_implementations():
    pytest.importorskip("qrcode.image.pil")
    pytest.importorskip("Cryptodome")
    image = bilibili_login._qr_image("https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=synthetic")
    assert b64decode(image.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n")
    assert len(bytes.fromhex(bilibili_login._correspond_path())) == 128
