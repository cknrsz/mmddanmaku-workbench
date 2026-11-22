from dataclasses import dataclass

from pydantic import BaseModel, Field

from danmaku.domain.inputs import VideoPart
from danmaku.domain.workbench import Bvid, Command, PageNumber, ResourceId


@dataclass(frozen=True)
class Publication:
    project_id: str
    account_id: str
    publish_at: int
    task_id: str | None = None
    check_after: float = 0
    checks: int = 0
    overdue: bool = False
    checking: bool = False
    due_observed: bool = False
    time_checked: bool = False


class PreloadPart(BaseModel):
    part: VideoPart
    publish_at: int
    already_created: bool = False


class PreloadProject(Command):
    bvid: Bvid
    page: PageNumber


class OverduePublication(Command):
    task_id: ResourceId
    send: bool = Field(strict=True)
