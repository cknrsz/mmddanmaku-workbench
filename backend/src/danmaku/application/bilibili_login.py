import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from secrets import token_urlsafe
from time import monotonic
from typing import Literal, Protocol

from starlette.concurrency import run_in_threadpool

from danmaku.application.sending.settings import BilibiliSettings
from danmaku.domain.bilibili_login import (
    BilibiliLoginSecret, BilibiliLoginStatus, BilibiliProfile, BilibiliQrCode, BilibiliQrStatus, QrChallenge,
)
from danmaku.domain.sending import BilibiliAccount, BilibiliAccountRole, BilibiliCredential
from danmaku.domain.workbench import WorkbenchError

CHECK_INTERVAL = 30 * 60
RETRY_INTERVAL = 60
QR_LIFETIME = 180


class Passport(Protocol):
    async def generate(self) -> QrChallenge: ...
    async def poll(self, key: str) -> str | BilibiliLoginSecret: ...
    async def refresh_needed(self, credential: BilibiliLoginSecret) -> bool: ...
    async def refresh(self, credential: BilibiliLoginSecret) -> BilibiliLoginSecret: ...
    async def confirm_refresh(self, credential: BilibiliLoginSecret) -> None: ...
    async def logout(self, credential: BilibiliLoginSecret) -> None: ...


@dataclass
class LoginAttempt:
    id: str
    key: str = field(repr=False)
    expires_at: float
    next_poll: float = 0
    status: Literal["waiting", "scanned", "expired"] = "waiting"
    credential: BilibiliLoginSecret | None = None
    completed: BilibiliLoginStatus | None = None


@dataclass(frozen=True)
class CheckedLogin:
    reference: str
    status: BilibiliLoginStatus
    next_check: float


