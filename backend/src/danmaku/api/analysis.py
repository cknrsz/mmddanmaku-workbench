from fastapi import APIRouter, Request, Response

from danmaku.api.workbench import Cursor, Limit, ReadSession, WriteSession
from danmaku.domain.analysis import AnalysisRun, Classification, ModelConfigInput, ModelSettings
from danmaku.domain.workbench import ErrorResponse, Page, ResourceId, Task

router = APIRouter(responses={422: {"model": ErrorResponse}})


@router.post("/tasks/{task_id}/stop", status_code=202)
async def stop_analysis(request: Request, session: WriteSession, task_id: ResourceId) -> Task:
    return await request.app.state.analysis.stop(session.principal, task_id)


@router.get("/settings/llm")
def model_settings(request: Request, response: Response, session: ReadSession) -> ModelSettings:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.llm_settings.get(session.principal)


@router.put("/settings/llm")
def configure_model(
    request: Request, response: Response, session: WriteSession, command: ModelConfigInput,
) -> ModelSettings:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.llm_settings.put(session.principal, command)


@router.get("/projects/{project_id}/runs")
def list_runs(
    request: Request, session: ReadSession, project_id: ResourceId, cursor: Cursor = None, limit: Limit = 100,
) -> Page[AnalysisRun]:
    return request.app.state.analysis.list(session.principal, project_id, cursor, limit)


@router.get("/projects/{project_id}/runs/{run_id}/classifications")
def classifications(
    request: Request, session: ReadSession, project_id: ResourceId, run_id: ResourceId,
    cursor: Cursor = None, limit: Limit = 100,
) -> Page[Classification]:
    return request.app.state.analysis.results(session.principal, project_id, run_id, cursor, limit)


@router.get("/projects/{project_id}/runs/{run_id}")
def get_run(request: Request, session: ReadSession, project_id: ResourceId, run_id: ResourceId) -> AnalysisRun:
    return request.app.state.analysis.get(session.principal, project_id, run_id)
