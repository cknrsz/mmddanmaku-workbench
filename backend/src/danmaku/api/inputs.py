from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from danmaku.api.workbench import Cursor, Limit, ReadSession, WriteSession
from danmaku.domain.inputs import (
    MAX_SUBTITLE_BYTES, DownloadMode, ProjectInputs, ReviewReady, Snapshot, SnapshotItem, SnapshotMerge,
    SnapshotSelection, SubtitleEncoding, SubtitleFormat, SubtitleLanguage, SubtitleVersion, VideoPart, Workflow,
)
from danmaku.domain.workbench import Bvid, ErrorResponse, Page, ResourceId, WorkbenchError

router = APIRouter(responses={422: {"model": ErrorResponse}})


@router.get("/videos/{bvid}/parts")
async def video_parts(
    request: Request, _session: ReadSession, bvid: Bvid, source: DownloadMode = "mock",
) -> list[VideoPart]:
    return await request.app.state.download.parts(source, bvid)


@router.get("/projects/{project_id}/inputs")
def project_inputs(request: Request, session: ReadSession, project_id: ResourceId) -> ProjectInputs:
    return request.app.state.projects.inputs(session.principal, project_id)


@router.get("/projects/{project_id}/snapshots/{snapshot_id}/items")
def snapshot_items(
    request: Request, session: ReadSession, project_id: ResourceId, snapshot_id: ResourceId,
    cursor: Cursor = None, limit: Limit = 100,
) -> Page[SnapshotItem]:
    return request.app.state.projects.snapshot_items(session.principal, project_id, snapshot_id, cursor, limit)


@router.post("/projects/{project_id}/snapshots/merge", responses={409: {"model": ErrorResponse}})
def merge_snapshots(request: Request, session: WriteSession, project_id: ResourceId, command: SnapshotMerge) -> Snapshot:
    return request.app.state.projects.merge_snapshots(session.principal, project_id, command)


@router.put("/projects/{project_id}/workflow/snapshot")
def select_snapshot(
    request: Request, session: WriteSession, project_id: ResourceId, command: SnapshotSelection,
) -> Workflow:
    return request.app.state.projects.select_snapshot(session.principal, project_id, command.snapshot_id)


@router.put("/projects/{project_id}/workflow/review", responses={409: {"model": ErrorResponse}})
def mark_review_ready(request: Request, session: WriteSession, project_id: ResourceId, command: ReviewReady) -> Workflow:
    return request.app.state.projects.mark_review_ready(session.principal, project_id, command)


@router.delete("/projects/{project_id}/workflow/subtitles/{language}")
def clear_subtitle_language(
    request: Request, session: WriteSession, project_id: ResourceId, language: SubtitleLanguage,
) -> Workflow:
    return request.app.state.subtitles.clear_language(session.principal, project_id, language)


@router.post(
    "/projects/{project_id}/subtitles", status_code=201,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    }}},
)
async def upload_subtitle(
    request: Request, session: WriteSession, project_id: ResourceId,
    format: Annotated[SubtitleFormat, Query()], encoding: SubtitleEncoding = "utf-8",
    filename: Annotated[str | None, Query(max_length=255)] = None,
    language: SubtitleLanguage | None = None,
) -> SubtitleVersion:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/octet-stream":
        raise WorkbenchError(415, "subtitle_content_type", "请上传 ASS、SRT 或 TXT 文件字节")
    await run_in_threadpool(request.app.state.projects.require, session.principal, project_id)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_SUBTITLE_BYTES:
            raise WorkbenchError(413, "subtitle_too_large", "字幕文件超过 2 MiB")
        content.extend(chunk)
    return await run_in_threadpool(
        request.app.state.subtitles.upload, session.principal, project_id, bytes(content), format, encoding, filename,
        language,
    )


@router.delete("/projects/{project_id}/subtitles/{version_id}", status_code=204, responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
})
def delete_subtitle(request: Request, session: WriteSession, project_id: ResourceId, version_id: ResourceId) -> None:
    request.app.state.subtitles.delete(session.principal, project_id, version_id)
