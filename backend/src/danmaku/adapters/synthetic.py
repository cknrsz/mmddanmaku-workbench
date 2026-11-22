import asyncio
from hashlib import sha256

from danmaku.domain.inputs import SEGMENT_MS, SourceBatch, SourceItem, SourcePart, VideoPart
from danmaku.domain.workbench import TaskRecord, WorkbenchError


async def synthetic_download_batch(_record: TaskRecord) -> None:
    """Simulate one successful batch with cancellable I/O latency."""
    await asyncio.sleep(0.75)


def synthetic_part(bvid: str, page: int) -> VideoPart:
    digest = sha256(f"{bvid}:{page}".encode()).digest()
    cid = int.from_bytes(digest[:8]) or 1
    return VideoPart(bvid=bvid, page=page, cid=str(cid),
                     title=f"合成分 P {page}", video_title=f"合成视频 {bvid}", duration_ms=3_600_000)


class SyntheticSource:
    async def parts(self, bvid: str) -> list[VideoPart]:
        return [synthetic_part(bvid, page) for page in (1, 2)]

    async def plan_cid(self, cid: str) -> SourceBatch:
        raise WorkbenchError(422, "invalid_request", "CID 下载需要 B 站模式")

    async def fetch(self, part: SourcePart, segment: int, segment_count: int) -> SourceBatch:
        await asyncio.sleep(0.75)
        return SourceBatch(part=part, segment_count=segment_count, segment=segment, items=[
            SourceItem(platform_id=f"synthetic-{segment}-{index}", text=f"合成弹幕 {index + 1}",
                       time_ms=segment * SEGMENT_MS + index * 1000, color=0xFFFFFF, font_size=25, mode=1)
            for index in range(5)
        ])
