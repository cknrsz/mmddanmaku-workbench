from datetime import datetime
from typing import Annotated, Literal, NamedTuple

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, model_validator

ResourceId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Bvid = Annotated[str, StringConstraints(pattern=r"^BV[0-9A-Za-z]{10}$")]
PageNumber = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
NumericId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
TaskState = Literal[
    "queued", "running", "backoff", "pausing", "paused", "cancelling", "cancelled", "succeeded", "failed", "needs_review",
]
TaskKind = Literal["download", "classify", "send"]
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "needs_review"}


class ProjectTaskState(NamedTuple):
    kind: TaskKind
    state: TaskState
    live: bool
    send_started: bool
    send_complete: bool
    send_needs_review: bool
    send_partial_failure: bool
    creation_order: int


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionInfo(BaseModel):
    csrf_token: str


class Principal(BaseModel):
    id: str


class ProjectVideos(Command):
    source_bvid: Bvid | None = None
    target_bvid: Bvid
    source_page: PageNumber | None = None
    target_page: PageNumber

    @model_validator(mode="after")
    def check_source(self):
        if (self.source_bvid is None) != (self.source_page is None):
            raise ValueError("source BV and page must be supplied together")
        return self


class ProjectCreate(ProjectVideos):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""


class ProjectActivity(BaseModel):
    stage: TaskKind | Literal["review"] = "download"
    running: bool = False
    active: bool = False


class SendQueueStatus(BaseModel):
    position: int
    total: int


class Project(ProjectVideos):
    id: str
    creation_order: int = 0
    name: Annotated[str, StringConstraints(min_length=1)]
    completed: bool = False
    send_needs_review: bool = False
    send_partial_failure: bool = False
    activity: ProjectActivity = Field(default_factory=ProjectActivity)
    send_queue: SendQueueStatus | None = None
    publication_status: Literal["waiting", "checking", "blocked"] | None = None
    preload_publish_at: int | None = None


class ProjectRename(Command):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ProjectCompletion(Command):
    completed: Annotated[bool, Field(strict=True)]


class ProjectDetail(Project):
    snapshot_ids: list[str] = Field(default_factory=list)
    subtitle_version_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)


class TaskCreate(Command):
    kind: Literal["download"]
    mode: Literal["mock", "bilibili"] = "mock"
    source_bvid: Bvid | None = None
    source_page: PageNumber | None = None
    source_cid: NumericId | None = None

    @model_validator(mode="after")
    def check_source(self):
        if (self.source_bvid is None) != (self.source_page is None):
            raise ValueError("source BV and page must be supplied together")
        if self.source_cid is not None and (self.source_bvid is not None or self.mode != "bilibili"):
            raise ValueError("source CID requires Bilibili mode and excludes source BV and page")
        return self


ReasoningEffort = Literal["low", "high", "max"]


class ClassifyCreate(Command):
    kind: Literal["classify"]
    snapshot_id: ResourceId
    subtitle_version_id: ResourceId | None = None
    japanese_subtitle_version_id: ResourceId | None = None
    skip_analysis: Annotated[bool, Field(strict=True)] = False
    llm_config_version: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)] | None = None
    reasoning_effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def check_model_version(self):
        if self.skip_analysis and self.llm_config_version is not None:
            raise ValueError("skipping analysis does not accept a model version")
        if self.skip_analysis and self.reasoning_effort is not None:
            raise ValueError("skipping analysis does not accept a reasoning effort")
        if not self.skip_analysis and self.llm_config_version is None:
            raise ValueError("smart analysis requires a model version")
        return self


class LegacyClassifyCreate(Command):
    kind: Literal["classify"]
    snapshot_id: ResourceId
    subtitle_version_id: ResourceId
    llm_config_version: int
    analyze_harm: Annotated[bool, Field(strict=True)] = False


SendMode = Literal["mock", "live"]


class SendConfirmation(Command):
    target_cid: NumericId
    account_id: NumericId
    secondary_account_id: NumericId | None = None
    secondary_level: Annotated[int, Field(strict=True, ge=1)] | None = None
    primary_only: bool = False


class SendCreate(Command):
    kind: Literal["send"]
    run_id: ResourceId
    expected_review_revision: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
    mode: SendMode = "mock"
    confirmation: SendConfirmation | None = None

    @model_validator(mode="after")
    def check_confirmation(self):
        if (self.confirmation is not None) != (self.mode == "live"):
            raise ValueError("live delivery requires target and account confirmation")
        return self


# Existing task rows can still contain a historical simulation command.
TaskCommand = Annotated[TaskCreate | ClassifyCreate | SendCreate, Field(discriminator="kind")]


class SendRequest(SendCreate):
    mode: Literal["live"] = "live"
    confirmation: SendConfirmation


TaskRequest = Annotated[TaskCreate | ClassifyCreate | SendRequest, Field(discriminator="kind")]


class TaskControl(Command):
    expected_version: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]


class SendPriority(TaskControl):
    confirmed_preload_task_id: ResourceId | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
    retries_exhausted: bool = False


class ErrorResponse(BaseModel):
    error: ErrorInfo


class WorkbenchError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(code)
        self.status = status
        self.info = ErrorInfo(code=code, message=message, retryable=retryable)


class TaskProgress(BaseModel):
    done: int = 0
    total: int | None = None
    failed: int = 0
    skipped: int = 0
    unknown: int = 0
    withdrawn: int = 0


class DownloadCheckpoint(BaseModel):
    completed_batches: int = 0


class AnalysisCheckpoint(DownloadCheckpoint):
    run_id: str
    analysis_skipped: bool = False


class SendAccountProgress(BaseModel):
    state: Literal["ready", "sending", "waiting", "finished"]
    next_attempt_at: datetime | None = None
    error: ErrorInfo | None = None


class SendCheckpoint(BaseModel):
    batch_id: str
    started: bool = False
    automatic_retry_started: bool = False
    next_send_task_id: str | None = None
    waiting_after_priority: bool = False
    priority_ready: bool = False
    primary_only: bool = False
    share_count: int = Field(default=0, ge=0, validation_alias=AliasChoices("share_count", "shared_pending"))
    secondary_upgrade_id: NumericId | None = None
    accounts: dict[Literal["primary", "secondary"], SendAccountProgress] = Field(default_factory=dict)
    publication: Literal["pending", "dispatch", "returned"] | None = None
    publication_first_pending: bool = False
    publication_retry_at: datetime | None = None


class Task(BaseModel):
    id: str
    project_id: str
    creation_order: int = Field(validation_alias=AliasChoices("creation_order", "order"))
    kind: TaskKind
    state: TaskState
    version: int
    progress: TaskProgress
    checkpoint: SendCheckpoint | AnalysisCheckpoint | DownloadCheckpoint
    next_attempt_at: datetime | None = None
    error: ErrorInfo | None = None


class TaskRecord(BaseModel):
    task: Task
    created_by: str
    idempotency_key: str
    inputs: TaskCommand | LegacyClassifyCreate
    source_bvid: Bvid
    source_page: PageNumber
    attempts: int = 0
    terminal_batch_error: bool = False


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None

