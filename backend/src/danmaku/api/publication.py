from fastapi import APIRouter, Request, Response

from danmaku.api.workbench import ReadSession, WriteSession
from danmaku.domain.publication import OverduePublication, PreloadPart, PreloadProject
from danmaku.domain.workbench import ErrorResponse, Project

router = APIRouter(responses={422: {"model": ErrorResponse}})


@router.get("/preload/parts")
async def preload_parts(request: Request, session: ReadSession, response: Response) -> list[PreloadPart]:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.preload.candidates(session.principal)


@router.post("/preload/projects", status_code=201)
async def preload_project(request: Request, session: WriteSession, command: PreloadProject) -> Project:
    return await request.app.state.preload.create(session.principal, command)


@router.get("/preload/overdue")
def overdue_publication(request: Request, session: ReadSession, response: Response) -> str | None:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.preload.overdue(session.principal)


@router.post("/preload/overdue", status_code=204)
async def decide_publication(request: Request, session: WriteSession, command: OverduePublication) -> Response:
    await request.app.state.preload.decide(session.principal, command)
    return Response(status_code=204)