class BilibiliLogin:
    """Own QR attempts and renewal. Only a verified account is committed to the credential store."""

    def __init__(self, settings: BilibiliSettings, passport: Passport,
                 profile: Callable[[BilibiliCredential], Awaitable[BilibiliProfile]], clock=monotonic):
        self.settings = settings
        self.passport = passport
        self.profile = profile
        self.clock = clock
        self.lock = settings.login_lock
        self.attempts: dict[tuple[str, BilibiliAccountRole], LoginAttempt] = {}
        self.checked: dict[tuple[str, BilibiliAccountRole], CheckedLogin] = {}

    async def start(self, owner_id: str, role: BilibiliAccountRole) -> BilibiliQrCode:
        async with self.lock:
            challenge = await self.passport.generate()
            attempt = LoginAttempt(token_urlsafe(24), challenge.key, self.clock() + QR_LIFETIME)
            self.attempts[owner_id, role] = attempt
            return BilibiliQrCode(id=attempt.id, image=challenge.image)

    async def cancel(self, owner_id: str, attempt_id: str, role: BilibiliAccountRole) -> None:
        async with self.lock:
            attempt = self.attempts.get((owner_id, role))
            if attempt is not None and attempt.id == attempt_id:
                self.attempts.pop((owner_id, role))

    async def poll(self, owner_id: str, attempt_id: str, role: BilibiliAccountRole) -> BilibiliQrStatus:
        async with self.lock:
            slot = (owner_id, role)
            attempt = self.attempts.get(slot)
            if attempt is None or attempt.id != attempt_id:
                return BilibiliQrStatus(status="expired")
            if attempt.expires_at <= self.clock() or attempt.status == "expired":
                self.attempts.pop(slot)
                return BilibiliQrStatus(status="expired")
            if attempt.completed is not None:
                return BilibiliQrStatus(status="authenticated", login=attempt.completed)
            if attempt.next_poll > self.clock():
                return BilibiliQrStatus(status=attempt.status)
            attempt.next_poll = self.clock() + 2
            if attempt.credential is None:
                result = await self.passport.poll(attempt.key)
                if isinstance(result, str):
                    attempt.status = result
                    return BilibiliQrStatus(status=attempt.status)
                attempt.credential = result
                attempt.status = "scanned"
            credential = attempt.credential
            profile = await self.profile(credential)
            await run_in_threadpool(self.settings.save_scanned, owner_id, credential, profile, role)
            attempt.credential = None
            attempt.key = ""
            attempt.completed = BilibiliLoginStatus(status="valid", profile=profile, auto_refresh=True)
            self.checked.pop(slot, None)
            return BilibiliQrStatus(status="authenticated", login=attempt.completed)

    async def logout(self, owner_id: str, role: BilibiliAccountRole) -> None:
        async with self.lock:
            self.attempts.pop((owner_id, role), None)
            self.checked.pop((owner_id, role), None)
            credential = await run_in_threadpool(self.settings.logout, owner_id, role)
            if credential is not None:
                try:
                    await self.passport.logout(credential)
                except WorkbenchError:
                    pass  # Removing the local binding succeeds even when B 站 is offline.

    async def status(self, owner_id: str, role: BilibiliAccountRole, *, force: bool = False) -> BilibiliLoginStatus:
        async with self.lock:
            slot = (owner_id, role)
            account, credential = await run_in_threadpool(self.settings.saved_login, owner_id, role)
            if account is None:
                return BilibiliLoginStatus(status="unconfigured")
            if credential is None:
                return BilibiliLoginStatus(status="renewal_required")
            cached = self.checked.get(slot)
            if not force and cached and cached.reference == account.secret_ref and cached.next_check > self.clock():
                return cached.status
            status, retry = await self._check(owner_id, role, account, credential)
            if status.status != "unavailable":
                level = status.profile.level if status.profile else None
                if level != account.level:
                    await run_in_threadpool(self.settings.save_level, owner_id, role, account.account_id, level)
                retry = retry or (role == "secondary" and status.status == "valid" and level in (None, 0))
            current, _ = await run_in_threadpool(self.settings.saved_login, owner_id, role)
            if current is not None and current.account_id == account.account_id:
                self.checked[slot] = CheckedLogin(
                    current.secret_ref, status, self.clock() + (RETRY_INTERVAL if retry else CHECK_INTERVAL),
                )
            return status

    async def _check(self, owner_id: str, role: BilibiliAccountRole, account: BilibiliAccount,
                     credential: BilibiliLoginSecret) -> tuple[BilibiliLoginStatus, bool]:
        can_refresh = credential.refresh_token is not None
        retry = False
        if can_refresh:
            try:
                credential = await self._maintain(owner_id, role, account, credential)
            except WorkbenchError as exc:
                # Rotation may already have been saved before confirmation failed.
                current, saved = await run_in_threadpool(self.settings.saved_login, owner_id, role)
                if current is not None and current.account_id == account.account_id and saved is not None:
                    credential = saved
                if exc.info.code == "bilibili_refresh_rejected":
                    can_refresh = False
                else:
                    retry = True
        try:
            profile = await self.profile(credential)
        except WorkbenchError as exc:
            if exc.info.code == "bilibili_credential_rejected":
                return BilibiliLoginStatus(status="renewal_required"), retry
            profile = BilibiliProfile(account_id=account.account_id, name=account.name,
                                      avatar_url=account.avatar_url, level=account.level)
            return BilibiliLoginStatus(status="unavailable", profile=profile, auto_refresh=can_refresh), True
        if profile.account_id != account.account_id:
            return BilibiliLoginStatus(status="renewal_required"), False
        return BilibiliLoginStatus(status="valid", profile=profile, auto_refresh=can_refresh), retry

    async def _maintain(self, owner_id: str, role: BilibiliAccountRole, account: BilibiliAccount,
                        credential: BilibiliLoginSecret) -> BilibiliLoginSecret:
        if credential.pending_confirmation is None:
            if not await self.passport.refresh_needed(credential):
                return credential
            refreshed = await self.passport.refresh(credential)
            uid = refreshed.dedeuserid
            if uid is not None and uid.get_secret_value() != account.account_id:
                raise WorkbenchError(409, "bilibili_account_changed", "B 站续期返回了其他账号")
            account = await run_in_threadpool(self.settings.replace_secret, owner_id, account, refreshed, role)
            if account is None:
                raise WorkbenchError(409, "bilibili_account_changed", "B 站登录状态已更改")
            credential = refreshed
        await self.passport.confirm_refresh(credential)
        confirmed = credential.model_copy(update={"pending_confirmation": None})
        await run_in_threadpool(self.settings.replace_secret, owner_id, account, confirmed, role)
        return confirmed

    async def maintain(self, owner_id: str) -> None:
        # Checking the saved session also keeps the credential current without a browser tab.
        while True:
            await asyncio.sleep(RETRY_INTERVAL)
            for role in ("primary", "secondary"):
                try:
                    await self.status(owner_id, role)
                except WorkbenchError:
                    continue
