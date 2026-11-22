"""Decode the CID-only view and segment responses used by the Bilibili adapter."""

from collections.abc import Iterator

from danmaku.domain.inputs import SourceItem


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if offset == len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128 and value < 2**64:
            return value, offset
    raise ValueError("invalid protobuf varint")


def _fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        field, wire = divmod(tag, 8)
        if not field:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, offset = _varint(data, offset)
            yield field, value
            continue
        if wire == 2:
            size, offset = _varint(data, offset)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError("unsupported protobuf wire type")
        end = offset + size
        if end > len(data):
            raise ValueError("truncated protobuf field")
        if wire == 2:
            yield field, data[offset:end]
        offset = end


def segment_count(data: bytes) -> int | None:
    for field, payload in _fields(data):
        if field == 4 and isinstance(payload, bytes):
            total = dict(_fields(payload)).get(2)
            if isinstance(total, int) and 0 < total <= 2**31 - 1:
                return total
    return None


def segment_items(data: bytes) -> list[SourceItem]:
    items = []
    for field, payload in _fields(data):
        if field != 1 or not isinstance(payload, bytes):
            continue
        entry = dict(_fields(payload))
        text = entry.get(7)
        if not isinstance(text, bytes) or not text.strip():
            continue
        string_id = entry.get(12)
        number_id = entry.get(1, 0)
        platform_id = None
        if isinstance(string_id, bytes) and string_id.isdigit() and string_id.lstrip(b"0"):
            platform_id = string_id.decode("ascii")
        elif isinstance(number_id, int) and 0 < number_id < 2**63:
            platform_id = str(number_id)
        color = entry.get(5, 0)
        items.append(SourceItem(
            platform_id=platform_id, text=text.decode("utf-8"), time_ms=entry.get(2, 0),
            mode=entry.get(3, 1), font_size=entry.get(4, 25),
            color="special" if entry.get(24, 0) or color == 60001 else color,
        ))
    return items
