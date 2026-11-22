from typing import Protocol

from danmaku.domain.inputs import SourceBatch, SourcePart, VideoPart


class DownloadSource(Protocol):
    """Resolve 1-based parts and fetch one 0-based current segment by fixed CID.

    Failures are sanitized WorkbenchError values. A retry requests the same segment.
    Empty segment results can advance coverage.
    """

    async def parts(self, bvid: str) -> list[VideoPart]: ...
    async def plan_cid(self, cid: str) -> SourceBatch: ...
    async def fetch(self, part: SourcePart, segment: int, segment_count: int) -> SourceBatch: ...
