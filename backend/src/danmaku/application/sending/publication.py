import asyncio
from collections.abc import Callable
from dataclasses import replace
from time import time
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from danmaku.application.bilibili_login import BilibiliLogin
from danmaku.application.ports import Repository, UnitOfWork
from danmaku.application.projects import Projects
from danmaku.application.sending.service import Sending
from danmaku.application.tasks.state import require_task
from danmaku.domain.bilibili_login import BilibiliLoginSecret
from danmaku.domain.inputs import ProjectParts, VideoPart
from danmaku.domain.publication import OverduePublication, PreloadPart, PreloadProject, Publication
from danmaku.domain.workbench import Principal, Project, ProjectCreate, WorkbenchError

CHECK_INTERVAL = 5
SCHEDULE_CHECK_LEAD = 10
SEND_DELAY = 5


class CreatorSource(Protocol):
    async def scheduled_parts(
        self, credential: BilibiliLoginSecret, bvid: str | None = None,
    ) -> list[tuple[int, VideoPart]]: ...
    async def publication_time(self, credential: BilibiliLoginSecret, bvid: str) -> int | None: ...


class Preload:
    """Stage reviewed deliveries until public visibility is verified or the plan is withdrawn."""

    def __init__(self, database: UnitOfWork, projects: Projects, sending: Sending, login: BilibiliLogin,
                 source: CreatorSource, clock: Callable[[], float] = time, diagnostic=None):
        self.database = database
        self.projects = projects
        self.sending = sending
        self.login = login
        self.source = source
        self.clock = clock
        self.diagnostic = diagnostic or (lambda **fields: None)

    def _existing(self, actor: Principal, bvids: set[str]) -> set[tuple[str, int]]:
        with self.database() as repository:
            return repository.existing_target_parts(actor.id, bvids)

    async def _login(self, actor: Principal, *, force: bool = False) -> tuple[str, BilibiliLoginSecret]:
        status = await self.login.status(actor.id, "primary", force=force)
        account, credential = await run_in_threadpool(self.login.settings.saved_login, actor.id)
        if account is None or credential is None or status.status in {"renewal_required", "unconfigured"}:
            raise WorkbenchError(409, "bilibili_credential_rejected", "请先登录 B 站主账号")
        if status.status != "valid" or status.profile is None or status.profile.account_id != account.account_id:
            raise WorkbenchError(503, "bilibili_publication_unavailable", "暂时无法核实 B 站主账号登录状态", True)
        return account.account_id, credential

    async def candidates(self, actor: Principal) -> list[PreloadPart]:
        _, credential = await self._login(actor)
        parts = await self.source.scheduled_parts(credential)
        existing = await run_in_threadpool(self._existing, actor, {part.bvid for _, part in parts})
        return [PreloadPart(part=part, publish_at=publish_at, already_created=(part.bvid, part.page) in existing)
                for publish_at, part in sorted(parts, key=lambda row: (row[0], row[1].bvid, row[1].page))]

    async def create(self, actor: Principal, command: PreloadProject) -> Project:
        existing = await run_in_threadpool(self._existing, actor, {command.bvid})
        if (command.bvid, command.page) in existing:
            raise WorkbenchError(409, "preload_project_exists", "项目已创建")
        account_id, credential = await self._login(actor)
        parts = await self.source.scheduled_parts(credential, command.bvid)
        selected = next((entry for entry in parts if entry[1].page == command.page), None)
        if selected is None:
            raise WorkbenchError(409, "preload_archive_changed", "稿件或分 P 已变更，请重新选择")
        publish_at, target = selected
        return await run_in_threadpool(
            self.projects.create, actor, ProjectCreate(target_bvid=target.bvid, target_page=target.page),
            ProjectParts(mode="bilibili", target=target), target_part_count=len(parts),
            preload=(account_id, publish_at),
        )

    def recover(self, owner_id: str) -> None:
        with self.database(write=True) as repository:
            for plan in repository.unobserved_publications(owner_id, self.clock()):
                repository.save_publication(replace(plan, overdue=True, due_observed=True, checking=False))

    def overdue(self, actor: Principal) -> str | None:
        with self.database() as repository:
            missed = repository.pending_publications(actor.id, overdue_only=True, first_only=True)
            return missed[0].task_id if missed else None

    async def decide(self, actor: Principal, command: OverduePublication) -> None:
        if command.send:
            await self._login(actor, force=True)
        await run_in_threadpool(self._decide, actor, command)

    def _decide(self, actor: Principal, command: OverduePublication) -> None:
        with self.database(write=True) as repository:
            missed = repository.pending_publications(actor.id, overdue_only=True, first_only=True)
            if not missed:
                return
            if missed[0].task_id != command.task_id:
                raise WorkbenchError(409, "publication_queue_changed", "预装弹幕队列已更新")
            if command.send:
                for plan in repository.pending_publications(actor.id, overdue_only=True):
                    repository.save_publication(replace(plan, overdue=False))
            else:
                for plan in repository.pending_publications(actor.id):
                    self.sending.withdraw(repository, require_task(repository, actor, plan.task_id), return_to_manual=True)

    def _candidate(self, repository: Repository, actor: Principal) -> Publication | None:
        plans = repository.pending_publications(actor.id)
        if not plans or any(plan.overdue for plan in plans):
            return None
        now = self.clock()
        head = plans[0]
        if (head.time_checked and max(head.publish_at + SEND_DELAY, head.check_after) <= now
                and not repository.has_dispatched_publication(actor.id)
                and require_task(repository, actor, head.task_id).task.state == "queued"):
            return head
        for plan in plans:
            if (not plan.time_checked and max(plan.publish_at - SCHEDULE_CHECK_LEAD, plan.check_after) <= now
                    and require_task(repository, actor, plan.task_id).task.state == "queued"):
                return plan
        return None

    def _next(self, actor: Principal) -> Publication | None:
        with self.database() as repository:
            now = self.clock()
            if not repository.unobserved_publications(actor.id, now):
                return self._candidate(repository, actor)
        with self.database(write=True) as repository:
            for plan in repository.unobserved_publications(actor.id, now):
                repository.save_publication(replace(plan, due_observed=True))
            return self._candidate(repository, actor)

    def _finish(self, actor: Principal, plan: Publication, new_time: int | None) -> None:
        with self.database(write=True) as repository:
            if repository.publication(plan.project_id) != plan:
                return
            now = self.clock()
            record = require_task(repository, actor, plan.task_id)
            account = repository.bilibili_account(actor.id)
            if record.task.state != "queued" or account is None or account.account_id != plan.account_id:
                return
            if plan.time_checked:
                if self._candidate(repository, actor) == plan and self.sending.activate_publication(repository, record):
                    self.diagnostic(event="dispatch", task_id=plan.task_id)
                    return
                repository.save_publication(replace(plan, check_after=now + 1))
            elif new_time is None:
                repository.save_publication(replace(plan, checking=True, check_after=now + CHECK_INTERVAL))
            else:
                repository.save_publication(replace(
                    plan, publish_at=new_time, check_after=0, checking=False, checks=0,
                    time_checked=new_time - SCHEDULE_CHECK_LEAD <= now,
                    due_observed=new_time <= now,
                ))

    def _target_bvid(self, project_id: str) -> str | None:
        with self.database() as repository:
            parts = repository.video_parts(project_id)
            return parts.target.bvid if parts is not None and parts.target is not None else None

    async def check_next(self, actor: Principal) -> None:
        plan = await run_in_threadpool(self._next, actor)
        if plan is None:
            return
        try:
            account_id, credential = await self._login(actor, force=True)
        except WorkbenchError as exc:
            self.diagnostic(event="login_error", task_id=plan.task_id, code=exc.info.code)
            await run_in_threadpool(self._retry_later, plan)
            return
        if account_id != plan.account_id:
            self.diagnostic(event="account_mismatch", task_id=plan.task_id)
            await run_in_threadpool(self._retry_later, plan)
            return
        new_time = None
        if not plan.time_checked:
            bvid = await run_in_threadpool(self._target_bvid, plan.project_id)
            if bvid is None:
                return
            try:
                new_time = await self.source.publication_time(credential, bvid)
            except WorkbenchError as exc:
                self.diagnostic(event="schedule_check_error", task_id=plan.task_id, code=exc.info.code)
            self.diagnostic(event="schedule_checked", task_id=plan.task_id, bvid=bvid,
                            previous_time=plan.publish_at, publication_time=new_time)
        await run_in_threadpool(self._finish, actor, plan, new_time)

    def _retry_later(self, plan: Publication) -> None:
        with self.database(write=True) as repository:
            if repository.publication(plan.project_id) == plan:
                repository.save_publication(replace(plan, check_after=self.clock() + CHECK_INTERVAL))

    async def run(self, owner_id: str, poll_interval: float) -> None:
        actor = Principal(id=owner_id)
        while True:
            try:
                await self.check_next(actor)
            except WorkbenchError as exc:
                if exc.info.code != "database_busy":
                    raise
            await asyncio.sleep(poll_interval)
