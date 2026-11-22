from contextlib import contextmanager
import logging

from pydantic import SecretStr

from danmaku.application.ports import CredentialStore
from danmaku.domain.workbench import WorkbenchError

logger = logging.getLogger(__name__)


class CredentialChanges:
    def __init__(self):
        self.created: list[tuple[CredentialStore, str]] = []
        self.superseded: list[tuple[CredentialStore, str]] = []

    def put(self, store: CredentialStore, secret: SecretStr, previous: str | None) -> str:
        reference = store.put(secret)
        self.created.append((store, reference))
        self.remove(store, previous)
        return reference

    def remove(self, store: CredentialStore, reference: str | None) -> None:
        if reference:
            self.superseded.append((store, reference))

    @staticmethod
    def discard(references: list[tuple[CredentialStore, str]]) -> None:
        for store, reference in references:
            try:
                store.delete(reference)
            except WorkbenchError:
                logger.warning("Could not clean up an unreferenced credential")


@contextmanager
def credential_changes():
    changes = CredentialChanges()
    try:
        yield changes
    except BaseException:
        changes.discard(changes.created)
        raise
    else:
        changes.discard(changes.superseded)
