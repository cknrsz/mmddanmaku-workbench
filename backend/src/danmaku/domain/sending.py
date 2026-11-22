from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from danmaku.domain.inputs import Cue, SourcePart, VideoPart
from danmaku.domain.workbench import Bvid, Command, NumericId, Page, SendMode, TaskControl

DeliveryStatus = Literal["pending", "in_flight", "confirmed", "failed", "skipped", "unknown", "withdrawn"]
DeliveryFilter = Literal[DeliveryStatus, "active"]
Resolution = Literal["confirmed", "skip", "retry"]
EffectDifference = Literal["unsupported_mode", "unsupported_color", "unsupported_font_size"]
BilibiliAccountRole = Literal["primary", "secondary"]


@dataclass(frozen=True)
class LiveSendCooldown:
    deadline: datetime
    consecutive_failures: int = 0
    reason: Literal["rate_limit", "repeated_failures"] | None = None


class BilibiliCredential(Command):
    sessdata: SecretStr = Field(min_length=1, max_length=1500, json_schema_extra={"writeOnly": True})
    bili_jct: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})
    buvid3: SecretStr | None = Field(None, min_length=1, max_length=128, json_schema_extra={"writeOnly": True})

    @field_validator("sessdata", "bili_jct", "buvid3")
    @classmethod
    def cookie_value(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and any(not 33 <= ord(c) <= 126 or c in ';"\\' for c in value.get_secret_value()):
            raise ValueError("invalid cookie value")
        return value


class BilibiliSettingsInput(Command):
    credential: BilibiliCredential


class BilibiliSettingsView(BaseModel):
    configured: bool = False
    version: int = 0
    account_id: NumericId | None = None


class BilibiliCredentialStatus(BaseModel):
    status: Literal["unconfigured", "valid", "renewal_required"]


class BilibiliAccount(BaseModel):
    account_id: NumericId
    version: int
    secret_ref: str
    avatar_url: str | None = None
    name: str | None = None
    level: int | None = Field(default=None, ge=0)


Seconds = Annotated[int, Field(strict=True, ge=0, le=86400)]


class SendTiming(Command):
    item_min_seconds: Seconds = 5
    item_max_seconds: Seconds = 7
    rate_limit_seconds: Seconds = 180

    @model_validator(mode="after")
    def ordered_interval(self) -> "SendTiming":
        if self.item_min_seconds > self.item_max_seconds:
            raise ValueError("invalid send interval")
        return self


class SendCapabilities(BaseModel):
    modes: tuple[int, ...]
    font_sizes: tuple[int, ...] | None
    rgb: bool = True
    special_color: bool = False
    platform_verified: bool = False


LIVE_CAPABILITIES = SendCapabilities(modes=(1, 4, 5, 7), font_sizes=(18, 25, 36))
BilibiliSendError = Literal[
    "bilibili_credential_missing", "bilibili_credential_rejected",
    "bilibili_account_changed", "bilibili_access_denied", "bilibili_sdk_missing",
    "credential_store_unavailable", "bilibili_video_unavailable", "bilibili_delivery_rejected",
]


class SendItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    delivery_key: str | None = None
    time_ms: int
    text: str
    font_size: int
    color_rgb: int | None
    mode: int
    checkbox_type: Literal[0, 4] = 0
    effect_differences: tuple[EffectDifference, ...] = ()
    account_role: BilibiliAccountRole | None = None

    @property
    def stable_key(self) -> str:
        return self.delivery_key or self.item_id


class SendBatch(BaseModel):
    id: str
    run_id: str
    review_revision: int
    mode: SendMode = "mock"
    account_id: NumericId | None = None
    secondary_account_id: NumericId | None = None
    target: VideoPart

    def accounts(self) -> dict[BilibiliAccountRole, str]:
        accounts: dict[BilibiliAccountRole, str] = {}
        if self.account_id is not None:
            accounts["primary"] = self.account_id
        if self.secondary_account_id is not None:
            accounts["secondary"] = self.secondary_account_id
        return accounts


class SendPreview(BaseModel):
    revision: int
    selected_count: int
    outside_count: int
    unsupported_count: int
    mode: Literal["live"] = "live"
    live_target: bool = False
    duration_mismatch_message: str | None = None
    duplicate_count: int = 0
    unstable_count: int = 0
    secondary_account_id: NumericId | None = None
    secondary_level: int | None = None
    source: SourcePart
    source_parts: list[SourcePart]
    target: VideoPart
    items: list[SendItem]
    next_cursor: str | None
    cues: list[Cue]


class BilibiliSendDiagnostic(BaseModel):
    http_status: int
    code: int | None = None
    message: str | None = None


class SendRate(BaseModel):
    task_id: str
    items_per_hour: float


class Attempt(BaseModel):
    number: int
    started_at: datetime
    finished_at: datetime | None = None
    outcome: Literal["in_flight", "confirmed", "rejected", "limited", "blocked", "unknown"] = "in_flight"
    error_code: str | None = None
    bilibili_error: BilibiliSendDiagnostic | None = None
    receipt: str | None = None
    resolution: Resolution | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    account_role: BilibiliAccountRole | None = None
    account_id: NumericId | None = Field(default=None, exclude=True)
    pacing_wait_seconds: float | None = Field(default=None, exclude=True)


class Delivery(BaseModel):
    id: str
    item: SendItem
    status: DeliveryStatus
    resending: bool = False
    resend_from: Literal["withdrawn", "skipped", "failed"] | None = Field(default=None, exclude=True)
    resend_priority: bool = Field(default=False, exclude=True)
    resend_wait_for: tuple[str, ...] = Field(default=(), exclude=True)
    error_code: str | None = None
    bilibili_error: BilibiliSendDiagnostic | None = None
    receipt: str | None = None
    delivered_at: datetime | None = None
    attempts: list[Attempt] = Field(default_factory=list)

    @property
    def active_rank(self) -> int:
        if self.status == "in_flight":
            return 0
        return 1 if self.resend_priority else 2

    def clear_resend(self) -> None:
        self.resending = False
        self.resend_from = None
        self.resend_priority = False
        self.resend_wait_for = ()


class DeliveryPage(Page[Delivery]):
    batch: SendBatch
    task_version: int
    total_count: int
    tracked_items: list[Delivery] = Field(default_factory=list)


class ResolveDelivery(TaskControl):
    outcome: Resolution


class SendWork(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    delivery_id: str
    target_cid: str
    target_bvid: Bvid | None = None
    account_ref: str
    mode: Literal["live"] = "live"
    account_id: NumericId
    account_role: BilibiliAccountRole = "primary"
    item: SendItem
    attempt: int


class SendResult(BaseModel):
    outcome: Literal["confirmed", "rejected", "limited", "blocked", "unknown", "deferred"]
    receipt: NumericId | None = None
    error_code: BilibiliSendError | None = None
    bilibili_error: BilibiliSendDiagnostic | None = None
    retry_after_seconds: Annotated[int, Field(strict=True, ge=0, le=86_400)] | None = None

    @model_validator(mode="after")
    def check_result(self):
        if (self.receipt is not None) != (self.outcome == "confirmed"):
            raise ValueError("confirmed result requires an opaque receipt")
        if self.outcome == "blocked" and self.error_code is None:
            raise ValueError("blocked result requires a diagnostic code")
        if self.retry_after_seconds is not None and self.outcome != "limited":
            raise ValueError("retry deadline requires a rate-limit result")
        return self
