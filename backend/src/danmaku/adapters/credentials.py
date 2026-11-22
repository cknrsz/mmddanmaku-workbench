from uuid import uuid4

from pydantic import SecretStr


class MemoryCredentialStore:
    """In-process store for synthetic tests. References expire with the process."""

    def __init__(self) -> None:
        self._secrets: dict[str, SecretStr] = {}

    def put(self, secret: SecretStr) -> str:
        reference = uuid4().hex
        self._secrets[reference] = secret
        return reference

    def get(self, reference: str) -> SecretStr | None:
        return self._secrets.get(reference)

    def delete(self, reference: str) -> None:
        self._secrets.pop(reference, None)

