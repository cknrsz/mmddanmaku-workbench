from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from binascii import Error
from json import dumps, loads
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from danmaku.domain.workbench import Page, WorkbenchError


class Identified(Protocol):
    id: str


def decode_cursor(
    scope: list[str], cursor: str | None, *, valid_id: Callable[[str], bool] = lambda value: UUID(value).hex == value,
) -> str:
    if cursor is None:
        return ""
    try:
        stored_scope, after = loads(b64decode(cursor, altchars=b"-_", validate=True))
        if stored_scope == scope and isinstance(after, str) and valid_id(after):
            return after
    except (Error, ValueError, TypeError, UnicodeError):
        pass
    raise WorkbenchError(400, "invalid_cursor", "分页游标无效")


def encode_cursor(scope: list[str], after: str) -> str:
    return urlsafe_b64encode(dumps([scope, after]).encode()).decode()


def valid_creation_cursor(value: str) -> bool:
    return value.isascii() and value.isdecimal() and len(value) <= 19 and 0 < int(value) < 2**63


def page[T: Identified](scope: list[str], items: list[T], limit: int) -> Page[T]:
    result = items[:limit]
    cursor = None
    if len(items) > limit:
        cursor = encode_cursor(scope, result[-1].id)
    return Page(items=result, next_cursor=cursor)

