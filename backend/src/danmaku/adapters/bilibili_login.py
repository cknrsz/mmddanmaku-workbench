import asyncio
from base64 import b64encode
from contextvars import ContextVar
from io import BytesIO
import logging
import re
from time import time
from urllib.parse import unquote

import httpx
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from danmaku.domain.bilibili_login import BilibiliLoginSecret, QrChallenge
from danmaku.domain.workbench import WorkbenchError

PASSPORT = "https://passport.bilibili.com"
WEB = f"{PASSPORT}/x/passport-login/web"
SITE = "https://www.bilibili.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": f"{SITE}/",
    "Origin": SITE,
}
QR_STATES = {86101: "waiting", 86090: "scanned", 86038: "expired"}
COOKIE_FIELDS = {"SESSDATA": "sessdata", "bili_jct": "bili_jct", "DedeUserID": "dedeuserid", "buvid3": "buvid3"}
MAX_RESPONSE = 128 * 1024
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""
private_request = ContextVar("bilibili_private_request", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not private_request.get()


_filter = _PrivateRequestFilter()
for _logger in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
    logging.getLogger(_logger).addFilter(_filter)


def _invalid() -> WorkbenchError:
    return WorkbenchError(502, "bilibili_login_response_invalid", "B 站登录响应无法识别，请重新生成二维码")


def _unavailable() -> WorkbenchError:
    return WorkbenchError(503, "bilibili_login_unavailable", "暂时无法连接 B 站登录服务，请稍后重试", True)


def _missing_dependency() -> WorkbenchError:
    return WorkbenchError(503, "bilibili_sdk_missing", "请安装项目的 bilibili 可选依赖以使用 B 站登录")


def _https_url(value: object, hosts: set[str], paths: set[str]) -> httpx.URL:
    if isinstance(value, str) and len(value) <= 4096:
        try:
            url = httpx.URL(value)
            if url.scheme == "https" and url.port is None and not url.userinfo and url.host in hosts and url.path in paths:
                return url
        except httpx.InvalidURL:
            pass
    raise _invalid()


def _qr_image(url: str) -> str:
    try:
        import qrcode
        from qrcode.image.pil import PilImage
    except ImportError:
        raise _missing_dependency() from None
    image = qrcode.make(url, image_factory=PilImage, box_size=6, border=4)
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return f"data:image/png;base64,{b64encode(buffer.getvalue()).decode('ascii')}"


def _correspond_path() -> str:
    try:
        from Cryptodome.Cipher import PKCS1_OAEP
        from Cryptodome.Hash import SHA256
        from Cryptodome.PublicKey import RSA
    except ImportError:
        raise _missing_dependency() from None
    cipher = PKCS1_OAEP.new(RSA.import_key(PUBLIC_KEY), SHA256)
    return cipher.encrypt(f"refresh_{int(time() * 1000)}".encode()).hex()


def _cookies(response: httpx.Response) -> dict[str, str]:
    result = {}
    for header in response.headers.get_list("set-cookie"):
        key, _, value = header.split(";", 1)[0].partition("=")
        if key.strip() in COOKIE_FIELDS and value:
            result[key.strip()] = value.strip('"')
    return result


def _credential(cookies: dict[str, str], refresh_token: object) -> BilibiliLoginSecret:
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _invalid()
    fields = {field: cookies[name] for name, field in COOKIE_FIELDS.items() if cookies.get(name)}
    uid = fields.get("dedeuserid", "")
    if not (uid.isascii() and uid.isdecimal()):
        fields.pop("dedeuserid", None)
    try:
        return BilibiliLoginSecret.model_validate({**fields, "refresh_token": refresh_token})
    except ValidationError:
        raise _invalid() from None


class BilibiliPassport:
    """B 站网页登录协议。网络错误可重试，换票和刷新 POST 由调用方管理重试。"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(self, method: str, url: str | httpx.URL, *, params=None, data=None,
                       credential: BilibiliLoginSecret | None = None, ticket: bool = False) -> httpx.Response:
        headers = dict(HEADERS)
        if credential is not None:
            headers["Cookie"] = "; ".join(f"{name}={value.get_secret_value()}"
                                          for name, field in COOKIE_FIELDS.items()
                                          if (value := getattr(credential, field)) is not None)
        if ticket:
            headers["Referer"] = f"{PASSPORT}/"
        token = private_request.set(True)
        try:
            request = httpx.Request(method, url, params=params, data=data, headers=headers)
            async with asyncio.timeout(10):
                response = await self.client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.status_code >= 400:
                        if response.status_code == 401:
                            raise WorkbenchError(409, "bilibili_credential_rejected", "B 站登录已失效，请重新扫码")
                        raise _unavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE:
                            raise _invalid()
                    headers = [(key, value) for key, value in response.headers.multi_items()
                               if key not in {"content-encoding", "content-length"}]
                    return httpx.Response(response.status_code, headers=headers, content=bytes(body))
                finally:
                    await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            raise _unavailable() from None
        finally:
            private_request.reset(token)

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise _invalid() from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise _invalid()
        code = payload["code"]
        if code == 0:
            return payload
        if code == -101:
            raise WorkbenchError(409, "bilibili_credential_rejected", "B 站登录已失效，请重新扫码")
        if code == 86095:
            raise WorkbenchError(409, "bilibili_refresh_rejected", "B 站拒绝续期，请重新扫码")
        raise _unavailable()

    @classmethod
    def _data(cls, response: httpx.Response) -> dict:
        data = cls._json(response).get("data")
        if not isinstance(data, dict):
            raise _invalid()
        return data

    async def generate(self) -> QrChallenge:
        data = self._data(await self._request("GET", f"{WEB}/qrcode/generate"))
        key = data.get("qrcode_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise _invalid()
        url = _https_url(data.get("url"), {"account.bilibili.com", "passport.bilibili.com"},
                         {"/h5/account-h5/auth/scan-web", "/h5-app/passport/login/scan"})
        return QrChallenge(key, await run_in_threadpool(_qr_image, str(url)))

    async def poll(self, key: str) -> str | BilibiliLoginSecret:
        response = await self._request("GET", f"{WEB}/qrcode/poll", params={"qrcode_key": key})
        data = self._data(response)
        code = data.get("code")
        if type(code) is not int:
            raise _invalid()
        if code in QR_STATES:
            return QR_STATES[code]
        if code != 0:
            raise _invalid()
        cookies = _cookies(response)
        if not all(cookies.get(name) for name in ("SESSDATA", "bili_jct")):
            url = _https_url(data.get("url"), {"passport.biligame.com", "passport.bilibili.com"},
                             {"/crossDomain", "/x/passport-login/web/crossDomain"})
            # Preserve the encoded SESSDATA value used in the Cookie header.
            for pair in url.query.decode("ascii").split("&"):
                name, _, value = pair.partition("=")
                if name in COOKIE_FIELDS and value:
                    cookies.setdefault(name, value if name == "SESSDATA" else unquote(value))
            if not all(cookies.get(name) for name in ("SESSDATA", "bili_jct")):
                cookies.update(_cookies(await self._request("GET", url, ticket=True)))
        return _credential(cookies, data.get("refresh_token"))

    async def refresh_needed(self, credential: BilibiliLoginSecret) -> bool:
        response = await self._request("GET", f"{WEB}/cookie/info", credential=credential)
        refresh = self._data(response).get("refresh")
        if type(refresh) is not bool:
            raise _invalid()
        return refresh

    async def refresh(self, credential: BilibiliLoginSecret) -> BilibiliLoginSecret:
        assert credential.refresh_token is not None
        path = await run_in_threadpool(_correspond_path)
        page = await self._request("GET", f"{SITE}/correspond/1/{path}", credential=credential)
        match = re.search(r'<div id="1-name">([0-9a-zA-Z_-]{1,256})</div>', page.text)
        if not match:
            raise _unavailable()
        response = await self._request("POST", f"{WEB}/cookie/refresh", credential=credential, data={
            "csrf": credential.bili_jct.get_secret_value(), "refresh_csrf": match[1],
            "refresh_token": credential.refresh_token.get_secret_value(), "source": "main_web",
        })
        new = _credential(_cookies(response), self._data(response).get("refresh_token"))
        return new.model_copy(update={"buvid3": new.buvid3 or credential.buvid3,
                                      "dedeuserid": new.dedeuserid or credential.dedeuserid,
                                      "pending_confirmation": credential.refresh_token})

    async def confirm_refresh(self, credential: BilibiliLoginSecret) -> None:
        assert credential.pending_confirmation is not None
        response = await self._request("POST", f"{WEB}/confirm/refresh", credential=credential, data={
            "csrf": credential.bili_jct.get_secret_value(),
            "refresh_token": credential.pending_confirmation.get_secret_value(),
        })
        self._json(response)

    async def logout(self, credential: BilibiliLoginSecret) -> None:
        await self._request("POST", f"{PASSPORT}/login/exit/v2", credential=credential,
                            data={"biliCSRF": credential.bili_jct.get_secret_value()})
