import asyncio
from collections.abc import AsyncIterator
from hmac import compare_digest
from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from danmaku.application.classification.console import ConsoleRun, ConsoleUpdate
from danmaku.application.sessions import LocalSession
from danmaku.domain.inputs import DownloadMode
from danmaku.domain.workbench import (
    ErrorResponse, Page, Project, ProjectCompletion, ProjectCreate, ProjectDetail, ProjectRename, ResourceId,
    SendCreate, SendRequest, SessionInfo, Task, TaskControl, TaskKind, TaskRequest, TaskState, TERMINAL_STATES, WorkbenchError,
)

COOKIE_NAME = "danmaku_session"
router = APIRouter(responses={422: {"model": ErrorResponse}})
Cursor = Annotated[str | None, Query(max_length=1024)]
Limit = Annotated[int, Query(ge=1, le=500)]


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin != f"http://{request.headers.get('host')}" or origin not in request.app.state.allowed_origins:
        raise WorkbenchError(403, "origin_rejected", "请求来源不受信任")


def get_session(request: Request) -> LocalSession:
    return request.app.state.sessions.authenticate(request.cookies.get(COOKIE_NAME))


ReadSession = Annotated[LocalSession, Depends(get_session)]


def check_write(request: Request, session: ReadSession) -> LocalSession:
    check_origin(request)
    token = request.headers.get("x-csrf-token", "")
    if not compare_digest(token.encode(), session.csrf_token.encode()):
        raise WorkbenchError(403, "csrf_rejected", "会话校验失败")
    return session


WriteSession = Annotated[LocalSession, Depends(check_write)]


@router.post("/session", status_code=201, dependencies=[Depends(check_origin)])
def open_session(request: Request, response: Response) -> SessionInfo:
    try:
        local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if (not local or request.headers.get("x-danmaku-local") != "1"
            or request.headers.get("sec-fetch-site") not in {None, "same-origin"}):
        raise WorkbenchError(403, "local_session_rejected", "请通过本机工作台页面连接")
    sessions = request.app.state.sessions
    existing = request.cookies.get(COOKIE_NAME)
    token, session = sessions.open(existing)
    if token != existing:
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict",
                            max_age=sessions.session_seconds, path="/api/v1")
    response.headers["Cache-Control"] = "no-store"
    return session.info()


@router.get("/session")
def current_session(session: ReadSession, response: Response) -> SessionInfo:
    response.headers["Cache-Control"] = "no-store"
    return session.info()


@router.delete("/session", status_code=204)
def sign_out(request: Request, response: Response, _session: WriteSession) -> None:
    request.app.state.sessions.revoke(request.cookies[COOKIE_NAME])
    response.delete_cookie(COOKIE_NAME, path="/api/v1", httponly=True, samesite="strict")


@router.post("/projects", status_code=201, responses={409: {"model": ErrorResponse}})
async def create_project(
    request: Request, command: ProjectCreate, session: WriteSession, source: DownloadMode | None = None,
    confirm_duration_mismatch: bool = False,
) -> Project:
    parts, target_part_count = (None, None)
    if source:
        parts, target_part_count = await request.app.state.download.resolve_project(source, command)
    return await run_in_threadpool(
        request.app.state.projects.create, session.principal, command, parts,
        confirm_duration_mismatch=confirm_duration_mismatch,
        target_part_count=target_part_count,
    )


@router.get("/projects")
def list_projects(
    request: Request, session: ReadSession, cursor: Cursor = None, limit: Limit = 100,
) -> Page[Project]:
    return request.app.state.projects.list(session.principal, cursor, limit)


@router.get("/projects/{project_id}")
def get_project(request: Request, session: ReadSession, project_id: ResourceId) -> ProjectDetail:
    return request.app.state.projects.get(session.principal, project_id)


@router.patch("/projects/{project_id}", responses={404: {"model": ErrorResponse}})
def rename_project(request: Request, session: WriteSession, project_id: ResourceId, command: ProjectRename) -> Project:
    return request.app.state.projects.rename(session.principal, project_id, command)


@router.patch("/projects/{project_id}/completion", responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def set_project_completion(
    request: Request, session: WriteSession, project_id: ResourceId, command: ProjectCompletion,
) -> Project:
    return request.app.state.projects.set_completion(session.principal, project_id, command)


@router.delete("/projects/{project_id}", status_code=204, responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
})
def delete_project(request: Request, session: WriteSession, project_id: ResourceId) -> None:
    request.app.state.projects.delete(session.principal, project_id)


