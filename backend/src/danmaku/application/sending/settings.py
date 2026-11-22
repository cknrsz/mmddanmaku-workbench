import asyncio
from collections.abc import Awaitable, Callable
import json

from pydantic import SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool

from danmaku.application.credential_changes import CredentialChanges, credential_changes
from danmaku.application.ports import CredentialStore, Repository, UnitOfWork
from danmaku.domain.bilibili_login import BilibiliLoginSecret, BilibiliProfile
from danmaku.domain.sending import (
    BilibiliAccount, BilibiliAccountRole, BilibiliCredential, BilibiliCredentialStatus,
    BilibiliSettingsInput, BilibiliSettingsView,
)
from danmaku.domain.workbench import Principal, WorkbenchError

ProfileLookup = Callable[[BilibiliCredential], Awaitable[BilibiliProfile]]


class BilibiliSettings:
    """Verify logins before storing them and enforce account confirmation for live sends."""

    def __init__(self, database: UnitOfWork, credentials: CredentialStore, profile: ProfileLookup):
        self.database = database
        self.credentials = credentials
        self.profile = profile
        self.login_lock = asyncio.Lock()

    def _read(self, account: BilibiliAccount) -> BilibiliLoginSecret | None:
        secret = self.credentials.get(account.secret_ref)
        if secret is None:
            return None
        try:
            return BilibiliLoginSecret.model_validate_json(secret.get_secret_value())
        except ValidationError:
            raise WorkbenchError(503, "credential_store_unavailable", "B 站登录凭据无法读取") from None

    def get(self, actor: Principal) -> BilibiliSettingsView:
        with self.database() as repository:
            return self.view(repository, actor.id)

    def view(self, repository: Repository, owner_id: str) -> BilibiliSettingsView:
        account = repository.bilibili_account(owner_id)
        if account is None:
            return BilibiliSettingsView()
        return BilibiliSettingsView(account_id=account.account_id, version=account.version,
                                     configured=self._read(account) is not None)

    def saved_login(self, owner_id: str, role: BilibiliAccountRole = "primary"
                    ) -> tuple[BilibiliAccount | None, BilibiliLoginSecret | None]:
        with self.database() as repository:
            account = repository.bilibili_account(owner_id, role)
            return account, self._read(account) if account is not None else None

    def has_saved_credential(self, account: BilibiliAccount) -> bool:
        return self._read(account) is not None

    def download_credential(self, owner_id: str) -> BilibiliCredential | None:
        account, credential = self.saved_login(owner_id)
        if account is not None and credential is None:
            raise WorkbenchError(409, "bilibili_credential_missing", "请重新保存 B 站登录凭据后再下载")
        return credential

    async def check_credential(self, actor: Principal) -> BilibiliCredentialStatus:
        account, credential = await run_in_threadpool(self.saved_login, actor.id)
        if account is None:
            return BilibiliCredentialStatus(status="unconfigured")
        if credential is None:
            return BilibiliCredentialStatus(status="renewal_required")
        try:
            account_id = (await self.profile(credential)).account_id
        except WorkbenchError as exc:
            if exc.info.code != "bilibili_credential_rejected":
                raise
            return BilibiliCredentialStatus(status="renewal_required")
        return BilibiliCredentialStatus(status="valid" if account_id == account.account_id else "renewal_required")

    def secret(self, owner_id: str, account_id: str, role: BilibiliAccountRole = "primary") -> BilibiliCredential:
        with self.database() as repository:
            account = repository.bilibili_account(owner_id, role)
            if account is None:
                raise WorkbenchError(409, "bilibili_credential_missing", "请先保存 B 站登录凭据")
            if account.account_id != account_id:
                raise WorkbenchError(409, "bilibili_account_changed", "请切回批次确认的 B 站账号")
            credential = self._read(account)
            if credential is None:
                raise WorkbenchError(409, "bilibili_credential_missing", "请重新保存该账号的登录凭据")
            return credential

    async def put(self, actor: Principal, command: BilibiliSettingsInput) -> BilibiliSettingsView:
        async with self.login_lock:
            profile = await self.profile(command.credential)
            return await run_in_threadpool(self._put, actor.id, command.credential, profile)

    def _put(self, owner_id: str, credential: BilibiliCredential, profile: BilibiliProfile) -> BilibiliSettingsView:
        with credential_changes() as changes, self.database(write=True) as repository:
            return self.save(repository, owner_id, credential, profile.account_id, changes, profile)

    def save(
        self, repository: Repository, owner_id: str, credential: BilibiliCredential, account_id: str,
        changes: CredentialChanges, profile: BilibiliProfile | None = None, role: BilibiliAccountRole = "primary",
    ) -> BilibiliSettingsView:
        other = repository.bilibili_account(owner_id, "secondary" if role == "primary" else "primary")
        if other is not None and other.account_id == account_id:
            raise WorkbenchError(409, "bilibili_duplicate_account", "主账号和次账号必须使用不同的 B 站账号")
        previous = repository.bilibili_account(owner_id, role)
        reference = changes.put(self.credentials, self._encode(credential), previous.secret_ref if previous else None)
        account = BilibiliAccount(account_id=account_id, version=previous.version + 1 if previous else 1,
                                  secret_ref=reference, name=profile.name if profile else None,
                                  avatar_url=profile.avatar_url if profile else None,
                                  level=profile.level if profile else None)
        repository.save_bilibili_account(owner_id, account, role)
        return BilibiliSettingsView(configured=True, account_id=account_id, version=account.version)

    @staticmethod
    def _encode(credential: BilibiliCredential) -> SecretStr:
        return SecretStr(json.dumps({
            name: value.get_secret_value() for name, value in credential if value is not None
        }, ensure_ascii=True))

    def save_scanned(self, owner_id: str, credential: BilibiliLoginSecret, profile: BilibiliProfile,
                     role: BilibiliAccountRole) -> None:
        credential = credential.model_copy(update={"dedeuserid": SecretStr(profile.account_id)})
        with credential_changes() as changes, self.database(write=True) as repository:
            self.save(repository, owner_id, credential, profile.account_id, changes, profile, role)

    def replace_secret(self, owner_id: str, account: BilibiliAccount,
                       credential: BilibiliLoginSecret, role: BilibiliAccountRole) -> BilibiliAccount | None:
        with credential_changes() as changes, self.database(write=True) as repository:
            if repository.bilibili_account(owner_id, role) != account:
                return None
            reference = changes.put(self.credentials, self._encode(credential), account.secret_ref)
            updated = account.model_copy(update={"secret_ref": reference})
            repository.save_bilibili_account(owner_id, updated, role)
            return updated

    def save_level(self, owner_id: str, role: BilibiliAccountRole, account_id: str, level: int | None) -> None:
        with self.database(write=True) as repository:
            account = repository.bilibili_account(owner_id, role)
            if account is not None and account.account_id == account_id and account.level != level:
                repository.save_bilibili_account(owner_id, account.model_copy(update={"level": level}), role)

    def logout(self, owner_id: str, role: BilibiliAccountRole) -> BilibiliLoginSecret | None:
        with credential_changes() as changes, self.database(write=True) as repository:
            account = repository.bilibili_account(owner_id, role)
            if account is None:
                return None
            try:
                credential = self._read(account)
            except WorkbenchError:
                credential = None
            if credential is not None:
                credential = credential.model_copy(update={"dedeuserid": SecretStr(account.account_id)})
            repository.delete_bilibili_account(owner_id, role)
            changes.remove(self.credentials, account.secret_ref)
            return credential
