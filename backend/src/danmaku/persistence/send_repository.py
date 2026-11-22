from datetime import datetime

from sqlalchemy import (
    JSON, ForeignKey, ForeignKeyConstraint, Index, case, delete, func, literal_column, null, or_, select, tuple_, update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from danmaku.domain.sending import (
    Attempt, BilibiliAccount, BilibiliAccountRole, Delivery, DeliveryFilter, LiveSendCooldown, SendBatch, SendTiming,
)
from danmaku.domain.settings import SendingSettings
from danmaku.domain.workbench import Task
from danmaku.persistence.analysis_repository import AnalysisBatchRow, AnalysisRunRow, ClassificationRow
from danmaku.persistence.input_rows import CueRow, ProjectPartsRow, SnapshotRow, SourceItemRow, SubtitleRow
from danmaku.persistence.repository import Base, ProjectRow, SendQueueRow, TaskEventRow, TaskRow
from danmaku.persistence.review_repository import ReviewDecisionRow, ReviewRepository


class SendBatchRow(Base):
    __tablename__ = "send_batches"

    id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"))
    data: Mapped[dict] = mapped_column(JSON)


class DeliveryRow(Base):
    __tablename__ = "send_deliveries"
    __table_args__ = (Index("ix_send_delivery_status", "batch_id", "status", "id"),)

    batch_id: Mapped[str] = mapped_column(ForeignKey("send_batches.id"), primary_key=True)
    id: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)


def delivery_status_matches(status: DeliveryFilter, *, include_resending: bool):
    if include_resending and status in {"withdrawn", "skipped", "failed"}:
        resending = DeliveryRow.data["resending"].as_boolean().is_(True)
        origin = func.coalesce(DeliveryRow.data["resend_from"].as_string(), "withdrawn")
        return (DeliveryRow.status == status) | (resending & (origin == status))
    if status == "active":
        return DeliveryRow.status.in_(("pending", "in_flight"))
    return DeliveryRow.status == status


is_priority_resend = func.json_extract(DeliveryRow.data, literal_column("'$.resend_priority'")) == literal_column("1")
priority_resend_index = Index(
    "ix_send_delivery_resend_priority", DeliveryRow.batch_id, DeliveryRow.status, DeliveryRow.id,
    sqlite_where=is_priority_resend,
)


def delivery_data(delivery: Delivery) -> dict:
    return delivery.model_dump(mode="json", exclude={"attempts"}) | {
        "resend_from": delivery.resend_from, "resend_priority": delivery.resend_priority,
        "resend_wait_for": list(delivery.resend_wait_for),
    }


class AttemptRow(Base):
    __tablename__ = "send_attempts"
    __table_args__ = (ForeignKeyConstraint(["batch_id", "delivery_id"],
                                           ["send_deliveries.batch_id", "send_deliveries.id"]),)

    batch_id: Mapped[str] = mapped_column(primary_key=True)
    delivery_id: Mapped[str] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


def delivery_role_matches(role: BilibiliAccountRole, *, primary_only: bool):
    assigned = func.coalesce(DeliveryRow.data["item"]["account_role"].as_string(), "primary")
    actual = select(AttemptRow.data["account_role"].as_string()).where(
        AttemptRow.batch_id == DeliveryRow.batch_id, AttemptRow.delivery_id == DeliveryRow.id,
    ).order_by(AttemptRow.number.desc()).limit(1).scalar_subquery()
    return case(
        (DeliveryRow.status == "pending", "primary" if primary_only else assigned),
        else_=func.coalesce(actual, assigned),
    ) == role


class LiveCooldownRow(Base):
    __tablename__ = "live_send_cooldowns"

    account_id: Mapped[str] = mapped_column(primary_key=True)
    deadline: Mapped[str]
    consecutive_failures: Mapped[int] = mapped_column(server_default="0")
    reason: Mapped[str | None]


class BilibiliAccountRow(Base):
    __tablename__ = "bilibili_accounts"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class BilibiliSecondaryAccountRow(Base):
    __tablename__ = "bilibili_secondary_accounts"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class SendTimingRow(Base):
    __tablename__ = "send_timing"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class LiveDeliveryRow(Base):
    __tablename__ = "live_send_keys"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    target_cid: Mapped[str] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(primary_key=True)