@router.post("/projects/{project_id}/tasks", status_code=202)
def create_task(
    request: Request, response: Response, session: WriteSession, project_id: ResourceId,
    command: TaskRequest,
    idempotency_key: Annotated[str, Header(pattern=r"^[!-~]{1,128}$")],
) -> Task:
    inputs = (SendCreate.model_validate(command.model_dump(exclude_unset=True) | {"mode": command.mode})
              if isinstance(command, SendRequest) else command)
    task = request.app.state.tasks.create(session.principal, project_id, idempotency_key, inputs)
    response.headers["Location"] = f"/api/v1/tasks/{task.id}"
    return task


@router.get("/projects/{project_id}/tasks")
def list_tasks(
    request: Request, session: ReadSession, project_id: ResourceId, cursor: Cursor = None,
    limit: Limit = 100, kind: TaskKind | None = None, state: TaskState | None = None,
) -> Page[Task]:
    return request.app.state.tasks.list(session.principal, project_id, cursor, limit, state, kind)


@router.get("/tasks/{task_id}")
def get_task(request: Request, session: ReadSession, task_id: ResourceId) -> Task:
    return request.app.state.tasks.get(session.principal, task_id)


@router.delete("/tasks/{task_id}", status_code=204, responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
})
def delete_task(request: Request, session: WriteSession, task_id: ResourceId) -> None:
    request.app.state.tasks.delete(session.principal, task_id)


@router.post("/tasks/{task_id}/pause", status_code=202)
def pause(request: Request, session: WriteSession, task_id: ResourceId, command: TaskControl) -> Task:
    return request.app.state.tasks.control(session.principal, task_id, command.expected_version, "pause")


@router.post("/tasks/{task_id}/resume", status_code=202)
def resume(request: Request, session: WriteSession, task_id: ResourceId, command: TaskControl) -> Task:
    return request.app.state.tasks.control(session.principal, task_id, command.expected_version, "resume")


def event_cursor(raw: str | None) -> int:
    if raw is None:
        return 0
    if len(raw) <= 16 and raw.isascii() and raw.isdecimal() and int(raw) < 2**53:
        return int(raw)
    raise WorkbenchError(400, "invalid_cursor", "事件游标无效")


@router.get(
    "/tasks/{task_id}/events", response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def task_events(
    request: Request, session: ReadSession, task_id: ResourceId, after: str | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
    analysis_console: bool = False,
) -> StreamingResponse:
    cursor = event_cursor(last_event_id if last_event_id is not None else after)
    tasks = request.app.state.tasks
    initial = await run_in_threadpool(tasks.replay, session.principal, task_id, cursor)
    if analysis_console and initial[0].kind != "classify":
        raise WorkbenchError(404, "analysis_not_found", "找不到分析任务")
    consoles = request.app.state.analysis.consoles if analysis_console else None

    async def stream() -> AsyncIterator[str]:
        latest, events = initial
        last = cursor
        heartbeat_seconds = 15
        loop = asyncio.get_running_loop()
        heartbeat = loop.time() + heartbeat_seconds
        last_run: ConsoleRun | None = None
        position: tuple[int, int] | None = None
        yield ": connected\n\n"
        while True:
            try:
                get_session(request)
            except WorkbenchError:
                return
            if consoles is not None:
                run = consoles.runs.get(task_id)
                if run is not last_run:
                    last_run = run
                    position = None
                update = None
                if run is not None:
                    update = run.update(position)
                    position = run.position
                elif position is None:
                    update = ConsoleUpdate(replace=True, reasoning="", output="")
                    position = (0, 0)
                if update is not None:
                    yield f"event: analysis.console\ndata: {update.model_dump_json()}\n\n"
            if (last > latest.version or
                    (last < latest.version and (not events or events[0].version != last + 1))):
                yield f"id: {latest.version}\nevent: reset\ndata: {latest.model_dump_json()}\n\n"
                last = latest.version
            else:
                for snapshot in events:
                    if snapshot.version != last + 1:
                        yield f"id: {latest.version}\nevent: reset\ndata: {latest.model_dump_json()}\n\n"
                        last = latest.version
                        break
                    yield (f"id: {snapshot.version}\nevent: task.updated\n"
                           f"data: {snapshot.model_dump_json()}\n\n")
                    last = snapshot.version
            if last == latest.version:
                if latest.state in TERMINAL_STATES:
                    if analysis_console:
                        yield "event: analysis.console.end\ndata: {}\n\n"
                    return
                if loop.time() >= heartbeat:
                    yield ": heartbeat\n\n"
                    heartbeat = loop.time() + heartbeat_seconds
                await asyncio.sleep(0.25)
            try:
                latest, events = await run_in_threadpool(tasks.replay, session.principal, task_id, last)
            except WorkbenchError as exc:
                if exc.info.retryable:
                    yield "retry: 1000\n\n"
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })

