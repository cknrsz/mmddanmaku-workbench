from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from danmaku.domain.sending import BilibiliCredential
from danmaku.domain.workbench import NumericId


class BilibiliLoginSecret(BilibiliCredential):
    refresh_token: SecretStr | None = Field(None, min_length=1, max_length=512)
    pending_confirmation: SecretStr | None = Field(None, min_length=1, max_length=512)
    dedeuserid: SecretStr | None = Field(None, min_length=1, max_length=32)


class BilibiliProfile(BaseModel):
    account_id: NumericId
    name: str | None = None
    avatar_url: str | None = None
    level: int | None = Field(default=None, ge=0)


class BilibiliLoginStatus(BaseModel):
    status: Literal["unconfigured", "valid", "renewal_required", "unavailable"]
    profile: BilibiliProfile | None = None
    auto_refresh: bool = False


class BilibiliQrCode(BaseModel):
    id: str
    image: str


class BilibiliQrStatus(BaseModel):
    status: Literal["waiting", "scanned", "expired", "authenticated"]
    login: BilibiliLoginStatus | None = None


@dataclass(frozen=True)
class QrChallenge:
    key: str = field(repr=False)
    image: str = field(repr=False)
