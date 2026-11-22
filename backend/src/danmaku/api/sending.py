from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, StringConstraints

from danmaku.api.workbench import Cursor, Limit, ReadSession, WriteSession
from danmaku.domain.sending import (
    LIVE_CAPABILITIES, BilibiliAccountRole, BilibiliCredentialStatus, BilibiliSettingsInput, BilibiliSettingsView,
    Delivery, DeliveryFilter, DeliveryPage, ResolveDelivery, SendCapabilities, SendPreview, SendRate, SendTiming,
)
from danmaku.domain.workbench import ErrorResponse, NumericId, ResourceId, SendPriority, Task, TaskControl

router = APIRouter(responses={422: {"model": ErrorResponse}})
DeliveryId = Annotated[str, StringConstraints(pattern=r"^[0-9]{12}$")]


@router.get("/projects/{project_id}/send-preview")
def preview_send(
    request: Request, session: ReadSession, project_id: ResourceId, run_id: ResourceId,
    cursor: Cursor = None, limit: Limit = 5, mode: Literal["live"] = "live",
) -> SendPreview:
    return request.app.state.tasks.sending.preview(session.principal, project_id, run_id, cursor, limit)


@router.get("/send-capabilities")
def send_capabilities(_session: ReadSession) -> dict[Literal["live"], SendCapabilities]:
    return {"live": LIVE_CAPABILITIES}


class DualUpgrade(BaseModel):
    secondary_account_id: NumericId


@router.get("/send-queue/dual-upgrade")
def dual_upgrade_count(request: Request, session: ReadSession) -> int:
    return request.app.state.tasks.sending.dual_upgrade_count(session.principal)


@router.post("/send-queue/dual-upgrade", status_code=202)
def request_dual_upgrade(request: Request, session: WriteSession, command: DualUpgrade) -> int:
    return request.app.state.tasks.sending.request_dual_upgrade(session.principal, command.secondary_account_id)


@router.get("/send-queue/pending-decision")
def pending_decision(request: Request, session: ReadSession) -> Task | None:
    return request.app.state.tasks.sending.pending_decision(session.principal)


@router.get("/send-queue/active")
def active_queue_task(request: Request, session: ReadSession) -> str | None:
    return request.app.state.tasks.sending.active_queue_task_id(session.principal)


@router.get("/send-queue/rate")
def send_rate(request: Request, session: ReadSession) -> SendRate | None:
    return request.app.state.tasks.sending.send_rate(session.principal)


@router.get("/send-queue/connection")
async def send_connection(request: Request, response: Response, _session: ReadSession) -> bool | None:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.send_connection.connected


@router.get("/send-queue/interrupted")
def interrupted_send_count(request: Request, session: ReadSession) -> int:
    return request.app.state.tasks.interrupted_send_count(session.principal)


class ConfirmedSendHead(TaskControl):
    task_id: ResourceId


class PausedSendHead(ConfirmedSendHead):
    project_name: str


@router.get("/send-queue/paused-head")
def paused_send_head(request: Request, session: ReadSession) -> PausedSendHead | None:
    result = request.app.state.tasks.paused_send_head(session.principal)
    if result is None:
        return None
    task, project_name = result
    return PausedSendHead(task_id=task.id, expected_version=task.version, project_name=project_name)


@router.post("/send-queue/resume-interrupted", status_code=202)
def resume_interrupted_sends(
    request: Request, session: WriteSession, command: ConfirmedSendHead | None = None,
) -> int:
    confirmed_head = (command.task_id, command.expected_version) if command is not None else None
    return request.app.state.tasks.resume_interrupted_sends(session.principal, confirmed_head)


@router.post("/tasks/{task_id}/send-priority", status_code=202)
def prioritize_send(request: Request, session: WriteSession, task_id: ResourceId, command: SendPriority) -> Task:
    return request.app.state.tasks.sending.prioritize(session.principal, task_id, command)


@router.post("/tasks/{task_id}/send-primary-only", status_code=202)
def use_primary_only(request: Request, session: WriteSession, task_id: ResourceId) -> Task:
    return request.app.state.tasks.sending.use_primary_only(session.principal, task_id)


@router.post("/tasks/{task_id}/send-cancel", status_code=202)
def cancel_send(request: Request, session: WriteSession, task_id: ResourceId, command: TaskControl) -> Task:
    return request.app.state.tasks.sending.cancel(session.principal, task_id, command)


@router.get("/settings/bilibili")
def get_bilibili_settings(request: Request, session: ReadSession) -> BilibiliSettingsView:
    return request.app.state.bilibili_settings.get(session.principal)


@router.get("/settings/sending")
def get_send_timing(request: Request, session: ReadSession) -> SendTiming:
    return request.app.state.tasks.sending.get_timing(session.principal)


@router.put("/settings/sending")
def put_send_timing(request: Request, session: WriteSession, command: SendTiming) -> SendTiming:
    return request.app.state.tasks.sending.put_timing(session.principal, command)


@router.post("/settings/bilibili/check-credential")
async def check_bilibili_credential(request: Request, session: WriteSession) -> BilibiliCredentialStatus:
    return await request.app.state.bilibili_settings.check_credential(session.principal)


@router.put("/settings/bilibili")
async def put_bilibili_settings(
    request: Request, session: WriteSession, command: BilibiliSettingsInput,
) -> BilibiliSettingsView:
    return await request.app.state.bilibili_settings.put(session.principal, command)


@router.get("/tasks/{task_id}/deliveries")
def list_deliveries(
    request: Request, session: ReadSession, task_id: ResourceId, status: DeliveryFilter | None = None,
    cursor: Cursor = None, limit: Limit = 100, page: Annotated[int | None, Query(ge=1)] = None,
    track: Annotated[list[DeliveryId] | None, Query(max_length=10)] = None,
    account_role: BilibiliAccountRole | None = None,
) -> DeliveryPage:
    return request.app.state.tasks.sending.list(session.principal, task_id, status, cursor, limit, page, track,
                                                account_role=account_role)


@router.post("/tasks/{task_id}/deliveries/retry-failed")
def retry_failed(request: Request, session: WriteSession, task_id: ResourceId, command: TaskControl) -> Task:
    return request.app.state.tasks.sending.retry_failed(session.principal, task_id, command)


@router.post("/tasks/{task_id}/deliveries/continue-queue")
def continue_queue(request: Request, session: WriteSession, task_id: ResourceId, command: TaskControl) -> Task:
    return request.app.state.tasks.sending.continue_queue(session.principal, task_id, command)


@router.post("/tasks/{task_id}/deliveries/{delivery_id}/resolve")
def resolve_delivery(
    request: Request, session: WriteSession, task_id: ResourceId, delivery_id: ResourceId, command: ResolveDelivery,
) -> Delivery:
    return request.app.state.tasks.sending.resolve(session.principal, task_id, delivery_id, command)


@router.post("/tasks/{task_id}/deliveries/{delivery_id}/withdraw")
def withdraw_delivery(
    request: Request, session: WriteSession, task_id: ResourceId, delivery_id: DeliveryId, command: TaskControl,
) -> Task:
    return request.app.state.tasks.sending.change_delivery(session.principal, task_id, delivery_id, command, resend=False)


@router.post("/tasks/{task_id}/deliveries/{delivery_id}/resend", status_code=202)
def resend_delivery(
    request: Request, session: WriteSession, task_id: ResourceId, delivery_id: DeliveryId, command: TaskControl,
) -> Task:
    return request.app.state.tasks.sending.change_delivery(session.principal, task_id, delivery_id, command, resend=True)
