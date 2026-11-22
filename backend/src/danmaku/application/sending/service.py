import asyncio
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from random import uniform
from typing import Literal, Protocol

from danmaku.application.pagination import decode_cursor, encode_cursor
from danmaku.application.ports import Repository, UnitOfWork
from danmaku.application.projects import duration_mismatch_message, require_project
from danmaku.application.review.service import review_run
from danmaku.application.tasks.state import advance, require_task
from danmaku.domain.inputs import NormalizedItem, REPOST_NOTICE, VideoPart
from danmaku.domain.sending import (
    Attempt, BilibiliAccount, BilibiliAccountRole, BilibiliSendDiagnostic, Delivery, DeliveryFilter, DeliveryPage,
    EffectDifference, LiveSendCooldown,
    ResolveDelivery,
    LIVE_CAPABILITIES, SendBatch, SendItem, SendPreview, SendRate, SendResult, SendTiming, SendWork,
)
from danmaku.domain.workbench import (
    TERMINAL_STATES, ErrorInfo, Principal, SendAccountProgress, SendCheckpoint, SendCreate, SendPriority, Task, TaskControl,
    TaskProgress, TaskRecord, WorkbenchError,
)

SEND_TIMEOUT_SECONDS = 10
SEND_MAX_ATTEMPTS = 4
PUBLICATION_FIRST_MAX_RETRIES = 2
LIVE_FAILURE_THRESHOLD = 3
LIVE_FAILURE_COOLDOWN_SECONDS = 60
UNKNOWN = ErrorInfo(code="delivery_unknown", message="有弹幕需要人工核对")
RATE_LIMIT = ErrorInfo(code="send_rate_limited", message="发送限流，等待重试", retryable=True)
REPEATED_FAILURES = ErrorInfo(code="send_repeated_failures", message="连续发送失败，正在延长冷却", retryable=True)
FAILED_REVIEW = ErrorInfo(code="send_failed_review", message="本批次有失败弹幕，请选择重试或继续下一批")
PARTIAL = ErrorInfo(code="send_partial", message="部分投递失败")
BLOCKED_MESSAGES = {
    "bilibili_credential_missing": "请重新保存该账号的登录凭据",
    "bilibili_credential_rejected": "B 站未确认登录，请更新该账号的凭据",
    "bilibili_account_changed": "请切回批次确认的 B 站账号",
    "bilibili_access_denied": "B 站拒绝发送或要求验证，请核对账号与平台提示",
    "bilibili_sdk_missing": "请安装 B 站接入依赖后重启",
    "credential_store_unavailable": "暂时无法读取 B 站登录凭据",
}


class Sender(Protocol):
    """Send one frozen item to a fixed CID with the supplied account reference.

    Return a confirmed opaque receipt, rejected, limited (definitely not delivered), or unknown.
    A deferred result means the request was never sent. Timeout and unexpected exceptions are ambiguous.
    """

    async def send(self, work: SendWork) -> SendResult: ...


async def execute_send(sender: Sender, work: SendWork) -> SendResult:
    try:
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            return await sender.send(work)
    except TimeoutError:
        return SendResult(outcome="unknown")


def freeze_item(item: NormalizedItem, offset_ms: int) -> SendItem:
    capabilities = LIVE_CAPABILITIES
    differences: list[EffectDifference] = []
    if item.mode not in capabilities.modes:
        differences.append("unsupported_mode")
    if item.color_rgb is None:
        differences.append("unsupported_color")
    return normalize_send_effects(SendItem(item_id=item.source_key, delivery_key=item.delivery_key,
                    time_ms=item.time_ms + offset_ms, text=item.text, mode=item.mode,
                    checkbox_type=item.checkbox_type,
                    color_rgb=item.color_rgb, font_size=item.font_size, effect_differences=tuple(differences)))


def normalize_send_effects(item: SendItem) -> SendItem:
    sizes = LIVE_CAPABILITIES.font_sizes
    font_size = item.font_size
    differences = item.effect_differences
    if sizes is not None and font_size not in sizes:
        font_size = 25
        differences = tuple(effect for effect in differences if effect != "unsupported_font_size")
    if item.mode in LIVE_CAPABILITIES.modes:
        differences = tuple(effect for effect in differences if effect != "unsupported_mode")
    if font_size == item.font_size and differences == item.effect_differences:
        return item
    return item.model_copy(update={
        "font_size": font_size,
        "effect_differences": differences,
    })


def has_stable_send_key(item: SendItem) -> bool:
    return item.stable_key.startswith("id:") or item.stable_key == REPOST_NOTICE.source_key


def delivery_preflight_error(item: SendItem, confirmed: set[str]) -> str | None:
    if item.stable_key in confirmed:
        return "duplicate_live_delivery"
    if item.effect_differences:
        return "unsupported_effect"
    if not has_stable_send_key(item):
        return "unstable_source"
    return None


def can_share_item(item: SendItem, level: int) -> bool:
    return not item.checkbox_type and (
        level >= 3 or (level >= 1 and item.mode == 1 and (level == 2 or item.color_rgb == 0xFFFFFF))
    )


