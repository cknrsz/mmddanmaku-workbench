from fastapi import APIRouter, Request, Response

from danmaku.api.workbench import ReadSession, WriteSession
from danmaku.domain.settings import SettingsUpdate, SettingsView
from danmaku.domain.workbench import ErrorResponse

router = APIRouter(responses={422: {"model": ErrorResponse}})


@router.get("/settings")
def get_settings(request: Request, response: Response, session: ReadSession) -> SettingsView:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.settings.get(session.principal)


@router.put("/settings")
async def update_settings(
    request: Request, response: Response, session: WriteSession, command: SettingsUpdate,
) -> SettingsView:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.settings.put(session.principal, command)
