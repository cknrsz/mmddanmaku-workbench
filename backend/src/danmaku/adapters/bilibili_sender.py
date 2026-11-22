import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
from math import ceil

import httpx
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from danmaku.application.sending.settings import BilibiliSettings
from danmaku.application.sending.connection import SendConnection
from danmaku.domain.bilibili_login import BilibiliProfile
from danmaku.domain.sending import BilibiliCredential, BilibiliSendDiagnostic, SendResult, SendWork
from danmaku.domain.workbench import NumericId, WorkbenchError

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEND_URL = "https://api.bilibili.com/x/v2/dm/post"
MAX_RESPONSE = 64 * 1024
DANMAKU_FREQUENCY_LIMIT_CODE = 36703
ID = TypeAdapter(NumericId)


def _identifier(value: object) -> str:
    if type(value) is int:
        value = str(value)
    return ID.validate_python(value, strict=True)


def _profile_name(value: object) -> str | None:
    if isinstance(value, str):
        return " ".join(value.split())[:80] or None
    return None


def _avatar_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 1024:
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return None
    if (url.scheme in {"http", "https"} and url.port is None and not url.userinfo
            and url.host.endswith(".hdslb.com")):
        return str(url.copy_with(scheme="https"))
    return None


def _aid(bvid: str) -> int:
    from bilibili_api.video import Video

    return Video(bvid=bvid).get_aid()


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        if value.isascii() and value.isdecimal():
            return min(86_400, int(value))
        wait = parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
        return min(86_400, max(0, ceil(wait.total_seconds())))
    except (TypeError, ValueError, OverflowError):
        return None


def _send_error_message(payload: dict | None, credential: BilibiliCredential, text: str) -> str | None:
    if payload is None or payload["code"] == 0:
        return None
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = payload.get("msg")
    if not isinstance(message, str):
        return None
    sensitive = [text]
    sensitive.extend(value.get_secret_value() for value in (credential.sessdata, credential.bili_jct, credential.buvid3)
                     if value is not None)
    for value in sorted(filter(None, sensitive), key=len, reverse=True):
        message = message.replace(value, "［已隐藏］")
    message = message.encode("utf-8", errors="replace").decode("utf-8")
    return " ".join(message.split())[:256] or None


