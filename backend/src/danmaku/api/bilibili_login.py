from fastapi import APIRouter, Request, Response

from danmaku.api.workbench import WriteSession
from danmaku.domain.bilibili_login import BilibiliLoginStatus, BilibiliQrCode, BilibiliQrStatus
from danmaku.domain.sending import BilibiliAccountRole
from danmaku.domain.workbench import ResourceId

router = APIRouter(prefix="/bilibili/login")


@router.post("/status")
async def status(request: Request, response: Response, session: WriteSession,
                 role: BilibiliAccountRole = "primary", force: bool = False) -> BilibiliLoginStatus:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.bilibili_login.status(session.principal.id, role, force=force)


@router.post("/qr", status_code=201)
async def start(request: Request, response: Response, session: WriteSession,
                role: BilibiliAccountRole = "primary") -> BilibiliQrCode:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.bilibili_login.start(session.principal.id, role)


@router.post("/qr/{attempt_id}/poll")
async def poll(request: Request, response: Response, session: WriteSession, attempt_id: ResourceId,
               role: BilibiliAccountRole = "primary") -> BilibiliQrStatus:
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.bilibili_login.poll(session.principal.id, attempt_id, role)


@router.delete("/qr/{attempt_id}", status_code=204)
async def cancel(request: Request, session: WriteSession, attempt_id: ResourceId,
                 role: BilibiliAccountRole = "primary") -> None:
    await request.app.state.bilibili_login.cancel(session.principal.id, attempt_id, role)


@router.delete("/", status_code=204)
async def logout(request: Request, session: WriteSession, role: BilibiliAccountRole = "primary") -> None:
    await request.app.state.bilibili_login.logout(session.principal.id, role)
