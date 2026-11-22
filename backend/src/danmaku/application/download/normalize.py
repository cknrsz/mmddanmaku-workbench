from danmaku.domain.inputs import NormalizedItem, SourceBatch, SourceItem


def normalize(batch: SourceBatch) -> list[tuple[SourceItem, NormalizedItem]]:
    items = []
    for index, raw in enumerate(batch.items):
        if not raw.text.strip():
            continue
        key = f"id:{raw.platform_id}" if raw.platform_id else f"segment:{batch.segment}:{index}"
        color = raw.color if isinstance(raw.color, int) else None
        diagnostic = None
        if batch.part.duration_ms is not None and raw.time_ms >= batch.part.duration_ms:
            diagnostic = "outside_video"
        elif color is None:
            diagnostic = "unsupported_color"
        items.append((raw, NormalizedItem(
            source_key=key, text=raw.text, time_ms=raw.time_ms, color_rgb=color,
            font_size=raw.font_size, mode=raw.mode, diagnostic=diagnostic,
        )))
    return items
