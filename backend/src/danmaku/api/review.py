from typing import Annotated

from fastapi import APIRouter, Depends, Request

from danmaku.api.workbench import Cursor, Limit, ReadSession, WriteSession
from danmaku.domain.review import ReviewChange, ReviewCommit, ReviewFilter, ReviewOffset, ReviewPage, ReviewRevision, ReviewText
from danmaku.domain.workbench import ErrorResponse, ResourceId

router = APIRouter(responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                              409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}})


@router.get("/projects/{project_id}/review")
def list_review(
    request: Request, session: ReadSession, project_id: ResourceId, run_id: ResourceId,
    filters: Annotated[ReviewFilter, Depends()], cursor: Cursor = None, limit: Limit = 100,
) -> ReviewPage:
    return request.app.state.review.list(session.principal, project_id, run_id, filters, cursor, limit)


@router.patch("/projects/{project_id}/review")
def update_review(
    request: Request, session: WriteSession, project_id: ResourceId, command: ReviewChange,
) -> ReviewCommit:
    return request.app.state.review.apply(session.principal, project_id, command)


@router.patch("/projects/{project_id}/review/offset")
def update_review_offset(
    request: Request, session: WriteSession, project_id: ResourceId, command: ReviewOffset,
) -> ReviewCommit:
    return request.app.state.review.set_offset(session.principal, project_id, command)


@router.patch("/projects/{project_id}/review/text")
def update_review_text(
    request: Request, session: WriteSession, project_id: ResourceId, command: ReviewText,
) -> ReviewCommit:
    return request.app.state.review.set_text(session.principal, project_id, command)


@router.post("/projects/{project_id}/review/reset")
def reset_review(
    request: Request, session: WriteSession, project_id: ResourceId, command: ReviewRevision,
) -> ReviewCommit:
    return request.app.state.review.reset(session.principal, project_id, command)
