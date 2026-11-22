from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe
from threading import Lock
from time import monotonic

from danmaku.domain.workbench import Principal, SessionInfo, WorkbenchError

LOCAL_OWNER_ID = "local-owner"


@dataclass(frozen=True)
class LocalSession:
    principal: Principal
    csrf_token: str
    expires_at: float

    def info(self) -> SessionInfo:
        return SessionInfo(csrf_token=self.csrf_token)


class LocalSessions:
    session_seconds = 12 * 60 * 60

    def __init__(self, clock: Callable[[], float] = monotonic):
        self.clock = clock
        self.lock = Lock()
        self.sessions: dict[str, LocalSession] = {}

    def stop(self) -> None:
        with self.lock:
            self.sessions.clear()

    def open(self, existing: str | None) -> tuple[str, LocalSession]:
        with self.lock:
            now = self.clock()
            self.sessions = {token: session for token, session in self.sessions.items() if session.expires_at > now}
            if existing is not None and existing in self.sessions:
                return existing, self.sessions[existing]
            token = token_urlsafe(32)
            session = LocalSession(
                Principal(id=LOCAL_OWNER_ID), token_urlsafe(32), now + self.session_seconds,
            )
            self.sessions[token] = session
            return token, session

    def authenticate(self, token: str | None) -> LocalSession:
        with self.lock:
            session = self.sessions.get(token) if token is not None else None
            if session is not None and session.expires_at > self.clock():
                return session
            if token is not None:
                self.sessions.pop(token, None)
        raise WorkbenchError(401, "session_required", "本机会话已过期，请重新连接")

    def revoke(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

