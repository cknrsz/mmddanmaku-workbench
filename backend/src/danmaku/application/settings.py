from starlette.concurrency import run_in_threadpool

from danmaku.application.classification.settings import Settings
from danmaku.application.credential_changes import credential_changes
from danmaku.application.ports import UnitOfWork
from danmaku.application.sending.settings import BilibiliSettings
from danmaku.domain.bilibili_login import BilibiliProfile
from danmaku.domain.settings import SettingsUpdate, SettingsView
from danmaku.domain.workbench import Principal


class WorkbenchSettings:
    def __init__(self, database: UnitOfWork, bilibili: BilibiliSettings, llm: Settings):
        self.database = database
        self.bilibili = bilibili
        self.llm = llm

    def get(self, actor: Principal) -> SettingsView:
        with self.database() as repository:
            return SettingsView(bilibili=self.bilibili.view(repository, actor.id),
                                llm=self.llm.view(repository, actor.id),
                                review=repository.review_settings(actor.id),
                                sending=repository.sending_settings(actor.id))

    async def put(self, actor: Principal, command: SettingsUpdate) -> SettingsView:
        if command.bilibili is None:
            return await run_in_threadpool(self._put, actor.id, command, None)
        async with self.bilibili.login_lock:
            profile = await self.bilibili.profile(command.bilibili)
            return await run_in_threadpool(self._put, actor.id, command, profile)

    def _put(self, owner_id: str, command: SettingsUpdate, profile: BilibiliProfile | None) -> SettingsView:
        with credential_changes() as changes, self.database(write=True) as repository:
            if command.bilibili is not None:
                assert profile is not None
                bilibili = self.bilibili.save(repository, owner_id, command.bilibili, profile.account_id, changes, profile)
            else:
                bilibili = self.bilibili.view(repository, owner_id)
            llm = (self.llm.save(repository, owner_id, command.llm, changes) if command.llm is not None
                   else self.llm.view(repository, owner_id))
            sending = repository.sending_settings(owner_id)
            if command.sending is not None:
                values = ({} if command.sending.model_fields_set == {"dual_account_enabled"}
                          else command.sending.model_dump(exclude={"dual_account_enabled"}))
                if command.sending.dual_account_enabled is not None:
                    values["dual_account_enabled"] = command.sending.dual_account_enabled
                updated = sending.model_copy(update=values)
                if updated != sending:
                    repository.save_sending_settings(owner_id, updated)
                    sending = updated
            review = repository.review_settings(owner_id)
            if command.review is not None and command.review != review:
                review = command.review
                repository.save_review_settings(owner_id, review)
            return SettingsView(bilibili=bilibili, llm=llm, review=review, sending=sending)