class SendRepository(ReviewRepository):
    def projects_waiting_for_first_send(self, project_ids: list[str]) -> set[str]:
        sent = select(AttemptRow.batch_id).where(AttemptRow.batch_id == TaskRow.id).exists()
        query = select(TaskRow.project_id).join(SendQueueRow).where(
            TaskRow.project_id.in_(project_ids), TaskRow.checkpoint["publication"].as_string() == "dispatch", ~sent,
        )
        return set(self.session.scalars(query))

    def send_timing(self, owner_id: str) -> SendTiming:
        return SendTiming.model_validate(self.sending_settings(owner_id).model_dump(exclude={"dual_account_enabled"}))

    def save_send_timing(self, owner_id: str, timing: SendTiming) -> None:
        current = self.sending_settings(owner_id)
        self.save_sending_settings(owner_id, current.model_copy(update=timing.model_dump()))

    def sending_settings(self, owner_id: str) -> SendingSettings:
        row = self.session.get(SendTimingRow, owner_id)
        return SendingSettings.model_validate(row.data) if row else SendingSettings()

    def save_sending_settings(self, owner_id: str, settings: SendingSettings) -> None:
        self.session.merge(SendTimingRow(owner_id=owner_id, data=settings.model_dump()))

    def task_in_use(self, task: Task) -> bool:
        if task.kind == "download":
            dependency = select(AnalysisRunRow.id).where(AnalysisRunRow.snapshot_id == task.id)
        elif task.kind == "classify":
            dependency = select(SendBatchRow.id).where(SendBatchRow.run_id == task.id)
        else:
            return False
        return self.session.scalar(select(dependency.exists()))

    def delete_task(self, task: Task) -> None:
        task_id = task.id
        if task.kind == "send":
            self.session.execute(delete(AttemptRow).where(AttemptRow.batch_id == task_id))
            self.session.execute(delete(DeliveryRow).where(DeliveryRow.batch_id == task_id))
            self.session.execute(delete(SendBatchRow).where(SendBatchRow.id == task_id))
        elif task.kind == "classify":
            self.session.execute(delete(ReviewDecisionRow).where(ReviewDecisionRow.run_id == task_id))
            self.session.execute(delete(ClassificationRow).where(ClassificationRow.run_id == task_id))
            self.session.execute(delete(AnalysisBatchRow).where(AnalysisBatchRow.run_id == task_id))
            self.session.execute(delete(AnalysisRunRow).where(AnalysisRunRow.id == task_id))
        else:
            self.session.execute(delete(SourceItemRow).where(SourceItemRow.snapshot_id == task_id))
            self.session.execute(delete(SnapshotRow).where(SnapshotRow.id == task_id))
        self.session.execute(delete(TaskEventRow).where(TaskEventRow.task_id == task_id))
        self.session.execute(delete(TaskRow).where(TaskRow.id == task_id))

    def delete_project(self, project_id: str) -> None:
        tasks = select(TaskRow.id).where(TaskRow.project_id == project_id)
        runs = select(AnalysisRunRow.id).where(AnalysisRunRow.project_id == project_id)
        subtitles = select(SubtitleRow.id).where(SubtitleRow.project_id == project_id)
        # Delete children before their parents so existing databases need no schema migration.
        for table, scope in (
            (AttemptRow, AttemptRow.batch_id.in_(tasks)),
            (DeliveryRow, DeliveryRow.batch_id.in_(tasks)),
            (SendBatchRow, SendBatchRow.id.in_(tasks)),
            (LiveDeliveryRow, LiveDeliveryRow.project_id == project_id),
            (ReviewDecisionRow, ReviewDecisionRow.run_id.in_(runs)),
            (ClassificationRow, ClassificationRow.run_id.in_(runs)),
            (AnalysisBatchRow, AnalysisBatchRow.run_id.in_(runs)),
            (AnalysisRunRow, AnalysisRunRow.project_id == project_id),
            (SourceItemRow, SourceItemRow.snapshot_id.in_(tasks)),
            (SnapshotRow, SnapshotRow.id.in_(tasks)),
            (CueRow, CueRow.version_id.in_(subtitles)),
            (SubtitleRow, SubtitleRow.project_id == project_id),
            (TaskEventRow, TaskEventRow.task_id.in_(tasks)),
            (TaskRow, TaskRow.project_id == project_id),
            (ProjectPartsRow, ProjectPartsRow.project_id == project_id),
            (ProjectRow, ProjectRow.id == project_id),
        ):
            self.session.execute(delete(table).where(scope))

    def bilibili_account(self, owner_id: str, role: BilibiliAccountRole = "primary") -> BilibiliAccount | None:
        row_type = BilibiliAccountRow if role == "primary" else BilibiliSecondaryAccountRow
        row = self.session.get(row_type, owner_id)
        return BilibiliAccount.model_validate(row.data) if row else None

    def save_bilibili_account(self, owner_id: str, account: BilibiliAccount,
                              role: BilibiliAccountRole = "primary") -> None:
        row_type = BilibiliAccountRow if role == "primary" else BilibiliSecondaryAccountRow
        self.session.merge(row_type(owner_id=owner_id, data=account.model_dump(mode="json")))

    def delete_bilibili_account(self, owner_id: str, role: BilibiliAccountRole = "primary") -> None:
        row_type = BilibiliAccountRow if role == "primary" else BilibiliSecondaryAccountRow
        row = self.session.get(row_type, owner_id)
        if row is not None:
            self.session.delete(row)

    def live_send_keys(self, project_id: str, target_cid: str) -> set[str]:
        return set(self.session.scalars(select(LiveDeliveryRow.source_key).where(
            LiveDeliveryRow.project_id == project_id, LiveDeliveryRow.target_cid == target_cid,
        )))

    def has_live_send_key(self, project_id: str, target_cid: str, key: str) -> bool:
        return self.session.get(LiveDeliveryRow, (project_id, target_cid, key)) is not None

    def save_live_send_key(self, project_id: str, target_cid: str, key: str) -> None:
        self.session.execute(insert(LiveDeliveryRow).values(
            project_id=project_id, target_cid=target_cid, source_key=key,
        ).on_conflict_do_nothing())

    def withdraw_pending_deliveries(self, batch_id: str) -> int:
        result = self.session.execute(update(DeliveryRow).where(
            DeliveryRow.batch_id == batch_id, DeliveryRow.status == "pending",
        ).values(status="withdrawn", data=func.json_set(
            DeliveryRow.data, "$.status", "withdrawn", "$.resending", func.json("false"),
            "$.resend_from", null(), "$.resend_priority", func.json("false"),
            "$.resend_wait_for", func.json("[]"),
            "$.error_code", null(), "$.bilibili_error", null(),
        )))
        return result.rowcount

    def live_cooldown(self, account_id: str) -> LiveSendCooldown | None:
        row = self.session.get(LiveCooldownRow, account_id)
        return LiveSendCooldown(datetime.fromisoformat(row.deadline), row.consecutive_failures, row.reason) if row else None

    def save_live_cooldown(self, account_id: str, cooldown: LiveSendCooldown) -> None:
        self.session.merge(LiveCooldownRow(account_id=account_id, deadline=cooldown.deadline.isoformat(),
                                           consecutive_failures=cooldown.consecutive_failures, reason=cooldown.reason))

    def add_send_batch(self, batch: SendBatch, deliveries: list[Delivery]) -> None:
        self.session.add(SendBatchRow(id=batch.id, run_id=batch.run_id, data=batch.model_dump(mode="json")))
        self.session.flush()
        self.session.add_all(DeliveryRow(batch_id=batch.id, id=row.id, status=row.status,
                                         data=delivery_data(row))
                             for row in deliveries)

    def save_send_batch(self, batch: SendBatch) -> None:
        row = self.session.get(SendBatchRow, batch.id)
        previous_secondary = row.data.get("secondary_account_id")
        if previous_secondary is not None and previous_secondary != batch.secondary_account_id:
            # Keep legacy attempts attributed to the account that actually sent them.
            self.session.flush()
            item_role = select(DeliveryRow.data["item"]["account_role"].as_string()).where(
                DeliveryRow.batch_id == AttemptRow.batch_id, DeliveryRow.id == AttemptRow.delivery_id,
            ).scalar_subquery()
            self.session.execute(update(AttemptRow).where(
                AttemptRow.batch_id == batch.id,
                AttemptRow.data["account_id"].as_string().is_(None),
                func.coalesce(AttemptRow.data["account_role"].as_string(), item_role) == "secondary",
            ).values(data=func.json_set(AttemptRow.data, "$.account_id", previous_secondary)))
        row.data = batch.model_dump(mode="json")

    def send_batch(self, batch_id: str) -> SendBatch:
        row = self.session.get(SendBatchRow, batch_id)
        return SendBatch.model_validate(row.data)

    def save_delivery(self, batch_id: str, delivery: Delivery) -> None:
        row = self.session.get(DeliveryRow, (batch_id, delivery.id))
        row.status = delivery.status
        row.data = delivery_data(delivery)

    def save_attempt(self, batch_id: str, delivery_id: str, attempt: Attempt) -> None:
        data = attempt.model_dump(mode="json")
        data["account_id"] = attempt.account_id
        data["pacing_wait_seconds"] = attempt.pacing_wait_seconds
        self.session.merge(AttemptRow(batch_id=batch_id, delivery_id=delivery_id, number=attempt.number,
                                      data=data))

    def discard_unstarted_attempt(self, batch_id: str, delivery_id: str, number: int) -> None:
        self.session.delete(self.session.get(AttemptRow, (batch_id, delivery_id, number)))

    def recent_send_attempts(self, owner_id: str, account_id: str, limit: int) -> list[Attempt]:
        started = func.rtrim(AttemptRow.data["started_at"].as_string(), "Z")
        order = (started, TaskRow.order, AttemptRow.delivery_id, AttemptRow.number)
        role = func.coalesce(AttemptRow.data["account_role"].as_string(),
                             DeliveryRow.data["item"]["account_role"].as_string())
        legacy_account_id = case(
            (role == "secondary", SendBatchRow.data["secondary_account_id"].as_string()),
            else_=SendBatchRow.data["account_id"].as_string(),
        )
        attempt_account_id = func.coalesce(AttemptRow.data["account_id"].as_string(), legacy_account_id)
        attempts = (
            select(AttemptRow.data).join(SendBatchRow, SendBatchRow.id == AttemptRow.batch_id)
            .join(DeliveryRow, (DeliveryRow.batch_id == AttemptRow.batch_id) & (DeliveryRow.id == AttemptRow.delivery_id))
            .join(TaskRow, TaskRow.id == SendBatchRow.id)
            .where(TaskRow.created_by == owner_id, SendBatchRow.data["mode"].as_string() == "live",
                   attempt_account_id == account_id)
        )
        confirmed = self.session.execute(
            attempts.with_only_columns(*order).where(or_(
                AttemptRow.data["outcome"].as_string() == "confirmed",
                AttemptRow.data["resolution"].as_string() == "confirmed",
            )).order_by(*(column.desc() for column in order)).limit(limit)
        ).all()
        if not confirmed:
            return []
        rows = self.session.scalars(attempts.where(
            tuple_(*order) >= tuple_(*confirmed[-1]),
            tuple_(*order) <= tuple_(*confirmed[0]),
        ).order_by(*order))
        return [Attempt.model_validate(row) for row in rows]

    def historical_send_deadlines(
        self, owner_id: str, account_id: str, after: datetime, through: datetime,
    ) -> list[datetime]:
        deadline = func.rtrim(TaskEventRow.snapshot["next_attempt_at"].as_string(), "Z")
        error = TaskEventRow.snapshot["error"]["code"].as_string()
        rows = self.session.scalars(
            select(deadline).join(SendBatchRow, SendBatchRow.id == TaskEventRow.task_id)
            .join(TaskRow, TaskRow.id == SendBatchRow.id)
            .where(TaskRow.created_by == owner_id, SendBatchRow.data["mode"].as_string() == "live",
                   SendBatchRow.data["account_id"].as_string() == account_id,
                   deadline > after.isoformat().removesuffix("+00:00"),
                   deadline <= through.isoformat().removesuffix("+00:00"))
            .group_by(deadline)
            .having(func.max(case((error == "send_repeated_failures", 1), else_=0)) == 0)
            .order_by(deadline)
        )
        return [datetime.fromisoformat(value + "Z") for value in rows]

    def _attach_attempts(self, batch_id: str, deliveries: list[Delivery]) -> list[Delivery]:
        if not deliveries:
            return deliveries
        by_id = {row.id: row for row in deliveries}
        attempts = self.session.scalars(select(AttemptRow).where(
            AttemptRow.batch_id == batch_id, AttemptRow.delivery_id.in_(by_id),
        ).order_by(AttemptRow.delivery_id, AttemptRow.number))
        for row in attempts:
            by_id[row.delivery_id].attempts.append(Attempt.model_validate(row.data))
        return deliveries

    def delivery(self, batch_id: str, delivery_id: str) -> Delivery | None:
        row = self.session.get(DeliveryRow, (batch_id, delivery_id))
        return self._attach_attempts(batch_id, [Delivery.model_validate(row.data)])[0] if row else None

    def count_deliveries(self, batch_id: str, status: DeliveryFilter | None, *, role: BilibiliAccountRole | None = None,
                         primary_only: bool = False, include_resending: bool = False) -> int:
        query = select(func.count()).select_from(DeliveryRow).where(DeliveryRow.batch_id == batch_id)
        if status is not None:
            query = query.where(delivery_status_matches(status, include_resending=include_resending))
        if role is not None:
            query = query.where(delivery_role_matches(role, primary_only=primary_only))
        return self.session.execute(query).scalar_one()

    def deliveries(self, batch_id: str, status: DeliveryFilter | None, after: str, count: int,
                   *, offset: int = 0, role: BilibiliAccountRole | None = None,
                   primary_only: bool = False, include_resending: bool = False) -> list[Delivery]:
        query = select(DeliveryRow.data).where(DeliveryRow.batch_id == batch_id)
        order = (DeliveryRow.id,)
        if status == "active":
            rank = case((DeliveryRow.status == "in_flight", 0), (is_priority_resend, 1), else_=2)
            order = (rank, DeliveryRow.id)
            if after:
                cursor_rank, cursor_id = (int(after[0]), after[1:]) if len(after) == 13 else (2, after)
                query = query.where(tuple_(*order) > (cursor_rank, cursor_id))
        else:
            query = query.where(DeliveryRow.id > after)
        if status is not None:
            query = query.where(delivery_status_matches(status, include_resending=include_resending))
        if role is not None:
            query = query.where(delivery_role_matches(role, primary_only=primary_only))
        rows = self.session.scalars(query.order_by(*order).limit(count).offset(offset))
        return self._attach_attempts(batch_id, [Delivery.model_validate(row) for row in rows])

    def next_pending_delivery(self, batch_id: str, role: BilibiliAccountRole | None) -> Delivery | None:
        query = select(DeliveryRow.data).where(DeliveryRow.batch_id == batch_id, DeliveryRow.status == "pending")
        if role is not None:
            query = query.where(func.coalesce(DeliveryRow.data["item"]["account_role"].as_string(), "primary") == role)
        row = self.session.scalar(query.order_by(DeliveryRow.id).limit(1))
        return self._attach_attempts(batch_id, [Delivery.model_validate(row)])[0] if row is not None else None

    def next_priority_resend(self, batch_id: str) -> Delivery | None:
        row = self.session.scalar(select(DeliveryRow.data).where(
            DeliveryRow.batch_id == batch_id, DeliveryRow.status == "pending", is_priority_resend,
        ).order_by(DeliveryRow.id).limit(1))
        return self._attach_attempts(batch_id, [Delivery.model_validate(row)])[0] if row is not None else None

    def has_later_send_attempt(self, batch_id: str, delivery_id: str) -> bool:
        return self.session.scalar(select(select(AttemptRow.number).where(
            AttemptRow.batch_id == batch_id, AttemptRow.delivery_id > delivery_id,
        ).exists()))

    def deliveries_by_id(self, batch_id: str, ids: set[str]) -> list[Delivery]:
        rows = self.session.scalars(select(DeliveryRow.data).where(
            DeliveryRow.batch_id == batch_id, DeliveryRow.id.in_(ids),
        ).order_by(DeliveryRow.id))
        return self._attach_attempts(batch_id, [Delivery.model_validate(row) for row in rows])