class BilibiliGateway:
    """Use the 17.4.2 request contract. Each send issues exactly one POST with no redirects."""

    def __init__(self, client: httpx.AsyncClient, connection: SendConnection | None = None):
        self.client = client
        self.connection = connection

    async def _request(
        self, method: str, url: str, credential: BilibiliCredential, data: dict | None = None,
        *, referer: str = "https://www.bilibili.com/",
    ) -> tuple[int, int | None, dict | None]:
        cookies = [
            ("SESSDATA", credential.sessdata), ("bili_jct", credential.bili_jct), ("buvid3", credential.buvid3),
        ]
        request = httpx.Request(method, url, data=data, headers={
            "Cookie": "; ".join(f"{name}={value.get_secret_value()}" for name, value in cookies if value is not None),
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "DanmakuWorkbench/0.1",
        })
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            status = response.status_code
            retry_after = _retry_after(response.headers.get("Retry-After"))
            if status != 200:
                return status, retry_after, None
            body = bytearray()
            async for chunk in response.aiter_bytes(8192):
                if len(body) + len(chunk) > MAX_RESPONSE:
                    return status, retry_after, None
                body.extend(chunk)
            try:
                payload = json.loads(body)
            except (UnicodeError, ValueError):
                return status, retry_after, None
            if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                return status, retry_after, None
            return status, retry_after, payload
        finally:
            await response.aclose()

    async def account_id(self, credential: BilibiliCredential) -> str:
        return (await self.profile(credential)).account_id

    async def profile(self, credential: BilibiliCredential) -> BilibiliProfile:
        try:
            async with asyncio.timeout(10):
                status, _, payload = await self._request("GET", NAV_URL, credential)
        except (httpx.HTTPError, TimeoutError):
            raise WorkbenchError(503, "bilibili_unavailable", "无法连接 B 站验证账号", True) from None
        account = payload.get("data") if payload else None
        if status == 200 and payload and payload["code"] == 0:
            if isinstance(account, dict) and account.get("isLogin") is True:
                try:
                    level_info = account.get("level_info")
                    level = level_info.get("current_level") if isinstance(level_info, dict) else None
                    return BilibiliProfile(account_id=_identifier(account.get("mid")),
                                           name=_profile_name(account.get("uname")),
                                           avatar_url=_avatar_url(account.get("face")),
                                           level=level if type(level) is int and level >= 0 else None)
                except ValidationError:
                    raise WorkbenchError(503, "bilibili_response_invalid", "B 站账号响应格式不受支持") from None
            if isinstance(account, dict) and account.get("isLogin") is False:
                raise WorkbenchError(409, "bilibili_credential_rejected", "B 站未确认登录，请检查凭据")
            raise WorkbenchError(503, "bilibili_response_invalid", "B 站账号响应格式不受支持")
        if status == 401 or (payload and payload["code"] in {-101, -111}):
            raise WorkbenchError(409, "bilibili_credential_rejected", "B 站未确认登录，请检查凭据")
        if status in {403, 412} or (payload and payload["code"] in {-403, -412, -352}):
            raise WorkbenchError(403, "bilibili_access_denied", "B 站拒绝账号验证")
        raise WorkbenchError(503, "bilibili_unavailable", "暂时无法验证 B 站账号", True)

    async def post(self, credential: BilibiliCredential, work: SendWork) -> SendResult:
        assert work.target_bvid is not None and work.item.color_rgb is not None
        try:
            aid = _aid(work.target_bvid)
        except ImportError:
            return SendResult(outcome="blocked", error_code="bilibili_sdk_missing")
        except (ValueError, IndexError):
            return SendResult(outcome="rejected", error_code="bilibili_video_unavailable")
        csrf = credential.bili_jct.get_secret_value()
        data = {
            "type": 1, "oid": work.target_cid, "aid": aid, "bvid": work.target_bvid, "msg": work.item.text,
            "progress": work.item.time_ms, "color": work.item.color_rgb, "fontsize": work.item.font_size,
            "pool": 0, "mode": work.item.mode, "plat": 1, "csrf": csrf, "csrf_token": csrf,
        }
        if work.item.checkbox_type:
            data["checkbox_type"] = work.item.checkbox_type
        if self.connection is not None and not self.connection.ready.is_set():
            return SendResult(outcome="deferred")
        try:
            status, wait, payload = await self._request(
                "POST", SEND_URL, credential, data, referer=f"https://www.bilibili.com/video/{work.target_bvid}",
            )
        except httpx.HTTPError:
            if self.connection is not None:
                self.connection.transport_failed()
            return SendResult(outcome="unknown")
        code = payload["code"] if payload else None
        diagnostic = BilibiliSendDiagnostic(
            http_status=status, code=code or None, message=_send_error_message(payload, credential, work.item.text),
        )
        if status == 429 or code in {-509, DANMAKU_FREQUENCY_LIMIT_CODE}:
            return SendResult(outcome="limited", retry_after_seconds=wait, bilibili_error=diagnostic)
        if status == 401 or code in {-101, -111}:
            return SendResult(outcome="blocked", error_code="bilibili_credential_rejected", bilibili_error=diagnostic)
        if status in {403, 412} or code in {-403, -412, -352}:
            return SendResult(outcome="blocked", error_code="bilibili_access_denied", bilibili_error=diagnostic)
        if status == 404 or code in {-404, 62002}:
            return SendResult(outcome="rejected", error_code="bilibili_video_unavailable", bilibili_error=diagnostic)
        if code == 0:
            result = payload.get("data")
            if isinstance(result, dict):
                try:
                    return SendResult(outcome="confirmed", receipt=_identifier(result.get("dmid_str") or result.get("dmid")))
                except ValidationError:
                    pass
        elif code is not None:
            return SendResult(outcome="rejected", error_code="bilibili_delivery_rejected", bilibili_error=diagnostic)
        return SendResult(outcome="unknown", bilibili_error=diagnostic)


class BilibiliSender:
    def __init__(self, settings: BilibiliSettings, gateway: BilibiliGateway):
        self.settings = settings
        self.gateway = gateway

    async def send(self, work: SendWork) -> SendResult:
        try:
            credential = await run_in_threadpool(
                self.settings.secret, work.account_ref, work.account_id, work.account_role,
            )
        except WorkbenchError as exc:
            code = exc.info.code
            if code not in {"bilibili_account_changed", "bilibili_credential_missing"}:
                code = "credential_store_unavailable"
            return SendResult(outcome="blocked", error_code=code)
        return await self.gateway.post(credential, work)