def assign_accounts(items: list[SendItem], confirmed: set[str], level: int) -> list[SendItem]:
    sendable = [item for item in items if delivery_preflight_error(item, confirmed) is None]
    eligible = [item for item in sendable if can_share_item(item, level)]
    if level >= 3:
        secondary = {item.item_id for item in eligible[1::2]}
    else:
        # Reserve half the whole batch, including rows that only the primary can send.
        quota = min(len(eligible), len(sendable) // 2)
        candidates = {item.item_id for item in eligible}
        secondary = set()
        remaining = len(candidates)
        for index, item in enumerate(sendable):
            if item.item_id in candidates:
                primary_count = index - len(secondary)
                if quota and (remaining == quota or primary_count > len(secondary)):
                    secondary.add(item.item_id)
                    quota -= 1
                remaining -= 1
    return [item.model_copy(update={"account_role": "secondary" if item.item_id in secondary else "primary"})
            if delivery_preflight_error(item, confirmed) is None else item for item in items]


def delivery_progress(progress: TaskProgress, *, done=0, failed=0, skipped=0, unknown=0, withdrawn=0) -> TaskProgress:
    return progress.model_copy(update={
        "done": progress.done + done, "failed": progress.failed + failed, "skipped": progress.skipped + skipped,
        "unknown": progress.unknown + unknown, "withdrawn": progress.withdrawn + withdrawn,
    })


def awaiting_failure_decision(task: Task) -> bool:
    return task.state == "paused" and task.error is not None and task.error.code == FAILED_REVIEW.code


class Sending:
    def _secondary(self, repository: Repository, owner_id: str) -> BilibiliAccount | None:
        if not repository.sending_settings(owner_id).dual_account_enabled:
            return None
        account = repository.bilibili_account(owner_id, "secondary")
        if account is not None and account.level and (
            self.has_saved_credential is None or self.has_saved_credential(account)
        ):
            return account
        return None

    def send_rate(self, actor: Principal) -> SendRate | None:
        with self.database() as repository:
            account = repository.bilibili_account(actor.id)
            if account is None:
                return None
            head = repository.send_queue_head(actor.id)
            if head is None:
                return None
            assert isinstance(head.checkpoint, SendCheckpoint)
            resuming = head.state == "queued" and head.checkpoint.started and not head.checkpoint.waiting_after_priority
            if not resuming and head.state not in {"running", "backoff", "pausing"}:
                return None
            batch = repository.send_batch(head.id)
            if batch.account_id != account.account_id:
                return None
            timing = repository.send_timing(actor.id)
            rates: dict[str, float] = {}
            for role, account_id in batch.accounts().items():
                status = head.checkpoint.accounts.get(role)
                if (role == "secondary" and head.checkpoint.primary_only) or (status and status.state == "finished"):
                    continue
                if account_id in rates:
                    continue
                rate = self._account_send_rate(repository, actor.id, account_id, timing)
                if rate is None:
                    return None
                rates[account_id] = rate
        return SendRate(task_id=head.id, items_per_hour=sum(rates.values())) if rates else None

    def _account_send_rate(
        self, repository: Repository, owner_id: str, account_id: str, timing: SendTiming,
    ) -> float | None:
        attempts = repository.recent_send_attempts(owner_id, account_id, 1000)
        confirmed = sum(attempt.outcome == "confirmed" or attempt.resolution == "confirmed"
                        for attempt in attempts)
        interval = (timing.item_min_seconds + timing.item_max_seconds) / 2
        configured_rate = 3600 / interval if interval > 0 else None
        if confirmed < 100:
            return configured_rate
        historical = [attempt for attempt in attempts[:-1] if attempt.pacing_wait_seconds is None]
        deadlines = repository.historical_send_deadlines(
            owner_id, account_id, historical[0].started_at, attempts[-1].started_at,
        ) if historical else []
        seconds = 0.0
        for attempt, following in zip(attempts, attempts[1:]):
            if attempt.pacing_wait_seconds is not None:
                seconds += attempt.pacing_wait_seconds
            else:
                seconds += self._historical_wait(attempt, following.started_at, deadlines, timing)
        if seconds <= 0:
            return configured_rate
        return (confirmed - 1) * 3600 / seconds

    @staticmethod
    def _historical_wait(
        attempt: Attempt, next_start: datetime, deadlines: list[datetime], timing: SendTiming,
    ) -> float:
        finished = attempt.finished_at
        if finished is None or finished >= next_start:
            return 0
        index = bisect_right(deadlines, finished)
        if index < len(deadlines) and deadlines[index] <= next_start:
            return (deadlines[index] - finished).total_seconds()
        # Older batch endings may have no persisted deadline. Estimate the pacing
        # wait using the current settings, capped by the actual inter-attempt gap.
        delay = (timing.item_min_seconds + timing.item_max_seconds) / 2
        if attempt.outcome == "limited":
            delay = max(delay, timing.rate_limit_seconds)
        return min(delay, (next_start - finished).total_seconds())

    def get_timing(self, actor: Principal) -> SendTiming:
        with self.database() as repository:
            return repository.send_timing(actor.id)

    def put_timing(self, actor: Principal, command: SendTiming) -> SendTiming:
        with self.database(write=True) as repository:
            repository.save_send_timing(actor.id, command)
        return command

    def __init__(
        self, database: UnitOfWork, clock: Callable[[], datetime],
        mock_part: Callable[[str, int], VideoPart] | None = None,
        *, has_saved_credential: Callable[[BilibiliAccount], bool] | None = None,
    ):
        self.database = database
        self.clock = clock
        self.mock_part = mock_part
        self.has_saved_credential = has_saved_credential

    def _selection(
        self, repository: Repository, actor: Principal, project_id: str, run_id: str,
        expected_revision: int | None = None,
    ):
        project = require_project(repository, actor, project_id)
        run = review_run(repository, project_id, run_id)
        revision = repository.review_revision(project_id)
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchError(409, "revision_conflict", "审核版本已更新")
        snapshot = repository.snapshot(project_id, run.snapshot_id)
        assert snapshot is not None
        parts = repository.video_parts(project_id)
        if parts is not None and (snapshot.mode == "mock" or parts.mode == "bilibili"):
            target = parts.target
        elif snapshot.mode == "mock" and self.mock_part is not None:
            target = self.mock_part(project.target_bvid, project.target_page)
        else:
            raise WorkbenchError(409, "target_part_missing", "项目缺少已解析的目标分 P")
        duration = snapshot.part.duration_ms
        frozen = [
            (freeze_item(item, offset_ms), item.diagnostic != "outside_video"
             and 0 <= item.time_ms and (duration is None or item.time_ms < duration))
            for item, offset_ms in repository.selected_items(run)
        ]
        frozen.sort(key=lambda entry: (entry[0].time_ms, entry[0].item_id))
        items = [item for item, _ in frozen]
        outside = [item for item, source_valid in frozen if not source_valid
                   or not 0 <= item.time_ms < target.duration_ms]
        live_target = parts is not None and parts.mode == "bilibili"
        return run, revision, snapshot, target, items, outside, live_target

    def preview(
        self, actor: Principal, project_id: str, run_id: str, cursor: str | None, limit: int,
    ) -> SendPreview:
        with self.database() as repository:
            run, revision, snapshot, target, selected, outside, live_target = self._selection(
                repository, actor, project_id, run_id,
            )
            source = snapshot.part
            source_parts = [source]
            if snapshot.members:
                parts_by_id = {entry.id: entry.part for entry in repository.snapshots(project_id)}
                source_parts = [parts_by_id[member] for member in snapshot.members if member in parts_by_id] or [source]
            keys = repository.live_send_keys(project_id, target.cid)
            secondary = self._secondary(repository, actor.id) if repository.bilibili_account(actor.id) else None
            level = secondary.level if secondary else None
            items = assign_accounts(selected, keys, level or 0) if live_target and not outside else selected
            secondary_id = secondary.account_id if secondary else None
            for record in repository.send_queue_records(actor.id, project_id):
                batch = repository.send_batch(record.task.id)
                if batch.run_id != run_id or batch.review_revision != revision or outside:
                    continue
                checkpoint = record.task.checkpoint
                assert isinstance(checkpoint, SendCheckpoint)
                secondary_id = None if checkpoint.primary_only else batch.secondary_account_id
                level = level if secondary is not None and secondary.account_id == secondary_id else None
                rows = repository.deliveries(batch.id, None, "", record.task.progress.total)
                roles = {
                    row.item.item_id: (
                        "primary" if checkpoint.primary_only and row.status == "pending"
                        else (row.attempts[-1].account_role or row.item.account_role)
                        if row.attempts and row.status != "pending" else row.item.account_role
                    ) for row in rows
                }
                items = [item.model_copy(update={"account_role": roles.get(item.item_id, item.account_role)})
                         for item in items]
                break
            samples = outside or items
            preflight = Counter(delivery_preflight_error(item, keys) for item in items)
            scope = ["send-preview", project_id, run_id, str(revision), "live", secondary_id or "", str(level)]
            after = decode_cursor(scope, cursor, valid_id=lambda value: value.isascii() and value.isdecimal()
                                  and len(value) <= 12 and 0 < int(value) < len(samples))
            start = int(after) if after else 0
            shown = samples[start:start + limit]
            end = start + len(shown)
            next_cursor = encode_cursor(scope, str(end)) if end < len(samples) else None
            cues = []
            preview_subtitle_id = run.subtitle_version_id or run.japanese_subtitle_version_id
            if shown and preview_subtitle_id:
                timestamp = shown[0].time_ms
                near = (cue for cue in repository.cues(preview_subtitle_id)
                        if cue.start_ms <= timestamp + 5000 and cue.end_ms >= timestamp - 5000)
                cues = sorted(near, key=lambda cue: (cue.start_ms, cue.order))[:5]
            mismatch = duration_mismatch_message(source, target)
            return SendPreview(revision=revision, source=source, source_parts=source_parts, target=target,
                               selected_count=len(items),
                               outside_count=len(outside), unsupported_count=preflight["unsupported_effect"],
                               live_target=live_target, duration_mismatch_message=mismatch,
                               duplicate_count=preflight["duplicate_live_delivery"],
                               unstable_count=preflight["unstable_source"],
                               secondary_account_id=secondary_id, secondary_level=level,
                               items=shown, next_cursor=next_cursor, cues=cues)

    def prepare(
        self, repository: Repository, actor: Principal, project_id: str, task_id: str, command: SendCreate,
    ) -> tuple[SendBatch, list[Delivery]]:
        if repository.has_project_send_in_queue(project_id):
            raise WorkbenchError(409, "send_active", "投递任务正在进行，请先取消该投递")
        run, revision, _, target, selected, outside, live_target = self._selection(
            repository, actor, project_id, command.run_id, command.expected_review_revision,
        )
        if not selected:
            raise WorkbenchError(409, "empty_selection", "此运行没有已勾选的弹幕")
        if outside:
            raise WorkbenchError(409, "outside_video", f"有 {len(outside)} 条已选弹幕超出视频时间轴，请检查预览")
        if not live_target:
            raise WorkbenchError(409, "bilibili_target_required", "真实投递需要已解析的 B 站目标分 P")
        assert command.confirmation is not None
        if command.confirmation.target_cid != target.cid:
            raise WorkbenchError(409, "target_confirmation_mismatch", "确认的目标 CID 与冻结分 P 不符")
        account = repository.bilibili_account(actor.id)
        if account is None:
            raise WorkbenchError(409, "bilibili_credential_missing", "请先保存 B 站登录凭据")
        if command.confirmation.account_id != account.account_id:
            raise WorkbenchError(409, "bilibili_account_changed", BLOCKED_MESSAGES["bilibili_account_changed"])
        confirmation = command.confirmation
        secondary = None if confirmation.primary_only else self._secondary(repository, actor.id)
        secondary_id = secondary.account_id if secondary else None
        level = secondary.level if secondary else None
        if ({"secondary_account_id", "secondary_level"} & confirmation.model_fields_set
                and (confirmation.secondary_account_id, confirmation.secondary_level) != (secondary_id, level)):
            raise WorkbenchError(409, "send_accounts_changed", "次账号或等级已更新，请重新确认投递预览")
        keys = repository.live_send_keys(project_id, target.cid)
        batch = SendBatch(id=task_id, run_id=run.id, review_revision=revision, target=target,
                          mode="live", account_id=account.account_id, secondary_account_id=secondary_id)
        deliveries = []
        for position, frozen in enumerate(assign_accounts(selected, keys, level or 0), 1):
            error = delivery_preflight_error(frozen, keys)
            status = "skipped" if error == "duplicate_live_delivery" else "failed" if error else "pending"
            deliveries.append(Delivery(id=f"{position:012d}", item=frozen, status=status, error_code=error))
        return batch, deliveries

    def use_primary_only(self, actor: Principal, task_id: str) -> Task:
        """Stop assigning work to the secondary account while existing requests settle."""
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            batch = repository.send_batch(task_id)
            checkpoint = task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            if batch.mode != "live" or task.state in TERMINAL_STATES | {"cancelling"}:
                raise WorkbenchError(409, "send_primary_only_unavailable", "当前投递无法切换发送账号")
            if checkpoint.secondary_upgrade_id is None and (checkpoint.primary_only or batch.secondary_account_id is None):
                return task
            in_flight = self._in_flight(repository, task_id)
            states, _ = self._account_schedule(repository, batch, in_flight, self.clock(), primary_only=True)
            checkpoint = checkpoint.model_copy(update={
                "primary_only": True, "secondary_upgrade_id": None,
                "accounts": states if batch.secondary_account_id is not None else {},
            })
            if task.state in {"paused", "pausing"}:
                return advance(repository, record, checkpoint=checkpoint)
            if task.state == "queued":
                return advance(repository, record, checkpoint=checkpoint, next_attempt_at=None)
            record.task = task.model_copy(update={"checkpoint": checkpoint})
            return self._advance_schedule(repository, record, batch, states, task.progress,
                                          ready_state="running" if in_flight else "queued")

    def _upgrade_candidates(self, repository: Repository, owner_id: str, primary_id: str):
        for record in repository.send_queue_records(owner_id):
            task = record.task
            checkpoint = task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            if task.state in {"pausing", "cancelling"} or task.progress.done + task.progress.unknown == task.progress.total:
                continue
            batch = repository.send_batch(task.id)
            if (batch.mode == "live" and batch.account_id == primary_id
                    and (batch.secondary_account_id is None or checkpoint.primary_only)):
                yield record

    def dual_upgrade_count(self, actor: Principal) -> int:
        with self.database() as repository:
            primary = repository.bilibili_account(actor.id)
            if primary is None or self._secondary(repository, actor.id) is None:
                return 0
            return sum(1 for _ in self._upgrade_candidates(repository, actor.id, primary.account_id))

    def request_dual_upgrade(self, actor: Principal, secondary_id: str) -> int:
        """Upgrade eligible queue entries atomically, or persist the request until the in-flight receipt settles."""
        with self.database(write=True) as repository:
            primary = repository.bilibili_account(actor.id)
            secondary = self._secondary(repository, actor.id)
            if primary is None or secondary is None or secondary.account_id != secondary_id:
                raise WorkbenchError(409, "send_accounts_changed", "发送账号已更新，请重新确认")
            count = 0
            for record in self._upgrade_candidates(repository, actor.id, primary.account_id):
                checkpoint = record.task.checkpoint
                if checkpoint.secondary_upgrade_id == secondary_id:
                    continue
                advance(repository, record, checkpoint=checkpoint.model_copy(update={"secondary_upgrade_id": secondary_id}))
                count += 1
            self.apply_queue_upgrades(repository, actor.id)
            return count

    def apply_queue_upgrades(
        self, repository: Repository, owner_id: str, current: TaskRecord | None = None,
    ) -> None:
        requests = repository.send_queue_records(owner_id, upgrades_only=True)
        if not requests:
            return
        head = repository.send_queue_head(owner_id)
        if head is not None and self._in_flight(repository, head.id):
            return
        for record in requests:
            if current is not None and current.task.id == record.task.id:
                record = current
            self._apply_dual_upgrade(repository, record)

    def _apply_dual_upgrade(self, repository: Repository, record: TaskRecord) -> Task:
        task = record.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if checkpoint.secondary_upgrade_id is None or self._in_flight(repository, task.id):
            return task
        checkpoint = checkpoint.model_copy(update={"secondary_upgrade_id": None})
        if task.state in TERMINAL_STATES | {"cancelling", "pausing"}:
            return advance(repository, record, checkpoint=checkpoint)
        batch = repository.send_batch(task.id)
        primary = repository.bilibili_account(record.created_by)
        try:
            secondary = self._secondary(repository, record.created_by)
        except WorkbenchError as exc:
            if exc.info.code != "credential_store_unavailable":
                raise
            secondary = None
        if (primary is None or primary.account_id != batch.account_id or secondary is None
                or secondary.account_id != task.checkpoint.secondary_upgrade_id):
            return advance(repository, record, checkpoint=checkpoint)
        pending = repository.deliveries(task.id, "pending", "", task.progress.total)
        if not pending:
            return advance(repository, record, checkpoint=checkpoint)
        confirmed = repository.live_send_keys(task.project_id, batch.target.cid)
        for row, item in zip(pending, assign_accounts([row.item for row in pending], confirmed, secondary.level or 0)):
            if row.item.account_role == item.account_role:
                continue
            self._reassign_pending(repository, task.id, row, item)
        batch = batch.model_copy(update={"secondary_account_id": secondary.account_id})
        repository.save_send_batch(batch)
        states, _ = self._account_schedule(repository, batch, {}, self.clock(), primary_only=False)
        checkpoint = checkpoint.model_copy(update={"primary_only": False, "accounts": states})
        if task.state == "paused":
            return advance(repository, record, checkpoint=checkpoint)
        if task.state == "queued":
            return advance(repository, record, checkpoint=checkpoint, next_attempt_at=None)
        # Let the worker claim the task again with its new account schedule.
        record.task = task.model_copy(update={"checkpoint": checkpoint})
        return self._advance_schedule(repository, record, batch, states, task.progress, ready_state="queued")

    def needs_primary_fallback(self, repository: Repository, record: TaskRecord) -> bool:
        checkpoint = record.task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if checkpoint.primary_only:
            return False
        secondary_id = repository.send_batch(record.task.id).secondary_account_id
        if secondary_id is None:
            return False
        try:
            secondary = self._secondary(repository, record.created_by)
        except WorkbenchError as exc:
            if exc.info.code != "credential_store_unavailable":
                raise
            return True
        return secondary is None or secondary.account_id != secondary_id

    @staticmethod
    def _reassign_pending(repository: Repository, task_id: str, row: Delivery, item: SendItem) -> None:
        assert row.status == "pending"
        # Older attempts may rely on the original delivery role.
        for attempt in row.attempts:
            if attempt.account_role is None:
                attempt.account_role = row.item.account_role or "primary"
                repository.save_attempt(task_id, row.id, attempt)
        row.item = item
        repository.save_delivery(task_id, row)

    def idle_dual_role(self, repository: Repository, record: TaskRecord,
                       batch: SendBatch) -> BilibiliAccountRole | None:
        checkpoint = record.task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if (batch.secondary_account_id is None or checkpoint.primary_only or checkpoint.secondary_upgrade_id
                or checkpoint.next_send_task_id):
            return None
        if repository.next_priority_resend(batch.id) is not None:
            return None
        in_flight = self._in_flight(repository, batch.id)
        idle = [role for role in batch.accounts()
                if role not in in_flight and repository.next_pending_delivery(batch.id, role) is None]
        return idle[0] if len(idle) == 1 and not self.needs_primary_fallback(repository, record) else None

    def pending_share(self, repository: Repository, record: TaskRecord, batch: SendBatch,
                      idle: BilibiliAccountRole) -> list[Delivery]:
        task = record.task
        account = repository.bilibili_account(record.created_by, idle)
        if account is None or account.account_id != batch.accounts()[idle] or not account.level:
            return []
        donor = "secondary" if idle == "primary" else "primary"
        if repository.next_pending_delivery(task.id, donor) is None:
            return []
        in_flight = self._in_flight(repository, task.id)
        confirmed = repository.live_send_keys(task.project_id, batch.target.cid)
        pending = [row for row in repository.deliveries(task.id, "pending", "", task.progress.total, role=donor)
                   if delivery_preflight_error(row.item, confirmed) is None]
        active_keys = {row.item.stable_key for row in in_flight.values()}
        eligible = [row for row in pending
                    if row.item.stable_key not in active_keys and can_share_item(row.item, account.level)]
        if len(pending) == 1 and not in_flight and eligible:
            # Transfer the last item only when it can go sooner, so two idle accounts cannot swap it back.
            now = self.clock()
            waits = {role: repository.live_cooldown(uid) for role, uid in batch.accounts().items()}
            ready_at = {role: max(now, wait.deadline) if wait else now for role, wait in waits.items()}
            if ready_at[idle] >= ready_at[donor]:
                return []
        # Give the idle account the extra item when the remaining counts cannot be equal.
        quota = (len(pending) + len(in_flight) + 1) // 2
        return eligible[:quota]

    def share_pending(self, repository: Repository, record: TaskRecord, batch: SendBatch,
                      idle: BilibiliAccountRole) -> bool:
        rows = self.pending_share(repository, record, batch, idle)
        if not rows:
            return False
        task = record.task
        for row in rows:
            self._reassign_pending(repository, task.id, row, row.item.model_copy(update={"account_role": idle}))
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        record.task = task.model_copy(update={
            "checkpoint": checkpoint.model_copy(update={"share_count": checkpoint.share_count + 1}),
        })
        states, _ = self._account_schedule(repository, batch, self._in_flight(repository, task.id), self.clock(),
                                           primary_only=False)
        self._advance_schedule(repository, record, batch, states, task.progress,
                               ready_state="running" if task.state == "running" else "queued")
        return True

    def start(
        self, repository: Repository, record: TaskRecord, *, only_role: BilibiliAccountRole | None = None,
        active: tuple[SendWork, ...] = (),
    ) -> SendWork | None:
        task = record.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if checkpoint.secondary_upgrade_id is not None:
            if self._in_flight(repository, task.id):
                return None
            self.apply_queue_upgrades(repository, record.created_by, record)
            task = record.task
            if task.state != "running":
                return None
            checkpoint = task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
        batch = repository.send_batch(task.id)
        assert batch.mode == "live" and batch.account_id is not None
        if self.needs_primary_fallback(repository, record):
            checkpoint = checkpoint.model_copy(update={"primary_only": True})
            task = advance(repository, record, checkpoint=checkpoint)
        accounts = batch.accounts()
        in_flight = self._in_flight(repository, task.id)
        if in_flight and (len(in_flight) == len(accounts) or checkpoint.next_send_task_id):
            return None
        if checkpoint.publication_first_pending and in_flight:
            return None
        active_accounts = {work.account_id for work in active}
        active_keys = {row.item.stable_key for row in in_flight.values()}
        for work in active:
            if work.task_id != task.id and work.target_cid == batch.target.cid:
                other = repository.task(work.task_id)
                if other is not None and other.task.project_id == task.project_id:
                    active_keys.add(work.item.stable_key)
        progress = task.progress
        now = self.clock()
        if checkpoint.publication_first_pending and checkpoint.publication_retry_at is not None:
            if checkpoint.publication_retry_at > now:
                advance(repository, record, state="backoff", next_attempt_at=checkpoint.publication_retry_at)
                return None
        while True:
            states, pending = self._account_schedule(repository, batch, in_flight, now, primary_only=checkpoint.primary_only)
            if not pending and not in_flight:
                assert progress.done + progress.unknown == progress.total
                self._finish(repository, record, progress, allow_auto_retry=True)
                return None
            if only_role is not None:
                pending = {role: row for role, row in pending.items() if role == only_role}
            if checkpoint.publication_first_pending and pending:
                first_role = min(pending, key=lambda role: pending[role].id)
                pending = {first_role: pending[first_role]}
                states = {first_role: states[first_role]}
            blocked = None
            for row in pending.values():
                row.item = normalize_send_effects(row.item)
                reason = delivery_preflight_error(row.item, set())
                if repository.has_live_send_key(task.project_id, batch.target.cid, row.item.stable_key):
                    reason = "duplicate_live_delivery"
                if reason:
                    blocked = row
                    break
            if blocked is not None:
                duplicate = reason == "duplicate_live_delivery"
                blocked.status = "skipped" if duplicate else "failed"
                blocked.error_code = reason
                blocked.bilibili_error = None
                blocked.clear_resend()
                repository.save_delivery(task.id, blocked)
                progress = delivery_progress(progress, done=1, skipped=int(duplicate), failed=int(not duplicate))
                continue
            ready = [(role, row) for role, row in pending.items() if states[role].state == "ready"
                     and accounts[role] not in active_accounts and row.item.stable_key not in active_keys]
            if not ready:
                if only_role is not None:
                    if progress != task.progress:
                        advance(repository, record, progress=progress)
                elif (progress != task.progress or (batch.secondary_account_id and states != checkpoint.accounts)
                      or (not in_flight and not any(state.state == "ready" for state in states.values()))):
                    self._advance_schedule(repository, record, batch, states, progress)
                return None
            role, delivery = min(ready, key=lambda entry: entry[1].id)
            break
        attempt = Attempt(number=len(delivery.attempts) + 1, started_at=now,
                          account_role=role, account_id=accounts[role])
        delivery.status = "in_flight"
        repository.save_delivery(task.id, delivery)
        repository.save_attempt(task.id, delivery.id, attempt)
        if only_role is not None:
            record.task = task.model_copy(update={"checkpoint": checkpoint.model_copy(
                update={"started": True, "waiting_after_priority": False},
            )})
        states[role] = SendAccountProgress(state="sending")
        self._advance_schedule(repository, record, batch, states, progress)
        return SendWork(task_id=task.id, delivery_id=delivery.id, target_cid=batch.target.cid,
                        target_bvid=batch.target.bvid, account_ref=record.created_by, item=delivery.item,
                        attempt=attempt.number, account_id=accounts[role], account_role=role)

    @staticmethod
    def _in_flight(repository: Repository, task_id: str) -> dict[BilibiliAccountRole, Delivery]:
        return {row.attempts[-1].account_role or row.item.account_role or "primary": row
                for row in repository.deliveries(task_id, "in_flight", "", 2)}

    def _account_schedule(
        self, repository: Repository, batch: SendBatch, in_flight: dict[BilibiliAccountRole, Delivery], now: datetime,
        *, primary_only: bool,
    ) -> tuple[dict[BilibiliAccountRole, SendAccountProgress], dict[BilibiliAccountRole, Delivery]]:
        states = {}
        pending = {}
        priority = repository.next_priority_resend(batch.id)
        priority_rows: dict[BilibiliAccountRole, Delivery] = {}
        if priority is not None and not in_flight:
            # A limited or deferred request can return to pending before its final receipt.
            blockers = repository.deliveries_by_id(batch.id, set(priority.resend_wait_for)) if priority.resend_wait_for else []
            waiting = [row for row in blockers if row.status == "pending"]
            for row in waiting or [priority]:
                role = "primary" if primary_only else row.item.account_role or "primary"
                priority_rows.setdefault(role, row)
        for role, account_id in batch.accounts().items():
            if role in in_flight:
                states[role] = SendAccountProgress(state="sending")
                continue
            if priority is not None:
                row = priority_rows.get(role)
            elif primary_only or batch.secondary_account_id is None:
                row = repository.next_pending_delivery(batch.id, None) if role == "primary" else None
            else:
                row = repository.next_pending_delivery(batch.id, role)
            if row is None:
                states[role] = SendAccountProgress(state="finished")
                continue
            pending[role] = row
            cooldown = repository.live_cooldown(account_id)
            states[role] = (SendAccountProgress(state="waiting", next_attempt_at=cooldown.deadline,
                                               error=self._cooldown_error(cooldown))
                            if cooldown and cooldown.deadline > now else SendAccountProgress(state="ready"))
        return states, pending

    @staticmethod
    def _advance_schedule(
        repository: Repository, record: TaskRecord, batch: SendBatch,
        states: dict[BilibiliAccountRole, SendAccountProgress], progress: TaskProgress,
        *, ready_state: Literal["queued", "running"] = "running",
    ) -> Task:
        checkpoint = record.task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        dual = batch.secondary_account_id is not None
        if dual:
            checkpoint = checkpoint.model_copy(update={"accounts": states})
        waiting = [state for state in states.values() if state.next_attempt_at is not None]
        if waiting and not any(state.state in {"ready", "sending"} for state in states.values()):
            next_account = min(waiting, key=lambda state: state.next_attempt_at)
            return advance(repository, record, state="backoff",
                           next_attempt_at=next_account.next_attempt_at,
                           error=next_account.error if not dual else None,
                           checkpoint=checkpoint, progress=progress)
        return advance(repository, record, state=ready_state, error=None, next_attempt_at=None,
                       checkpoint=checkpoint, progress=progress)

    def resume(self, repository: Repository, record: TaskRecord) -> Task:
        """Resume a paused batch in place, restoring any active cooldown for the started head."""
        task = record.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if checkpoint.started:
            head = repository.send_queue_head(record.created_by)
            if head is not None and head.id == task.id:
                batch = repository.send_batch(task.id)
                if batch.secondary_account_id is not None:
                    states, _ = self._account_schedule(repository, batch, {}, self.clock(), primary_only=checkpoint.primary_only)
                    record.task = task.model_copy(update={"checkpoint": checkpoint.model_copy(
                        update={"waiting_after_priority": False},
                    )})
                    return self._advance_schedule(repository, record, batch, states, task.progress, ready_state="queued")
                account_id = batch.account_id
                cooldown = repository.live_cooldown(account_id) if account_id is not None else None
                if cooldown is not None and cooldown.deadline > self.clock():
                    return advance(repository, record, state="backoff", next_attempt_at=cooldown.deadline,
                                   error=self._cooldown_error(cooldown),
                                   checkpoint=checkpoint.model_copy(update={"waiting_after_priority": False}))
        return advance(repository, record, state="queued", error=None)

    @staticmethod
    def _cooldown_error(cooldown: LiveSendCooldown) -> ErrorInfo | None:
        if cooldown.reason == "repeated_failures":
            return REPEATED_FAILURES
        return RATE_LIMIT if cooldown.reason == "rate_limit" else None

    @staticmethod
    def _save_live_wait(
        repository: Repository, account_id: str, timing: SendTiming, attempt: Attempt,
        rate_limit_delay: float | None = None, limited_attempts: int = 0,
    ) -> LiveSendCooldown:
        now = attempt.finished_at
        assert now is not None
        outcome = attempt.outcome
        previous = repository.live_cooldown(account_id)
        failures = previous.consecutive_failures if previous else 0
        delay = uniform(timing.item_min_seconds, timing.item_max_seconds)
        attempt.pacing_wait_seconds = max(delay, rate_limit_delay or 0)
        reason = None
        if outcome == "confirmed":
            failures = 0
        elif outcome in {"limited", "rejected"}:
            failures += 1
            if rate_limit_delay is not None:
                delay = max(delay, rate_limit_delay)
                reason = "rate_limit"
            if failures >= LIVE_FAILURE_THRESHOLD or limited_attempts >= LIVE_FAILURE_THRESHOLD:
                delay = max(delay, LIVE_FAILURE_COOLDOWN_SECONDS)
                reason = "repeated_failures"
                failures = 0
        deadline = now + timedelta(seconds=delay)
        if previous and previous.deadline > deadline:
            deadline, reason = previous.deadline, previous.reason
        cooldown = LiveSendCooldown(deadline, failures, reason)
        repository.save_live_cooldown(account_id, cooldown)
        return cooldown

    def recover(self, repository: Repository, record: TaskRecord) -> bool:
        in_flight = self._in_flight(repository, record.task.id)
        for delivery in in_flight.values():
            self._unknown(repository, record, delivery, recovering=True)
        return bool(in_flight)

    def cancel(self, actor: Principal, task_id: str, command: TaskControl) -> Task:
        """Withdraw pending items atomically and let any in-flight receipt settle."""
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            if task.state in {"cancelling", "cancelled"}:
                return task
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            if task.state in TERMINAL_STATES or repository.send_batch(task_id).mode != "live":
                raise WorkbenchError(409, "send_cancel_unavailable", "当前批次无法取消投递")
            head = repository.send_queue_head(actor.id)
            if head and head.id != task_id and head.checkpoint.next_send_task_id == task_id:
                active = require_task(repository, actor, head.id)
                self._settle_priority(repository, active, swap=False)
                advance(repository, active)
            self._settle_priority(repository, record, swap=False)
            return self.withdraw(repository, record)

    def withdraw(self, repository: Repository, record: TaskRecord, *, return_to_manual: bool = False) -> Task:
        task = record.task
        withdrawn = repository.withdraw_pending_deliveries(task.id)
        progress = delivery_progress(task.progress, done=withdrawn, withdrawn=withdrawn)
        state = "cancelling" if repository.count_deliveries(task.id, "in_flight") else "cancelled"
        checkpoint = self._finished_accounts_checkpoint(record.task) if state == "cancelled" else record.task.checkpoint
        if return_to_manual:
            checkpoint = checkpoint.model_copy(update={
                "publication": "returned", "publication_first_pending": False, "publication_retry_at": None,
            })
        elif checkpoint.publication == "pending":
            checkpoint = checkpoint.model_copy(update={"publication": None})
        return advance(repository, record, state=state, progress=progress, next_attempt_at=None,
                       checkpoint=checkpoint, error=UNKNOWN if progress.unknown else None)

    def _settle_cancelled_delivery(
        self, repository: Repository, record: TaskRecord, delivery: Delivery, attempt: Attempt,
    ) -> Task:
        task = record.task
        withdrawn = int(delivery.status != "confirmed")
        if withdrawn:
            delivery.status = "withdrawn"
            delivery.error_code = None
            delivery.bilibili_error = None
        delivery.clear_resend()
        repository.save_delivery(task.id, delivery)
        repository.save_attempt(task.id, delivery.id, attempt)
        state = "cancelling" if self._in_flight(repository, task.id) else "cancelled"
        checkpoint = self._finished_accounts_checkpoint(task) if state == "cancelled" else task.checkpoint
        return advance(repository, record, state=state, checkpoint=checkpoint,
                       progress=delivery_progress(task.progress, done=1, withdrawn=withdrawn))

    def settle(self, repository: Repository, record: TaskRecord, work: SendWork, result: SendResult) -> Task:
        task = record.task
        delivery = repository.delivery(task.id, work.delivery_id)
        assert delivery is not None and delivery.status == "in_flight"
        attempt = delivery.attempts[-1]
        assert attempt.number == work.attempt and attempt.outcome == "in_flight"
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if result.outcome == "deferred":
            if task.state == "cancelling":
                attempt.outcome = "blocked"
                attempt.finished_at = self.clock()
                return self._settle_cancelled_delivery(repository, record, delivery, attempt)
            repository.discard_unstarted_attempt(task.id, delivery.id, attempt.number)
            delivery.status = "pending"
            repository.save_delivery(task.id, delivery)
            cooldown = repository.live_cooldown(work.account_id) or LiveSendCooldown(self.clock())
            return self._settle_next(repository, record, task.progress, cooldown)
        if checkpoint.publication_first_pending and result.outcome in {"blocked", "rejected"} and (
            task.state != "cancelling"
        ):
            previous_failures = sum(entry.outcome in {"blocked", "rejected"} for entry in delivery.attempts[:-1])
            now = self.clock()
            attempt.finished_at = now
            attempt.outcome = result.outcome
            attempt.error_code = delivery.error_code = result.error_code or "delivery_rejected"
            attempt.bilibili_error = delivery.bilibili_error = result.bilibili_error
            delivery.status = "pending"
            retry_at = now + timedelta(minutes=1)
            repository.save_delivery(task.id, delivery)
            repository.save_attempt(task.id, delivery.id, attempt)
            if previous_failures >= PUBLICATION_FIRST_MAX_RETRIES:
                self._settle_priority(repository, record, swap=False)
                return self.withdraw(repository, record, return_to_manual=True)
            return advance(
                repository, record, state="paused" if task.state == "pausing" else "backoff",
                next_attempt_at=retry_at,
                checkpoint=checkpoint.model_copy(update={"publication_retry_at": retry_at}),
            )
        if checkpoint.publication_first_pending and result.outcome != "limited":
            checkpoint = checkpoint.model_copy(update={
                "publication_first_pending": False, "publication_retry_at": None,
            })
            task = record.task = task.model_copy(update={"checkpoint": checkpoint})
        if result.outcome == "unknown":
            return self._unknown(repository, record, delivery, result.bilibili_error)
        now = self.clock()
        timing = repository.send_timing(record.created_by)
        attempt.finished_at = now
        attempt.outcome = result.outcome
        attempt.bilibili_error = delivery.bilibili_error = result.bilibili_error
        if result.outcome == "blocked":
            code = result.error_code
            assert code is not None
            attempt.error_code = delivery.error_code = code
            delivery.status = "pending"
            cooldown = self._save_live_wait(repository, work.account_id, timing, attempt)
            if task.state == "cancelling":
                return self._settle_cancelled_delivery(repository, record, delivery, attempt)
            repository.save_delivery(task.id, delivery)
            repository.save_attempt(task.id, delivery.id, attempt)
            self._settle_priority(repository, record, swap=False)
            checkpoint = record.task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            if work.account_role == "secondary" and (
                checkpoint.primary_only
                or code in {"bilibili_credential_missing", "bilibili_credential_rejected", "bilibili_account_changed"}
                or self.needs_primary_fallback(repository, record)
            ):
                record.task = record.task.model_copy(update={
                    "checkpoint": checkpoint.model_copy(update={"primary_only": True}),
                })
                return self._settle_next(repository, record, task.progress, cooldown)
            state = "pausing" if self._in_flight(repository, task.id) else "paused"
            return advance(repository, record, state=state, error=ErrorInfo(
                code=code, message=BLOCKED_MESSAGES.get(code, "真实投递暂不可用"),
            ))
        rate_limit_delay = None
        limited_attempts = 0
        terminal_count = 1
        failed = 0
        if result.outcome == "confirmed":
            assert result.receipt is not None
            attempt.receipt = f"bilibili:{result.receipt}"
            delivery.receipt = attempt.receipt
            delivery.delivered_at = now
            delivery.status = "confirmed"
            repository.save_live_send_key(task.project_id, work.target_cid, delivery.item.stable_key)
        elif result.outcome == "rejected":
            attempt.error_code = result.error_code or "delivery_rejected"
            delivery.status = "failed"
            failed = 1
        else:
            attempt.error_code = "send_rate_limited"
            # Requeueing a failed or unknown attempt starts a new rate-limit budget.
            since_retry = next((entry.number for entry in reversed(delivery.attempts) if entry.resolution == "retry"), 0)
            limited_attempts = sum(
                entry.outcome == "limited"
                and (entry.account_role or delivery.item.account_role or "primary") == work.account_role
                for entry in delivery.attempts[since_retry:]
            )
            rate_limit_delay = max(timing.rate_limit_seconds, result.retry_after_seconds or 0)
            if limited_attempts < SEND_MAX_ATTEMPTS:
                delivery.status = "pending"
                terminal_count = 0
            else:
                delivery.status = "failed"
                failed = 1
                if checkpoint.publication_first_pending:
                    checkpoint = checkpoint.model_copy(update={
                        "publication_first_pending": False, "publication_retry_at": None,
                    })
                    task = record.task = task.model_copy(update={"checkpoint": checkpoint})
        cooldown = self._save_live_wait(repository, work.account_id, timing, attempt,
                                        rate_limit_delay, limited_attempts)
        if task.state == "cancelling":
            return self._settle_cancelled_delivery(repository, record, delivery, attempt)
        delivery.error_code = attempt.error_code
        if terminal_count:
            delivery.clear_resend()
        repository.save_delivery(task.id, delivery)
        repository.save_attempt(task.id, delivery.id, attempt)
        progress = delivery_progress(task.progress, done=terminal_count, failed=failed)
        return self._settle_next(repository, record, progress, cooldown, priority_ready=bool(terminal_count))

    def _settle_next(
        self, repository: Repository, record: TaskRecord, progress: TaskProgress, cooldown: LiveSendCooldown,
        *, priority_ready: bool = False,
    ) -> Task:
        task = record.task
        batch = repository.send_batch(task.id)
        in_flight = self._in_flight(repository, task.id)
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        finished = progress.done + progress.unknown == progress.total and (
            task.state != "pausing" or progress.failed or progress.unknown
        )
        if checkpoint.next_send_task_id and (priority_ready or checkpoint.priority_ready):
            if in_flight:
                record.task = task.model_copy(update={
                    "checkpoint": checkpoint.model_copy(update={"priority_ready": True}),
                })
            else:
                swapped = self._settle_priority(repository, record, swap=True)
                if swapped and not finished and task.state != "pausing":
                    single = batch.secondary_account_id is None
                    return advance(repository, record, state="queued", progress=progress,
                                   next_attempt_at=cooldown.deadline if single else None,
                                   error=self._cooldown_error(cooldown) if single else None)
        if finished:
            return self._finish(repository, record, progress, allow_auto_retry=True)
        wait_error = self._cooldown_error(cooldown)
        if task.state == "pausing":
            return advance(repository, record, state="pausing" if in_flight else "paused", progress=progress,
                           next_attempt_at=cooldown.deadline, error=task.error or wait_error)
        head = repository.send_queue_head(record.created_by)
        if not in_flight and head is not None and head.id != task.id:
            if batch.secondary_account_id is not None:
                states, _ = self._account_schedule(repository, batch, {}, self.clock(),
                                                   primary_only=checkpoint.primary_only)
                checkpoint = checkpoint.model_copy(update={"accounts": states})
            return advance(repository, record, state="queued", progress=progress, error=None,
                           next_attempt_at=None, checkpoint=checkpoint)
        if batch.secondary_account_id is None:
            return advance(repository, record, state="backoff", progress=progress,
                           next_attempt_at=cooldown.deadline, error=wait_error)
        states, _ = self._account_schedule(repository, batch, in_flight, self.clock(),
                                           primary_only=checkpoint.primary_only)
        return self._advance_schedule(repository, record, batch, states, progress)

    def _finish(
        self, repository: Repository, record: TaskRecord, progress: TaskProgress, *, allow_auto_retry: bool = False,
    ) -> Task:
        self._settle_priority(repository, record, swap=False)
        checkpoint = self._finished_accounts_checkpoint(record.task)
        if allow_auto_retry and progress.failed and not checkpoint.automatic_retry_started:
            batch = repository.send_batch(record.task.id)
            progress = self._requeue_failed(repository, record.task, batch, progress)
            checkpoint = checkpoint.model_copy(update={"automatic_retry_started": True})
            if progress.done + progress.unknown < progress.total:
                return advance(repository, record, state="paused" if record.task.state == "pausing" else "queued",
                               error=None, next_attempt_at=None, progress=progress, checkpoint=checkpoint)
        if checkpoint.publication == "pending":
            checkpoint = checkpoint.model_copy(update={"publication": None})
        if progress.unknown:
            return advance(repository, record, state="needs_review", error=UNKNOWN,
                           next_attempt_at=None, progress=progress, checkpoint=checkpoint)
        error = PARTIAL if progress.failed else None
        return advance(repository, record, state="failed" if error else "succeeded", error=error,
                       next_attempt_at=None, progress=progress, checkpoint=checkpoint)

    @staticmethod
    def _finished_accounts_checkpoint(task: Task) -> SendCheckpoint:
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        return checkpoint.model_copy(update={
            "accounts": {role: SendAccountProgress(state="finished") for role in checkpoint.accounts},
        })

    def _unknown(
        self, repository: Repository, record: TaskRecord, delivery: Delivery,
        bilibili_error: BilibiliSendDiagnostic | None = None, *, recovering: bool = False,
    ) -> Task:
        task = record.task
        attempt = delivery.attempts[-1]
        assert attempt.outcome == "in_flight"
        attempt.finished_at = self.clock()
        account_id = repository.send_batch(task.id).accounts()[attempt.account_role or delivery.item.account_role or "primary"]
        timing = repository.send_timing(record.created_by)
        attempt.outcome = "unknown"
        cooldown = self._save_live_wait(repository, account_id, timing, attempt)
        attempt.error_code = UNKNOWN.code
        delivery.status = "unknown"
        delivery.clear_resend()
        delivery.error_code = UNKNOWN.code
        attempt.bilibili_error = delivery.bilibili_error = bilibili_error
        repository.save_delivery(task.id, delivery)
        repository.save_attempt(task.id, delivery.id, attempt)
        self._settle_priority(repository, record, swap=False)
        progress = delivery_progress(task.progress, unknown=1)
        if task.state == "cancelling":
            state = "cancelling" if self._in_flight(repository, task.id) else "cancelled"
            checkpoint = self._finished_accounts_checkpoint(record.task) if state == "cancelled" else record.task.checkpoint
            return advance(repository, record, state=state, error=UNKNOWN, next_attempt_at=None,
                           progress=progress, checkpoint=checkpoint)
        if progress.done + progress.unknown == progress.total:
            return self._finish(repository, record, progress, allow_auto_retry=True)
        if recovering or task.state == "pausing":
            in_flight = self._in_flight(repository, task.id)
            state = "pausing" if in_flight and not recovering else "paused"
            return advance(repository, record, state=state, error=task.error or UNKNOWN,
                           next_attempt_at=cooldown.deadline, progress=progress)
        return self._settle_next(repository, record, progress, cooldown)

    def _record(self, repository: Repository, actor: Principal, task_id: str) -> TaskRecord:
        record = require_task(repository, actor, task_id)
        if record.task.kind != "send":
            raise WorkbenchError(404, "send_batch_not_found", "找不到投递批次")
        return record

    def list(
        self, actor: Principal, task_id: str, status: DeliveryFilter | None, cursor: str | None, limit: int,
        page_number: int | None = None, track: list[str] | None = None,
        *, account_role: BilibiliAccountRole | None = None,
    ) -> DeliveryPage:
        scope = ["deliveries", task_id, status or ""]
        if account_role is not None:
            scope.append(account_role)
        with self.database() as repository:
            record = self._record(repository, actor, task_id)
            if cursor is not None and page_number is not None:
                raise WorkbenchError(422, "invalid_pagination", "不能同时指定页码和游标")
            after = decode_cursor(scope, cursor, valid_id=lambda token: token.isascii() and token.isdecimal()
                                  and (len(token) == 12 or
                                       (status == "active" and len(token) == 13 and token[0] in "012")))
            checkpoint = record.task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            total_count = repository.count_deliveries(task_id, status, role=account_role,
                                                       primary_only=checkpoint.primary_only, include_resending=True)
            last_page = max(1, (total_count + limit - 1) // limit)
            offset = (min(page_number or 1, last_page) - 1) * limit
            rows = repository.deliveries(task_id, status, after, limit + 1, offset=offset,
                                         role=account_role, primary_only=checkpoint.primary_only, include_resending=True)
            rows, has_more = rows[:limit], len(rows) > limit
            next_cursor = None
            if has_more:
                last = rows[-1]
                next_cursor = encode_cursor(scope, f"{last.active_rank}{last.id}" if status == "active" else last.id)
            missing = set(track or ()) - {row.id for row in rows}
            return DeliveryPage(items=rows, next_cursor=next_cursor,
                                batch=repository.send_batch(task_id), task_version=record.task.version,
                                total_count=total_count,
                                tracked_items=repository.deliveries_by_id(task_id, missing) if missing else [])

    def change_delivery(
        self, actor: Principal, task_id: str, delivery_id: str, command: TaskControl, *, resend: bool,
    ) -> Task:
        """Withdraw pending work or requeue a withdrawn, skipped, or failed item in its original batch.

        The delivery and progress change in one transaction. On a version conflict, reload
        the batch before deciding whether to submit again.
        """
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            delivery = repository.delivery(task_id, delivery_id)
            if delivery is None:
                raise WorkbenchError(404, "delivery_not_found", "找不到投递条目")
            batch = repository.send_batch(task_id)
            if resend:
                origin = delivery.status
                if (batch.mode != "live" or origin not in {"withdrawn", "skipped", "failed"}
                        or task.state in {"pausing", "cancelling"}):
                    raise WorkbenchError(409, "delivery_resend_unavailable", "当前条目无法重新发送")
                if task.progress.unknown:
                    raise WorkbenchError(409, "delivery_unknown", "请先核对未知投递")
                if origin == "failed":
                    self._record_failed_retry(repository, task_id, delivery, actor)
                delivery.status = "pending"
                delivery.resending = True
                delivery.resend_from = origin
                delivery.resend_priority = repository.has_later_send_attempt(task_id, delivery.id)
                if delivery.resend_priority:
                    delivery.resend_wait_for = tuple(row.id for row in self._in_flight(repository, task_id).values())
                delivery.item = normalize_send_effects(delivery.item)
                delivery.error_code = None
                delivery.bilibili_error = None
                repository.save_delivery(task_id, delivery)
                progress = delivery_progress(task.progress, done=-1, withdrawn=-int(origin == "withdrawn"),
                                             skipped=-int(origin == "skipped"), failed=-int(origin == "failed"))
                if task.state in TERMINAL_STATES or task.state == "paused":
                    checkpoint = task.checkpoint
                    if task.state in TERMINAL_STATES and checkpoint.publication in {"returned", "dispatch"}:
                        checkpoint = checkpoint.model_copy(update={"publication": None})
                    return advance(repository, record, state="queued", progress=progress, error=None,
                                   next_attempt_at=task.next_attempt_at if batch.secondary_account_id is None else None,
                                   checkpoint=checkpoint)
                return self._after_delivery_edit(repository, record, batch, progress)

            if (batch.mode != "live" or delivery.status != "pending"
                    or task.state in TERMINAL_STATES | {"cancelling"}):
                raise WorkbenchError(409, "delivery_withdraw_unavailable", "仅可撤销待投递的弹幕")
            delivery.status = "withdrawn"
            delivery.clear_resend()
            delivery.error_code = None
            delivery.bilibili_error = None
            repository.save_delivery(task_id, delivery)
            progress = delivery_progress(task.progress, done=1, withdrawn=1)
            if progress.done + progress.unknown == progress.total:
                return self._finish(repository, record, progress, allow_auto_retry=True)
            return self._after_delivery_edit(repository, record, batch, progress)

    def _after_delivery_edit(
        self, repository: Repository, record: TaskRecord, batch: SendBatch, progress: TaskProgress,
    ) -> Task:
        task = record.task
        if batch.secondary_account_id is not None and task.state in {"backoff", "running"}:
            in_flight = self._in_flight(repository, task.id)
            checkpoint = task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            states, _ = self._account_schedule(repository, batch, in_flight, self.clock(),
                                               primary_only=checkpoint.primary_only)
            return self._advance_schedule(repository, record, batch, states, progress,
                                          ready_state="queued" if task.state == "backoff" else "running")
        return advance(repository, record, progress=progress)

    def _record_failed_retry(self, repository: Repository, task_id: str, delivery: Delivery, actor: Principal | None) -> None:
        if not delivery.attempts:
            return
        attempt = delivery.attempts[-1]
        attempt.resolution = "retry"
        attempt.resolved_by = actor.id if actor else None
        attempt.resolved_at = self.clock()
        repository.save_attempt(task_id, delivery.id, attempt)

    def _requeue_failed(
        self, repository: Repository, task: Task, batch: SendBatch, progress: TaskProgress,
        actor: Principal | None = None,
    ) -> TaskProgress:
        rows = repository.deliveries(task.id, "failed", "", progress.failed)
        confirmed = repository.live_send_keys(task.project_id, batch.target.cid)
        skipped = 0
        for row in rows:
            if row.item.stable_key in confirmed:
                row.status = "skipped"
                row.error_code = "duplicate_live_delivery"
                skipped += 1
            else:
                row.status = "pending"
                row.error_code = None
                self._record_failed_retry(repository, task.id, row, actor)
            row.bilibili_error = None
            repository.save_delivery(task.id, row)
        return delivery_progress(progress, done=skipped - len(rows), failed=-len(rows), skipped=skipped)

    def retry_failed(self, actor: Principal, task_id: str, command: TaskControl) -> Task:
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            batch = repository.send_batch(task_id)
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            can_retry = task.state == "failed" or awaiting_failure_decision(task)
            if batch.mode != "live" or not can_retry or not task.progress.failed:
                raise WorkbenchError(409, "send_retry_unavailable", "当前批次没有可重试的真实投递")
            progress = self._requeue_failed(repository, task, batch, task.progress, actor)
            if progress.done + progress.unknown == progress.total:
                return self._finish(repository, record, progress)
            return advance(repository, record, state="queued", progress=progress, error=None, next_attempt_at=None)

    def continue_queue(self, actor: Principal, task_id: str, command: TaskControl) -> Task:
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            if not awaiting_failure_decision(task):
                raise WorkbenchError(409, "send_decision_unavailable", "当前批次未在等待失败处理决定")
            return self._finish(repository, record, task.progress)

    @staticmethod
    def _priority_queue_head(repository: Repository, actor: Principal) -> Task | None:
        head = repository.send_queue_head(actor.id)
        if head is None or head.state not in {"running", "backoff", "pausing", "paused"}:
            return None
        assert isinstance(head.checkpoint, SendCheckpoint)
        return head if head.state == "paused" or head.checkpoint.next_send_task_id is None else None

    @staticmethod
    def _settle_priority(repository: Repository, record: TaskRecord, *, swap: bool) -> bool:
        task = record.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if checkpoint.next_send_task_id is None:
            return False
        swapped = False
        if swap:
            target = repository.task(checkpoint.next_send_task_id, record.created_by)
            if target is not None and target.task.state == "queued":
                repository.swap_send_queue_positions(task.id, target.task.id)
                swapped = True
        # The caller persists this with the attempt outcome and the next task state.
        record.task = task.model_copy(update={
            "checkpoint": checkpoint.model_copy(update={
                "next_send_task_id": None, "waiting_after_priority": swapped, "priority_ready": False,
            }),
        })
        return swapped

    def active_queue_task_id(self, actor: Principal) -> str | None:
        with self.database() as repository:
            head = self._priority_queue_head(repository, actor)
            return head.id if head else None

    def prioritize(self, actor: Principal, task_id: str, command: SendPriority) -> Task:
        """Swap the waiting batch with the queue head at an item boundary."""
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            head = self._priority_queue_head(repository, actor)
            if (head and head.id != task.id and head.checkpoint.publication == "dispatch"
                    and getattr(command, "confirmed_preload_task_id", None) != head.id):
                raise WorkbenchError(409, "preload_priority_confirmation",
                                     "预装弹幕任务正在进行，是否仍要插队？")
            if not self._request_priority(repository, actor, task):
                raise WorkbenchError(409, "send_priority_unavailable", "当前队首状态不允许此批次插队")
            return advance(repository, record)

    def _request_priority(self, repository: Repository, actor: Principal, task: Task) -> bool:
        head = self._priority_queue_head(repository, actor)
        if task.state != "queued" or task.checkpoint.publication == "pending" or head is None or task.id == head.id:
            return False
        active = require_task(repository, actor, head.id)
        if repository.deliveries(head.id, "in_flight", "", 1):
            checkpoint = head.checkpoint.model_copy(update={"next_send_task_id": task.id})
            advance(repository, active, checkpoint=checkpoint)
        else:
            repository.swap_send_queue_positions(head.id, task.id)
            checkpoint = head.checkpoint.model_copy(update={
                "next_send_task_id": None, "waiting_after_priority": True,
            })
            state = "paused" if head.state in {"paused", "pausing"} else "queued"
            advance(repository, active, state=state, checkpoint=checkpoint)
        return True

    def activate_publication(self, repository: Repository, record: TaskRecord) -> bool:
        actor = Principal(id=record.created_by)
        head = repository.send_queue_head(actor.id)
        priority = self._priority_queue_head(repository, actor)
        if head and not priority and head.state != "queued":
            return False
        checkpoint = record.task.checkpoint.model_copy(update={
            "publication": "dispatch", "publication_first_pending": True,
        })
        task = advance(repository, record, checkpoint=checkpoint)
        if head is not None:
            repository.move_send_to_front(task.id, after_head=priority is not None)
            if priority is not None:
                self._request_priority(repository, actor, task)
        repository.delete_publication(task.project_id)
        return True

    def pending_decision(self, actor: Principal) -> Task | None:
        with self.database() as repository:
            head = repository.send_queue_head(actor.id)
            return head if head is not None and awaiting_failure_decision(head) else None

    def resolve(self, actor: Principal, task_id: str, delivery_id: str, command: ResolveDelivery) -> Delivery:
        with self.database(write=True) as repository:
            record = self._record(repository, actor, task_id)
            task = record.task
            delivery = repository.delivery(task_id, delivery_id)
            if delivery is None:
                raise WorkbenchError(404, "delivery_not_found", "找不到投递条目")
            if task.version != command.expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            if delivery.status != "unknown":
                raise WorkbenchError(409, "delivery_not_unknown", "该条弹幕已离开待核对状态")
            if task.state == "cancelled" and command.outcome == "retry":
                raise WorkbenchError(409, "delivery_retry_cancelled", "此批次已取消投递")
            if command.outcome == "retry" and task.state not in {"paused", "needs_review"}:
                raise WorkbenchError(409, "delivery_retry_unavailable", "当前批次无法重发未知回执")
            if task.state == "needs_review" and command.outcome == "retry" and repository.has_project_send_in_queue(task.project_id):
                raise WorkbenchError(409, "send_already_queued", "此项目已有投递批次，请等其结束后重发")
            attempt = delivery.attempts[-1]
            attempt.resolution = command.outcome
            attempt.resolved_at = self.clock()
            attempt.resolved_by = actor.id
            delivery.status = {"confirmed": "confirmed", "skip": "skipped", "retry": "pending"}[command.outcome]
            delivery.error_code = "manual_skip" if command.outcome == "skip" else None
            delivery.bilibili_error = None
            if command.outcome == "confirmed":
                delivery.delivered_at = attempt.resolved_at
                batch = repository.send_batch(task_id)
                repository.save_live_send_key(task.project_id, batch.target.cid, delivery.item.stable_key)
            repository.save_attempt(task_id, delivery_id, attempt)
            repository.save_delivery(task_id, delivery)
            progress = delivery_progress(task.progress, done=int(command.outcome != "retry"),
                                         skipped=int(command.outcome == "skip"), unknown=-1)
            unknown_pause = task.state == "paused" and task.error and task.error.code == UNKNOWN.code
            if task.state == "needs_review":
                if command.outcome == "retry":
                    advance(repository, record, state="queued", progress=progress, error=None, next_attempt_at=None)
                else:
                    self._finish(repository, record, progress)
            elif unknown_pause and command.outcome != "retry" and progress.done + progress.unknown == progress.total:
                self._finish(repository, record, progress, allow_auto_retry=True)
            elif unknown_pause and command.outcome == "confirmed":
                advance(repository, record, state="queued", progress=progress, error=None)
            else:
                error = None if task.error and task.error.code == UNKNOWN.code and not progress.unknown else task.error
                advance(repository, record, progress=progress, error=error)
            if command.outcome == "confirmed":
                queued = repository.tasks(task.project_id, "", 1, "queued", "send")
                if queued:
                    self._request_priority(repository, actor, queued[0])
            return delivery
