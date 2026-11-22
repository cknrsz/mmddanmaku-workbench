from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, StringConstraints, computed_field, field_validator

from danmaku.domain.workbench import Command, ErrorInfo, ReasoningEffort, TaskState

Provider = Literal["mock", "openai_compatible"]
Strategy = Literal["full", "segmented", "bilingual", "single_subtitle", "skipped"]
Category = Literal["translation", "other"]
Operation = Literal["classify"]
PROMPT_VERSION = "6"
ANALYSIS_TIMEOUT_SECONDS = 15 * 60
ANALYSIS_STOPPED = ErrorInfo(code="analysis_stopped", message="分析已停止")


class ModelOptions(BaseModel):
    thinking_enabled: bool = Field(default=True, strict=True)
    reasoning_effort: ReasoningEffort = "high"
    extra_requirements: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""


class ModelConfig(ModelOptions):
    provider: Provider
    base_url: str | None
    model: str
    version: int

    @property
    def uses_deepseek_thinking(self) -> bool:
        return (
            self.provider == "openai_compatible" and self.base_url is not None
            and urlsplit(self.base_url).hostname == "api.deepseek.com" and self.model == "deepseek-flash"
        )

    @property
    def supports_reasoning_effort(self) -> bool:
        return self.uses_deepseek_thinking and self.thinking_enabled


MOCK_CONFIG = ModelConfig(provider="mock", base_url=None, model="synthetic-v1", version=0)


class ModelConfigInput(Command, ModelOptions):
    provider: Provider
    base_url: Annotated[str, StringConstraints(max_length=500)] | None = None
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    api_key: Annotated[SecretStr, Field(min_length=1, max_length=1024, json_schema_extra={"writeOnly": True})] | None = None
    remove_api_key: bool = Field(default=False, strict=True)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and any(not 33 <= ord(character) <= 126 for character in value.get_secret_value()):
            raise ValueError("invalid API key")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isascii() or any(ord(character) <= 32 for character in value) or any(
            character in value for character in "\\?#@%"
        ):
            raise ValueError("invalid endpoint")
        url = urlsplit(value)
        port = url.port
        host = url.hostname
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        if not host or (url.scheme != "https" and not (url.scheme == "http" and loopback)):
            raise ValueError("invalid endpoint")
        if not all(character.isalnum() or character in ".-:" for character in host):
            raise ValueError("invalid hostname")
        if port == 0 or any(segment in {".", ".."} for segment in url.path.split("/")):
            raise ValueError("invalid endpoint")
        return url.geturl().rstrip("/")


class ModelSettings(ModelConfig):
    configured: bool

    @computed_field
    @property
    def can_set_reasoning_effort(self) -> bool:
        return self.supports_reasoning_effort


class AnalysisInput(BaseModel):
    id: str
    project_id: str
    snapshot_id: str
    subtitle_version_id: str | None = None
    japanese_subtitle_version_id: str | None = None
    config: ModelConfig | None
    prompt_version: str | None = "1"
    strategy: Strategy | None = None


class AnalysisRun(AnalysisInput):
    name: str = ""
    state: TaskState


class Classification(BaseModel):
    id: str
    category: Category | None = None
    reason: str | None = None
    cue_ids: list[str] = Field(default_factory=list)
    diagnostic: str | None = None


class AnalysisOutput(BaseModel):
    classifications: list[Classification] = Field(default_factory=list)
