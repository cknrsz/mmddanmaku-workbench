from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable

from pydantic import SecretStr

from danmaku.domain.analysis import ModelConfig, Operation

ModelText = Literal["reasoning", "output"]
PublishText = Callable[[ModelText, str], None]


class ModelFailure(Exception):
    """Provider-neutral code and optional sanitized provider or transport details."""

    def __init__(self, code: str, *, http_status: int | None = None, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.detail = detail


class ModelProvider(Protocol):
    async def generate(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict,
    ) -> object:
        """Return decoded JSON for one operation, or raise ModelFailure."""
        ...


@runtime_checkable
class StreamingModelProvider(ModelProvider, Protocol):
    async def generate_stream(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict, publish: PublishText,
    ) -> object:
        """Publish provisional text from the same request and return its decoded final JSON."""
        ...
