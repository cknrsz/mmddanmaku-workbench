from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import uniform
from typing import Literal
from uuid import UUID, uuid4

from danmaku.application.classification.settings import require_config
from danmaku.application.download.normalize import normalize
from danmaku.domain.analysis import ANALYSIS_STOPPED, PROMPT_VERSION, AnalysisInput, AnalysisOutput
from danmaku.domain.inputs import SEGMENT_MS, Snapshot, SourceBatch, VideoPart, Workflow
from danmaku.application.pagination import decode_cursor, encode_cursor, valid_creation_cursor
from danmaku.application.ports import Repository, UnitOfWork
from danmaku.application.projects import require_project
from danmaku.application.sending.service import FAILED_REVIEW, UNKNOWN, Sending, awaiting_failure_decision
from danmaku.application.tasks.state import advance, require_task
from danmaku.domain.sending import BilibiliAccount, BilibiliAccountRole, SendResult, SendWork
from danmaku.domain.workbench import (
    AnalysisCheckpoint, ClassifyCreate, DownloadCheckpoint, ErrorInfo, Page, Principal, SendAccountProgress,
    SendCheckpoint, SendCreate,
    Task, TaskCommand, TaskProgress,
    TaskCreate, TaskKind, TaskRecord, TaskState, TERMINAL_STATES, WorkbenchError,
)

MOCK_BATCHES = 10
ITEMS_PER_BATCH = 5
MAX_ATTEMPTS = 4
CREDENTIAL_BLOCKS = {"llm_credential_missing", "llm_credential_rejected"}
INTERRUPTED = ErrorInfo(code="interrupted", message="服务中断，恢复后从已保存进度继续")
BatchResult = SourceBatch | AnalysisOutput | SendResult | None
BatchWork = TaskRecord | SendWork


def _valid_task_cursor(value: str) -> bool:
    if value.isascii() and value.isdecimal() and len(value) <= 19:
        return valid_creation_cursor(value)
    return UUID(value).hex == value


class BatchError(Exception):
    """Sanitized failure for one batch. A retry repeats the same uncommitted batch."""

    def __init__(self, info: ErrorInfo, *, retry_after: timedelta | None = None):
        super().__init__(info.code)
        self.info = info
        self.retry_after = retry_after


