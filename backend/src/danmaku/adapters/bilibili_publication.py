from collections.abc import AsyncIterator
import json
from time import time

import httpx
from pydantic import ValidationError

from danmaku.adapters.bilibili_login import COOKIE_FIELDS, HEADERS, private_request
from danmaku.domain.bilibili_login import BilibiliLoginSecret
from danmaku.domain.inputs import VideoPart
from danmaku.domain.workbench import WorkbenchError

MEMBER = "https://member.bilibili.com"
PUBLIC = "https://api.bilibili.com/x/web-interface/view"
ARCHIVES = f"{MEMBER}/x/web/archives"
ARCHIVE_DETAIL = f"{MEMBER}/x/vupre/web/archive/view"
PAGE_SIZE = 20
MAX_RESPONSE = 4 * 1024 * 1024


def _upstream_error() -> WorkbenchError:
    return WorkbenchError(502, "bilibili_publication_unavailable", "暂时无法读取 B 站稿件，请稍后重试", True)


def _positive(value: object) -> int:
    if type(value) is int:
        return max(0, value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        return int(value)
    return 0


def _publish_at(archive: dict) -> int | None:
    timestamp = _positive(archive.get("dtime"))
    return timestamp if 0 < timestamp < 253402300800 else None


class BilibiliPublication:
    """Read creator metadata using the existing login and verify visibility anonymously."""

    def __init__(self, client: httpx.AsyncClient, diagnostic=None):
        self.client = client
        self.diagnostic = diagnostic or (lambda **fields: None)

    async def _get(self, url: str, params: dict, credential: BilibiliLoginSecret | None = None) -> dict:
        headers = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json", "Cache-Control": "no-cache"}
        if credential is not None:
            cookies = {name: getattr(credential, field) for name, field in COOKIE_FIELDS.items()}
            headers["Cookie"] = "; ".join(
                f"{name}={value.get_secret_value()}" for name, value in cookies.items() if value is not None
            )
            headers["Referer"] = f"{MEMBER}/platform/upload-manager/article"
        else:
            headers["Referer"] = HEADERS["Referer"]
        token = private_request.set(True)
        try:
            # An explicit request keeps the client's cookie jar out of the public probe.
            request = httpx.Request("GET", url, params=params, headers=headers)
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(8192):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        raise _upstream_error()
                    body.extend(chunk)
                payload = json.loads(body)
            finally:
                await response.aclose()
        except (httpx.HTTPError, ValueError) as exc:
            self.diagnostic(event="request_error", endpoint="public" if credential is None else "creator",
                            error=type(exc).__name__,
                            http_status=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
            raise _upstream_error() from None
        finally:
            private_request.reset(token)
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise _upstream_error()
        if credential is not None and payload["code"] in {-101, -111}:
            raise WorkbenchError(409, "bilibili_credential_rejected", "请重新登录 B 站主账号")
        return payload

    async def _detail(self, credential: BilibiliLoginSecret, bvid: str) -> dict:
        return await self._creator(ARCHIVE_DETAIL, {
            "bvid": bvid, "topic_grey": 1, "t": int(time() * 1000),
        }, credential)

    async def _creator(self, url: str, params: dict, credential: BilibiliLoginSecret) -> dict:
        result = await self._get(url, params, credential)
        if result["code"] or not isinstance(result.get("data"), dict):
            raise _upstream_error()
        return result["data"]

    async def _archives(self, credential: BilibiliLoginSecret) -> AsyncIterator[dict]:
        previous = None
        for page in range(1, 501):
            data = await self._creator(ARCHIVES, {
                "status": "is_pubing,pubed,not_pubed", "pn": page, "ps": PAGE_SIZE,
                "coop": 1, "interactive": 1,
            }, credential)
            rows = data.get("arc_audits")
            if rows is None and ("arc_audits" in data or not data):
                return
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or rows == previous:
                raise _upstream_error()
            for row in rows:
                yield row
            total = _positive((data.get("page") or {}).get("count")) if isinstance(data.get("page"), dict) else 0
            if len(rows) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
                return
            previous = rows
        raise _upstream_error()

    async def scheduled_parts(
        self, credential: BilibiliLoginSecret, bvid: str | None = None,
    ) -> list[tuple[int, VideoPart]]:
        candidates = []
        async for row in self._archives(credential):
            archive = row.get("Archive")
            if not isinstance(archive, dict) or (bvid is not None and bvid != archive.get("bvid")):
                continue
            if archive.get("state") != -40:
                continue
            archive_bvid = archive.get("bvid")
            if not isinstance(archive_bvid, str):
                raise _upstream_error()
            publish_at = _publish_at(archive)
            raw_parts = row.get("Videos")
            valid_parts = self._has_parts(raw_parts)
            if not valid_parts or publish_at is None:
                detail = await self._detail(credential, archive_bvid)
                if not valid_parts:
                    raw_parts = detail.get("videos")
                if isinstance(detail.get("archive"), dict):
                    publish_at = _publish_at(detail["archive"]) or publish_at
            if publish_at is None or not isinstance(raw_parts, list) or not raw_parts:
                raise _upstream_error()
            candidates.extend((publish_at, part) for part in self._parts(archive, raw_parts))
            if bvid is not None:
                break
        return candidates

    @staticmethod
    def _has_parts(parts: object) -> bool:
        return isinstance(parts, list) and bool(parts) and all(
            isinstance(row, dict) and _positive(row.get("cid")) and _positive(row.get("duration")) for row in parts
        )

    @staticmethod
    def _parts(archive: dict, rows: list) -> list[VideoPart]:
        title = archive.get("title")
        try:
            parts = []
            for index, row in enumerate(rows, 1):
                page = _positive(row.get("index") or row.get("page")) or index
                duration = _positive(row.get("duration"))
                if len(rows) == 1 and not duration:
                    duration = _positive(archive.get("duration"))
                parts.append(VideoPart(
                    bvid=archive["bvid"], page=page, cid=str(_positive(row.get("cid"))),
                    duration_ms=duration * 1000, title=row.get("title") or row.get("part") or title,
                    video_title=title,
                ))
            if len({part.page for part in parts}) != len(parts):
                raise _upstream_error()
            return parts
        except (AttributeError, KeyError, ValidationError):
            raise _upstream_error() from None

    async def published(self, target: VideoPart) -> bool:
        payload = await self._get(PUBLIC, {"bvid": target.bvid})
        data = payload.get("data")
        pages = data.get("pages") if isinstance(data, dict) else None
        self.diagnostic(
            event="public_response", bvid=target.bvid, target_page=target.page, target_cid=target.cid,
            code=payload["code"], response_bvid=data.get("bvid") if isinstance(data, dict) else None,
            state=data.get("state") if isinstance(data, dict) else None,
            pages=[{"page": row.get("page"), "cid": row.get("cid")} for row in pages if isinstance(row, dict)]
            if isinstance(pages, list) else None,
        )
        if payload["code"] in {-403, -404, 62002, 62004, 62012}:
            return False
        if payload["code"] or not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            raise _upstream_error()
        return data.get("bvid") == target.bvid and data.get("state") == 0 and any(
            isinstance(row, dict) and _positive(row.get("page")) == target.page
            and str(_positive(row.get("cid"))) == target.cid for row in data["pages"]
        )

    async def publication_time(self, credential: BilibiliLoginSecret, bvid: str) -> int | None:
        result = await self._detail(credential, bvid)
        archive = result.get("archive")
        publish_at = _publish_at(archive) if isinstance(archive, dict) else None
        if publish_at is not None:
            return publish_at
        async for row in self._archives(credential):
            archive = row.get("Archive")
            if isinstance(archive, dict) and archive.get("bvid") == bvid:
                return _publish_at(archive)
        return None
