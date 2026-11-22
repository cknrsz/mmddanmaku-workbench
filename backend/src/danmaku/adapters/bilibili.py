from contextlib import asynccontextmanager
import asyncio
from json import loads
from urllib.parse import quote, unquote

from danmaku.adapters.bilibili_protobuf import segment_count, segment_items
from danmaku.domain.inputs import CidPart, SourceBatch, SourceItem, SourcePart, VideoPart
from danmaku.domain.sending import BilibiliCredential
from danmaku.domain.workbench import WorkbenchError


def platform_error(code: int | None, detail: str = "") -> WorkbenchError:
    suffix = f"\n{detail}" if detail else ""
    if code in {429, -509}:
        return WorkbenchError(503, "bilibili_rate_limited", "B 站请求限流" + suffix, True)
    if code in {401, 403, 412, -101, -111, -403, -412, -352}:
        return WorkbenchError(403, "bilibili_access_denied", "B 站拒绝访问或要求验证" + suffix)
    if code in {404, -404, 62002}:
        return WorkbenchError(404, "bilibili_video_unavailable", "B 站视频不可用" + suffix)
    return WorkbenchError(503, "bilibili_unavailable", "B 站服务暂不可用" + suffix, True)


class BilibiliSource:
    def __init__(self, credential: BilibiliCredential | None = None):
        self.credential = credential

    def _api_message(self, message: object) -> str:
        if not isinstance(message, str):
            return ""
        if self.credential is not None:
            secrets = set()
            for _, value in self.credential:
                if value is not None:
                    secret = value.get_secret_value()
                    secrets.update((secret, quote(secret, safe=""), unquote(secret)))
            for secret in sorted(filter(None, secrets), key=len, reverse=True):
                message = message.replace(secret, "［已隐藏］")
        message = message.encode("utf-8", errors="replace").decode("utf-8")
        return " ".join(message.split())[:256]

    @asynccontextmanager
    async def _sdk(self):
        try:
            from bilibili_api import Credential, select_client
            from bilibili_api.exceptions import (
                ApiException, ArgsException, DanmakuClosedException, NetworkException,
                ResponseCodeException, ResponseException, WbiRetryTimesExceedException,
            )
            from httpx import TransportError
        except ImportError as exc:
            raise WorkbenchError(503, "bilibili_sdk_missing", f"请安装 B 站下载依赖\n{type(exc).__name__}") from None
        try:
            select_client("httpx")
            async with asyncio.timeout(20):
                yield Credential(**{
                    name: value.get_secret_value()
                    for name, value in (self.credential or ()) if value is not None
                })
        except (NetworkException, ResponseCodeException) as exc:
            # get_danmakus wraps request exceptions in NetworkException(-1, ...).
            cause = (exc.__cause__ or exc.__context__) if isinstance(exc, NetworkException) else None
            original = cause or exc
            details = [f"异常类型 {type(original).__name__}"]
            code = None
            if isinstance(original, ResponseCodeException):
                if type(original.code) is int:
                    code = original.code
                    details.append(f"B 站错误码 {code}")
                if message := self._api_message(original.msg):
                    details.append(f"B 站提示 {message}")
            else:
                response = getattr(original, "response", None)
                status = getattr(response, "status_code", getattr(original, "status", getattr(exc, "status", None)))
                if type(status) is int and 100 <= status <= 599:
                    code = status
                    details.append(f"B 站 HTTP {status}")
            raise platform_error(code, "\n".join(details)) from None
        except TimeoutError:
            raise platform_error(None, "B 站请求超过 20 秒（TimeoutError）") from None
        except (TransportError, WbiRetryTimesExceedException) as exc:
            raise platform_error(None, f"异常类型 {type(exc).__name__}") from None
        except DanmakuClosedException:
            raise WorkbenchError(403, "bilibili_danmaku_closed", "此分 P 已关闭弹幕") from None
        except ArgsException:
            raise WorkbenchError(422, "bilibili_invalid_video", "B 站视频或分 P 参数无效") from None
        except (ResponseException, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise WorkbenchError(
                503, "bilibili_response_invalid", f"B 站响应格式不受支持\n异常类型 {type(exc).__name__}",
            ) from None
        except ApiException as exc:
            raise WorkbenchError(
                503, "bilibili_api_error", f"B 站 SDK 无法完成读取\n异常类型 {type(exc).__name__}",
            ) from None

    @asynccontextmanager
    async def _video(self, bvid: str):
        async with self._sdk() as credential:
            from bilibili_api import video
            yield video.Video(bvid=bvid, credential=credential)

    async def _cid_request(self, credential, endpoint: str, cid: str, **params: int) -> bytes:
        from bilibili_api.utils.network import HEADERS, get_client

        response = await get_client().request(
            method="GET", url=f"https://api.bilibili.com/x/v2/dm/web/{endpoint}",
            params={"type": 1, "oid": cid, **params}, headers=HEADERS.copy(),
            cookies={name: value for name, value in credential.get_cookies().items() if value},
            allow_redirects=False,
        )
        # Bilibili can return a bodyless 304 for an unavailable segment on an unconditional GET.
        if endpoint == "seg.so" and response.code == 304 and not response.raw:
            return b""
        if response.code != 200:
            raise platform_error(response.code, f"B 站 HTTP {response.code}")
        if response.raw.startswith(b"{") or "json" in response.headers.get("content-type", ""):
            payload = loads(response.raw)
            code = payload.get("code")
            if type(code) is int and code != 0:
                raise platform_error(code, f"B 站错误码 {code}")
            raise ValueError("expected protobuf")
        return response.raw

    async def plan_cid(self, cid: str) -> SourceBatch:
        async with self._sdk() as credential:
            count = segment_count(await self._cid_request(credential, "view", cid))
            if count is None:
                raise WorkbenchError(503, "bilibili_segment_count_unknown", "B 站未返回此 CID 的弹幕分段数", True)
            return SourceBatch(part=CidPart(cid=cid), segment_count=count)

    async def parts(self, bvid: str) -> list[VideoPart]:
        async with self._video(bvid) as video:
            info = await video.get_info()
            parts = []
            for part in info["pages"]:
                cid = part["cid"]
                playback = await video.get_download_url(cid=int(cid))
                parts.append(VideoPart(bvid=bvid, page=part["page"], cid=str(cid),
                                       duration_ms=playback["timelength"], title=part["part"],
                                       video_title=info.get("title")))
            return parts

    async def fetch(self, part: SourcePart, segment: int, segment_count: int) -> SourceBatch:
        if isinstance(part, CidPart):
            async with self._sdk() as credential:
                data = await self._cid_request(credential, "seg.so", part.cid, segment_index=segment + 1)
                if data == b"\x10\x01":
                    raise WorkbenchError(403, "bilibili_danmaku_closed", "此分 P 已关闭弹幕")
                return SourceBatch(part=part, segment_count=segment_count, segment=segment, items=segment_items(data))
        async with self._video(part.bvid) as video:
            entries = await video.get_danmakus(cid=int(part.cid), from_seg=segment, to_seg=segment)
            return SourceBatch(part=part, segment_count=segment_count, segment=segment, items=[
                SourceItem(
                    platform_id=entry.id_str or (str(entry.id_) if entry.id_ > 0 else None),
                    text=entry.text, time_ms=round(entry.dm_time * 1000),
                    color="special" if entry.color == "special" else int(entry.color, 16),
                    font_size=entry.font_size, mode=entry.mode,
                )
                for entry in entries
            ])