class Tasks:
    def __init__(
        self, database: UnitOfWork, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *, mock_part: Callable[[str, int], VideoPart] | None = None,
        has_saved_credential: Callable[[BilibiliAccount], bool] | None = None,
    ):
        self.database = database
        self.clock = clock
        self.sending = Sending(database, clock, mock_part, has_saved_credential=has_saved_credential)

    def create(self, actor: Principal, project_id: str, key: str, command: TaskCommand) -> Task:
        if isinstance(command, SendCreate) and command.mode != "live":
            raise WorkbenchError(422, "send_mode_removed", "当前仅支持真实投递")
        with self.database(write=True) as repository:
            project = require_project(repository, actor, project_id)
            existing = repository.by_key(actor.id, project_id, key)
            if existing:
                if existing.inputs != command:
                    raise WorkbenchError(409, "idempotency_conflict", "幂等键已用于其他请求")
                return existing.task
            task_id = uuid4().hex
            run = None
            send_batch = None
            publication = None
            skipped = failed = 0
            if isinstance(command, ClassifyCreate):
                snapshot = repository.snapshot(project_id, command.snapshot_id)
                if snapshot is None:
                    raise WorkbenchError(404, "snapshot_not_found", "找不到弹幕快照")
                if snapshot.state != "complete":
                    raise WorkbenchError(409, "snapshot_incomplete", "弹幕快照尚未完成")
                chinese = command.subtitle_version_id
                japanese = command.japanese_subtitle_version_id
                if not command.skip_analysis and chinese is None and japanese is None:
                    raise WorkbenchError(422, "subtitle_required", "请至少选择一份中文或日语字幕")
                if chinese is not None and chinese == japanese:
                    raise WorkbenchError(422, "subtitle_versions_match", "请选择不同的日语原文和中文字幕")
                for version_id in (chinese, japanese):
                    if version_id is None:
                        continue
                    subtitle = repository.subtitle(project_id, version_id)
                    if subtitle is None:
                        raise WorkbenchError(404, "subtitle_not_found", "找不到字幕版本")
                    if not command.skip_analysis and not subtitle.cue_count:
                        raise WorkbenchError(422, "subtitle_empty", "所选字幕需要包含对白")
                if repository.has_active_project_tasks(project_id, "classify"):
                    raise WorkbenchError(409, "analysis_active", "当前项目已有分析任务正在处理")
                config, prompt_version, strategy = None, None, "skipped"
                if not command.skip_analysis:
                    assert command.llm_config_version is not None
                    config = require_config(repository, actor.id, command.llm_config_version)
                    if command.reasoning_effort is not None:
                        if not config.supports_reasoning_effort:
                            raise WorkbenchError(422, "reasoning_effort_unsupported", "当前模型配置无法设置思考强度")
                        config = config.model_copy(update={"reasoning_effort": command.reasoning_effort})
                    prompt_version = PROMPT_VERSION
                    strategy = "bilingual" if chinese and japanese else "single_subtitle"
                run = AnalysisInput(
                    id=task_id, project_id=project_id, snapshot_id=snapshot.id,
                    subtitle_version_id=chinese, japanese_subtitle_version_id=japanese,
                    config=config, prompt_version=prompt_version, strategy=strategy,
                )
                checkpoint = AnalysisCheckpoint(run_id=task_id, analysis_skipped=command.skip_analysis)
                total = snapshot.item_count
            elif isinstance(command, SendCreate):
                send_batch, deliveries = self.sending.prepare(repository, actor, project_id, task_id, command)
                publication = repository.publication(project_id)
                if publication is not None and publication.account_id != send_batch.account_id:
                    raise WorkbenchError(409, "bilibili_account_changed", "请切回创建预装项目时使用的主账号")
                roles = {row.item.account_role for row in deliveries if row.status == "pending"}
                checkpoint = SendCheckpoint(batch_id=task_id, publication="pending" if publication else None, accounts={
                    role: SendAccountProgress(state="ready" if role in roles else "finished")
                    for role in send_batch.accounts()
                } if send_batch.secondary_account_id is not None else {})
                total = len(deliveries)
                skipped = sum(delivery.status == "skipped" for delivery in deliveries)
                failed = sum(delivery.status == "failed" for delivery in deliveries)
            else:
                if not (command.source_cid or command.source_bvid or project.source_bvid):
                    raise WorkbenchError(422, "source_required", "请输入源视频 CID，或选择 BV 号和分 P")
                checkpoint = DownloadCheckpoint()
                total = MOCK_BATCHES * ITEMS_PER_BATCH if command.mode == "mock" else None
            source_bvid = project.source_bvid or project.target_bvid
            source_page = project.source_page or project.target_page
            if isinstance(command, TaskCreate) and command.source_bvid is not None and command.source_page is not None:
                source_bvid, source_page = command.source_bvid, command.source_page
            review_only = run is not None and run.strategy == "skipped"
            task = Task(
                id=task_id, project_id=project.id, creation_order=0, kind=command.kind, version=1,
                state="succeeded" if review_only else "queued",
                progress=TaskProgress(total=total, done=(total or 0) if review_only else skipped + failed,
                                      skipped=skipped, failed=failed),
                checkpoint=checkpoint,
            )
            task = repository.save_task(TaskRecord(
                task=task, inputs=command, created_by=actor.id, idempotency_key=key,
                source_bvid=source_bvid, source_page=source_page,
            ))
            if run is not None:
                repository.add_run(run)
                assert isinstance(command, ClassifyCreate)
                workflow = repository.workflow(project_id) or Workflow()
                repository.save_workflow(project_id, Workflow(
                    snapshot_id=run.snapshot_id, chinese_subtitle_id=run.subtitle_version_id,
                    japanese_subtitle_id=run.japanese_subtitle_version_id, analysis_id=run.id,
                    reasoning_effort=(command.reasoning_effort if run.config and run.config.supports_reasoning_effort
                                      else workflow.reasoning_effort),
                ))
            if send_batch is not None:
                repository.add_send_batch(send_batch, deliveries)
                if publication is not None:
                    repository.save_publication(replace(publication, task_id=task.id))
            return task

    def get(self, actor: Principal, task_id: str) -> Task:
        with self.database() as repository:
            return require_task(repository, actor, task_id).task

    def stop_analysis(self, actor: Principal, task_id: str) -> Task:
        with self.database(write=True) as repository:
            record = require_task(repository, actor, task_id)
            task = record.task
            if not isinstance(task.checkpoint, AnalysisCheckpoint):
                raise WorkbenchError(404, "analysis_not_found", "找不到分析任务")
            if task.state in TERMINAL_STATES or task.checkpoint.completed_batches:
                return task
            return advance(repository, record, state="failed", error=ANALYSIS_STOPPED, next_attempt_at=None)

    def delete(self, actor: Principal, task_id: str) -> None:
        with self.database(write=True) as repository:
            task = require_task(repository, actor, task_id).task
            if task.state in {"running", "pausing", "cancelling"}:
                raise WorkbenchError(409, "task_active", "请先暂停任务，或等待任务结束后再删除")
            if repository.task_in_use(task):
                message = ("请先删除引用此快照的分析任务" if task.kind == "download"
                           else "请先删除引用此分析结果的投递任务")
                raise WorkbenchError(409, "task_in_use", message)
            repository.delete_task(task)

    def list(
        self, actor: Principal, project_id: str, cursor: str | None, limit: int,
        state: TaskState | None, kind: str | None,
    ) -> Page[Task]:
        scope = ["tasks-by-creation", actor.id, project_id, state or "", kind or ""]
        with self.database() as repository:
            require_project(repository, actor, project_id)
            after = decode_cursor(scope, cursor, valid_id=_valid_task_cursor)
            tasks = repository.tasks(project_id, after, limit + 1, state, kind)
        next_cursor = encode_cursor(scope, str(tasks[limit - 1].creation_order)) if len(tasks) > limit else None
        return Page(items=tasks[:limit], next_cursor=next_cursor)

    def control(
        self, actor: Principal, task_id: str, expected_version: int, action: Literal["pause", "resume"],
    ) -> Task:
        with self.database(write=True) as repository:
            record = require_task(repository, actor, task_id)
            state = record.task.state
            if action == "pause" and state in {"pausing", "paused"}:
                return record.task
            if action == "resume" and (state == "queued" or (state == "backoff" and record.task.kind == "send")):
                return record.task
            if record.task.version != expected_version:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
            if action == "pause":
                if state == "running":
                    return advance(repository, record, state="pausing")
                if state in {"queued", "backoff"}:
                    return advance(repository, record, state="paused")
            if action == "resume" and state == "paused":
                if record.task.kind == "classify" and repository.has_active_project_tasks(record.task.project_id, "classify"):
                    raise WorkbenchError(409, "analysis_active", "当前项目已有分析任务正在处理")
                if awaiting_failure_decision(record.task):
                    raise WorkbenchError(409, FAILED_REVIEW.code, FAILED_REVIEW.message)
                if record.task.kind == "send":
                    return self.sending.resume(repository, record)
                error = record.task.error if record.terminal_batch_error else None
                deadline = None if record.task.kind == "classify" else record.task.next_attempt_at
                return advance(repository, record, state="queued", error=error, next_attempt_at=deadline)
            raise WorkbenchError(409, "invalid_transition", "当前任务状态不支持此操作")

    @staticmethod
    def _interrupted_sends(repository: Repository, actor: Principal) -> list[tuple[TaskRecord, Task]]:
        return [
            (record, before_pause)
            for record, before_pause in repository.paused_sends_with_history(actor.id, INTERRUPTED.code)
            if before_pause.state in {"queued", "running", "backoff"}
        ]

    def interrupted_send_count(self, actor: Principal) -> int:
        with self.database() as repository:
            return len(self._interrupted_sends(repository, actor))

    @staticmethod
    def _manually_paused_send_head(repository: Repository, actor: Principal) -> Task | None:
        head = repository.send_queue_head(actor.id)
        if head and head.state == "paused" and (
            head.error is None or head.error.retryable or head.error.code == UNKNOWN.code
        ):
            return head
        return None

    def paused_send_head(self, actor: Principal) -> tuple[Task, str] | None:
        with self.database() as repository:
            head = self._manually_paused_send_head(repository, actor)
            if head is not None:
                return head, require_project(repository, actor, head.project_id).name
        return None

    def resume_interrupted_sends(
        self, actor: Principal, confirmed_head: tuple[str, int] | None = None,
    ) -> int:
        with self.database(write=True) as repository:
            interrupted = self._interrupted_sends(repository, actor)
            resumed_head = 0
            if confirmed_head is not None:
                head = self._manually_paused_send_head(repository, actor)
                if head is not None and (head.id, head.version) == confirmed_head:
                    record = require_task(repository, actor, head.id)
                    self.sending.resume(repository, record)
                    resumed_head = 1
                else:
                    raise WorkbenchError(409, "version_conflict", "队首任务已更新，请重新点击恢复所有任务")
            for record, before_pause in interrupted:
                state = "backoff" if before_pause.state == "backoff" else "queued"
                advance(repository, record, state=state, error=before_pause.error)
            return len(interrupted) + resumed_head

    def recover(self) -> None:
        with self.database(write=True) as repository:
            for record in repository.retired_send_tasks():
                advance(repository, record, state="failed", next_attempt_at=None, error=ErrorInfo(
                    code="send_mode_removed", message="模拟投递已停用，任务记录已保留",
                ))
            send_owners = set()
            for record in repository.interrupted():
                if record.task.kind == "send":
                    if record.task.checkpoint.publication == "pending":
                        continue
                    send_owners.add(record.created_by)
                    if self.sending.recover(repository, record):
                        continue
                    if record.task.state == "pausing":
                        advance(repository, record, state="paused")
                        continue
                if record.task.kind == "classify" and record.task.state in {"queued", "backoff"}:
                    error = record.task.error
                    assert error is not None
                    advance(repository, record, state="failed", next_attempt_at=None,
                            error=error.model_copy(update={"retryable": False}))
                    continue
                error = INTERRUPTED
                if record.task.kind == "classify":
                    error = error.model_copy(update={"message": "服务中断，恢复后会重新请求完整分析"})
                advance(repository, record, state="paused", error=record.task.error if record.terminal_batch_error else error)
            for owner_id in send_owners:
                self.sending.apply_queue_upgrades(repository, owner_id)

    def _due(self, repository: Repository, sending: bool, kind: TaskKind | None) -> TaskRecord | None:
        now = self.clock()
        record = repository.due(now, sending=sending, ignore_deadline=sending, kind=kind)
        return record if record is None or not sending or self._send_due(repository, record) else None

    def claim(self, sending: bool = False, kind: TaskKind | None = None) -> TaskRecord | None:
        # Idle polling shares a read lock. Recheck after acquiring the write lock.
        with self.database() as repository:
            if self._due(repository, sending, kind) is None:
                return None
        with self.database(write=True) as repository:
            record = self._due(repository, sending, kind)
            if record is None:
                return None
            if record.task.state == "backoff" and not sending:
                advance(repository, record, state="queued", next_attempt_at=None)
                return None
            if sending:
                self._claim_send(repository, record)
            else:
                error = record.task.error if record.terminal_batch_error else None
                advance(repository, record, state="running", next_attempt_at=None, error=error)
            return record

    def next_send_work(self, active: tuple[SendWork, ...]) -> SendWork | None:
        with self.database() as repository:
            queue = repository.send_dispatch_records()
            if not queue or queue[0].task.state in {"paused", "cancelling"}:
                return None
            head = queue[0]
            checkpoint = head.task.checkpoint
            assert isinstance(checkpoint, SendCheckpoint)
            active_here = any(work.task_id == head.task.id for work in active)
            if active_here and "secondary" not in checkpoint.accounts:
                return None
            cooling_down = head.task.state in {"queued", "backoff"} and not self._send_due(repository, head)
            if cooling_down and (
                checkpoint.primary_only or not any(state.state == "finished" for state in checkpoint.accounts.values())
            ):
                return None
            awaiting_receipt = (
                active_here and head.task.state == "running"
                and all(state.state in {"sending", "finished"} for state in checkpoint.accounts.values())
                and not self.sending.needs_primary_fallback(repository, head)
            )
            # With no following batch, only a possible handoff can make progress while waiting.
            if len(queue) == 1 and (cooling_down or awaiting_receipt):
                batch = repository.send_batch(head.task.id)
                idle = self.sending.idle_dual_role(repository, head, batch)
                if idle is None or not self.sending.pending_share(repository, head, batch, idle):
                    return None
        with self.database(write=True) as repository:
            queue = repository.send_dispatch_records()
            if not queue:
                return None
            head = queue[0]
            if head.task.state == "pausing" and not repository.count_deliveries(head.task.id, "in_flight"):
                advance(repository, head, state="paused")
                return None
            if head.task.state not in {"queued", "running", "backoff"}:
                return None
            # A settlement may have committed while its worker is still returning the receipt.
            if head.task.state != "running" and any(work.task_id == head.task.id for work in active):
                return None
            batch = repository.send_batch(head.task.id)
            idle = (self.sending.idle_dual_role(repository, head, batch)
                    if head.task.state in {"running", "backoff"} else None)
            if head.task.state == "queued" and self._send_due(repository, head):
                self._claim_send(repository, head)
                idle = self.sending.idle_dual_role(repository, head, batch)
            if idle is not None and self.sending.share_pending(repository, head, batch, idle):
                idle = None
            if head.task.state == "backoff" and self._send_due(repository, head):
                self._claim_send(repository, head)
            if head.task.state == "running":
                if head.terminal_batch_error:
                    advance(repository, head, state="failed")
                    return None
                work = self.sending.start(repository, head, active=active)
                if work is not None:
                    return work
            if len(queue) == 2 and idle is not None and head.task.state in {"running", "backoff"}:
                return self._start_send_ahead(repository, head, queue[1], idle, batch.accounts()[idle], active)
            return None

    def _send_due(self, repository: Repository, record: TaskRecord) -> bool:
        deadline = record.task.next_attempt_at
        return deadline is None or deadline <= self.clock() or self.sending.needs_primary_fallback(repository, record)

    @staticmethod
    def _claim_send(repository: Repository, record: TaskRecord) -> None:
        checkpoint = record.task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        advance(repository, record, state="running", next_attempt_at=None, error=record.task.error
                if record.terminal_batch_error else None, checkpoint=checkpoint.model_copy(
                    update={"started": True, "waiting_after_priority": False},
                ))

    def _start_send_ahead(
        self, repository: Repository, head: TaskRecord, following: TaskRecord, role: BilibiliAccountRole,
        account_id: str, active: tuple[SendWork, ...],
    ) -> SendWork | None:
        task = following.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, SendCheckpoint)
        if (following.created_by != head.created_by or task.state != "queued" or checkpoint.secondary_upgrade_id
                or checkpoint.publication == "dispatch" or head.task.checkpoint.publication == "dispatch"
                or any(work.account_id == account_id for work in active)):
            return None
        batch = repository.send_batch(task.id)
        if batch.mode != "live" or batch.accounts().get(role) != account_id:
            return None
        if role == "secondary" and (checkpoint.primary_only or self.sending.needs_primary_fallback(repository, following)):
            return None
        cooldown = repository.live_cooldown(account_id)
        if cooldown is not None and cooldown.deadline > self.clock():
            return None
        return self.sending.start(repository, following, only_role=role, active=active)

    def next_batch(self, task_id: str) -> BatchWork | None:
        with self.database(write=True) as repository:
            record = repository.task(task_id)
            if record is None:
                return None
            if record.task.state == "pausing":
                if record.task.kind != "send" or not repository.count_deliveries(task_id, "in_flight"):
                    advance(repository, record, state="paused")
                return None
            if record.task.state != "running":
                return None
            if record.terminal_batch_error:
                advance(repository, record, state="failed")
                return None
            if record.task.kind == "send":
                return self.sending.start(repository, record)
            if isinstance(record.task.checkpoint, AnalysisCheckpoint):
                checkpoint = record.task.checkpoint
                run = repository.analysis_input(checkpoint.run_id)
                if run.strategy in {"bilingual", "single_subtitle"} and checkpoint.completed_batches:
                    advance(repository, record, state="succeeded", error=None)
                    return None
                return record
            snapshot = repository.snapshot(record.task.project_id, task_id)
            if (snapshot is not None and snapshot.state == "complete") or (
                snapshot is None and record.inputs.mode == "mock"
                and record.task.checkpoint.completed_batches == MOCK_BATCHES
            ):
                advance(repository, record, state="succeeded")
                return None
            return record

    def settle(
        self, claimed: BatchWork, failure: BatchError | None, result: BatchResult = None,
    ) -> Task | None:
        with self.database(write=True) as repository:
            task_id = claimed.task_id if isinstance(claimed, SendWork) else claimed.task.id
            record = repository.task(task_id)
            if not isinstance(claimed, SendWork) and claimed.task.kind == "classify":
                if record is None or record.task.error == ANALYSIS_STOPPED:
                    return None
            assert record is not None
            if isinstance(claimed, SendWork):
                assert record.task.state in {"running", "pausing", "cancelling"}
                outcome = result if failure is None and isinstance(result, SendResult) else SendResult(outcome="unknown")
                self.sending.settle(repository, record, claimed, outcome)
                self.sending.apply_queue_upgrades(repository, record.created_by, record)
                return record.task
            assert record.task.state in {"running", "pausing"}
            assert record.task.checkpoint == claimed.task.checkpoint
            if failure is not None:
                return self._settle_failure(repository, record, failure)
            if isinstance(result, AnalysisOutput):
                return self._save_analysis(repository, record, result)
            if isinstance(result, SourceBatch):
                return self._save_download(repository, record, result)
            completed = record.task.checkpoint.completed_batches + 1
            record.attempts = 0
            state: TaskState = record.task.state
            if state == "pausing":
                state = "paused"
            elif completed == MOCK_BATCHES:
                state = "succeeded"
            return advance(
                repository, record, state=state, error=None,
                checkpoint=DownloadCheckpoint(completed_batches=completed),
                progress=record.task.progress.model_copy(update={"done": completed * ITEMS_PER_BATCH}),
            )

    def _save_download(self, repository: Repository, record: TaskRecord, result: SourceBatch) -> Task:
        task = record.task
        snapshot = repository.snapshot(task.project_id, task.id)
        if snapshot is None:
            snapshot = Snapshot(
                id=task.id, mode=record.inputs.mode, part=result.part, segment_count=result.segment_count,
                range_kind="synthetic" if record.inputs.mode == "mock" else "current_segments",
            )
            repository.save_snapshot(snapshot)
        assert result.part == snapshot.part and result.segment_count == snapshot.segment_count
        if result.segment is not None:
            assert result.segment == snapshot.completed_segments < snapshot.segment_count
            snapshot.item_count += repository.add_source_items(task.id, normalize(result))
            snapshot.completed_segments += 1
            coverage = snapshot.completed_segments * SEGMENT_MS
            duration = snapshot.part.duration_ms
            snapshot.covered_until_ms = coverage if duration is None else min(duration, coverage)
            if snapshot.completed_segments == snapshot.segment_count:
                snapshot.state = "complete"
                snapshot.sequence = repository.next_snapshot_sequence(task.project_id)
            repository.save_snapshot(snapshot)
            if snapshot.state == "complete" and snapshot.mode == "bilibili":
                repository.add_repost_notice(snapshot.id)
        record.attempts = 0
        state: TaskState = "running"
        if task.state == "pausing":
            state = "paused"
        elif snapshot.state == "complete":
            state = "succeeded"
        updated = advance(
            repository, record, state=state, error=None,
            checkpoint=DownloadCheckpoint(completed_batches=snapshot.completed_segments),
            progress=TaskProgress(done=snapshot.item_count, total=(
                snapshot.item_count if snapshot.state == "complete" else task.progress.total
            )),
        )
        return updated

    def _save_analysis(
        self, repository: Repository, record: TaskRecord, result: AnalysisOutput,
    ) -> Task:
        task = record.task
        checkpoint = task.checkpoint
        assert isinstance(checkpoint, AnalysisCheckpoint)
        run = repository.analysis_input(checkpoint.run_id)
        record.attempts = 0
        state: TaskState = "paused" if task.state == "pausing" else "succeeded"
        repository.save_classifications(run, result.classifications)
        return advance(
            repository, record, state=state, error=None,
            checkpoint=AnalysisCheckpoint(run_id=run.id, completed_batches=1),
            progress=task.progress.model_copy(update={"done": len(result.classifications)}),
        )

    def _settle_failure(self, repository: Repository, record: TaskRecord, failure: BatchError) -> Task:
        if failure.info.code in CREDENTIAL_BLOCKS:
            return advance(repository, record, state="paused", error=failure.info)
        record.attempts += 1
        if not failure.info.retryable or record.attempts >= MAX_ATTEMPTS:
            error = failure.info.model_copy(update={"retryable": False})
            if record.task.state == "pausing":
                record.terminal_batch_error = True
                return advance(repository, record, state="paused", error=error)
            return advance(repository, record, state="failed", error=error)
        delay = failure.retry_after
        if delay is None:
            delay = timedelta(seconds=min(30, 2**record.attempts + uniform(0, 1)))
        state = "paused" if record.task.state == "pausing" else "backoff"
        return advance(repository, record, state=state, error=failure.info,
                       next_attempt_at=self.clock() + delay)

    def replay(self, actor: Principal, task_id: str, after: int) -> tuple[Task, list[Task]]:
        with self.database() as repository:
            latest = require_task(repository, actor, task_id).task
            return latest, repository.events(task_id, after, 100)

