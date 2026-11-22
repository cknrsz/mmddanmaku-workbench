from collections.abc import Awaitable, Callable

from starlette.concurrency import run_in_threadpool

from danmaku.application.download.ports import DownloadSource
from danmaku.application.ports import UnitOfWork
from danmaku.application.tasks.service import BatchError
from danmaku.domain.inputs import DownloadMode, ProjectParts, SEGMENT_MS, SourceBatch, VideoPart
from danmaku.domain.workbench import ProjectCreate, TaskRecord, WorkbenchError


def select_part(parts: list[VideoPart], page: int) -> VideoPart:
    for part in parts:
        if part.page == page:
            return part
    raise WorkbenchError(422, "video_part_required", "请选择此视频实际存在的分 P")


class Download:
    def __init__(
        self, database: UnitOfWork, mock: DownloadSource, live: DownloadSource,
        legacy_batch: Callable[[TaskRecord], Awaitable[None]],
        mock_part: Callable[[str, int], VideoPart],
        live_fetch_source: Callable[[str], DownloadSource] | None = None,
    ):
        self.database = database
        self.sources = {"mock": mock, "bilibili": live}
        self.legacy_batch = legacy_batch
        self.mock_part = mock_part
        self.live_fetch_source = live_fetch_source

    async def parts(self, mode: DownloadMode, bvid: str) -> list[VideoPart]:
        return await self.sources[mode].parts(bvid)

    async def resolve_project(self, mode: DownloadMode, command: ProjectCreate) -> tuple[ProjectParts, int]:
        source = None
        if command.source_bvid is not None and command.source_page is not None:
            source = select_part(await self.parts(mode, command.source_bvid), command.source_page)
        target_parts = await self.parts(mode, command.target_bvid)
        target = select_part(target_parts, command.target_page)
        return ProjectParts(mode=mode, source=source, target=target), len(target_parts)

    def _saved_inputs(self, record: TaskRecord):
        with self.database() as repository:
            return (repository.snapshot(record.task.project_id, record.task.id),
                    repository.video_parts(record.task.project_id))

    async def execute(self, record: TaskRecord) -> SourceBatch | None:
        mode = record.inputs.mode
        cid = record.inputs.source_cid
        source = self.sources[mode]
        step = None
        try:
            snapshot, parts = await run_in_threadpool(self._saved_inputs, record)
            if mode == "bilibili" and (snapshot is not None or cid is not None):
                step = (f"下载弹幕第 {snapshot.completed_segments + 1}/{snapshot.segment_count} 段"
                        if snapshot is not None else "读取弹幕分段数")
                if self.live_fetch_source is not None:
                    source = await run_in_threadpool(self.live_fetch_source, record.created_by)
            if snapshot is not None:
                return await source.fetch(snapshot.part, snapshot.completed_segments, snapshot.segment_count)
            if cid is not None:
                return await source.plan_cid(cid)
            if mode == "mock":
                if record.task.checkpoint.completed_batches:
                    await self.legacy_batch(record)
                    return None
                part = self.mock_part(record.source_bvid, record.source_page)
                return await source.fetch(part, 0, 10)
            if record.inputs.source_bvid is None and parts and parts.mode == "bilibili" and parts.source:
                part = parts.source
            else:
                step = "读取源视频分 P"
                part = select_part(await source.parts(record.source_bvid), record.source_page)
            return SourceBatch(part=part, segment_count=(part.duration_ms + SEGMENT_MS - 1) // SEGMENT_MS)
        except WorkbenchError as exc:
            if step:
                identifier = f"CID {cid}" if cid is not None else f"{record.source_bvid} P{record.source_page}"
                location = f"{step}（{identifier}）"
                raise BatchError(exc.info.model_copy(update={"message": f"{exc.info.message}\n{location}"})) from None
            raise BatchError(exc.info) from None
