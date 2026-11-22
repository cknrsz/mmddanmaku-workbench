from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, aliased, mapped_column

from danmaku.domain.publication import Publication
from danmaku.domain.workbench import (
    Project, ProjectTaskState, SendQueueStatus, Task, TaskKind, TaskRecord, TaskState, TERMINAL_STATES, WorkbenchError,
)


class Base(DeclarativeBase):
    pass


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_owner_id", "owner_id", "id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str]
    name: Mapped[str]
    source_bvid: Mapped[str]
    target_bvid: Mapped[str]
    source_page: Mapped[int]
    target_page: Mapped[int]

    def dto(self, creation_order: int) -> Project:
        return Project(id=self.id, creation_order=creation_order, name=self.name,
                       target_bvid=self.target_bvid, target_page=self.target_page,
                       source_bvid=self.source_bvid or None, source_page=self.source_page or None)


class ProjectOrderRow(Base):
    __tablename__ = "project_creation_order"
    __table_args__ = {"sqlite_autoincrement": True}

    order: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True)


class ProjectCompletionRow(Base):
    __tablename__ = "project_completion_overrides"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    completed: Mapped[bool]


class ProjectSendShareRow(Base):
    # Retained for SQLite v23 compatibility. Dispatch no longer uses project-level markers.
    __tablename__ = "project_send_shares"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "created_by", "idempotency_key"),
        Index("ix_tasks_project_id", "project_id", "id"),
        Index("ix_tasks_schedule", "state", "next_attempt_at", "order"),
    )

    order: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    created_by: Mapped[str]
    idempotency_key: Mapped[str]
    inputs: Mapped[dict] = mapped_column(JSON)
    source_bvid: Mapped[str]
    source_page: Mapped[int]
    attempts: Mapped[int]
    terminal_batch_error: Mapped[bool]
    kind: Mapped[str]
    state: Mapped[str]
    version: Mapped[int]
    progress: Mapped[dict] = mapped_column(JSON)
    checkpoint: Mapped[dict] = mapped_column(JSON)
    next_attempt_at: Mapped[str | None]
    error: Mapped[dict | None] = mapped_column(JSON)

    def snapshot(self) -> Task:
        return Task.model_validate(self, from_attributes=True)

    def record(self) -> TaskRecord:
        return TaskRecord(
            task=self.snapshot(),
            created_by=self.created_by,
            idempotency_key=self.idempotency_key,
            inputs=self.inputs,
            source_bvid=self.source_bvid,
            source_page=self.source_page,
            attempts=self.attempts,
            terminal_batch_error=self.terminal_batch_error,
        )


class TaskEventRow(Base):
    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON)


class SendQueueRow(Base):
    __tablename__ = "send_queue"

    position: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)


class PublicationRow(Base):
    __tablename__ = "publications"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)
    account_id: Mapped[str]
    publish_at: Mapped[int]
    check_after: Mapped[float]
    checks: Mapped[int]
    overdue: Mapped[bool]
    checking: Mapped[bool]
    due_observed: Mapped[bool]
    time_checked: Mapped[bool]

    def dto(self) -> Publication:
        return Publication(**{column.name: getattr(self, column.name) for column in self.__table__.columns})


class SqliteRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_project(self, owner_id: str, project: Project) -> Project:
        self.session.add(ProjectRow(owner_id=owner_id, id=project.id, name=project.name,
                                    source_bvid=project.source_bvid or "", source_page=project.source_page or 0,
                                    target_bvid=project.target_bvid, target_page=project.target_page))
        self.session.flush()
        order = ProjectOrderRow(project_id=project.id)
        self.session.add(order)
        self.session.flush()
        return project.model_copy(update={"creation_order": order.order})

    def existing_target_parts(self, owner_id: str, bvids: set[str]) -> set[tuple[str, int]]:
        query = select(ProjectRow.target_bvid, ProjectRow.target_page).where(
            ProjectRow.owner_id == owner_id, ProjectRow.target_bvid.in_(bvids),
        )
        return set(self.session.execute(query).tuples())

    def publication(self, project_id: str) -> Publication | None:
        row = self.session.get(PublicationRow, project_id)
        return row.dto() if row else None

    def save_publication(self, publication: Publication) -> None:
        values = asdict(publication)
        self.session.execute(insert(PublicationRow).values(values).on_conflict_do_update(
            index_elements=[PublicationRow.project_id], set_=values,
        ))

    def delete_publication(self, project_id: str) -> None:
        self.session.execute(delete(PublicationRow).where(PublicationRow.project_id == project_id))

    def pending_publications(self, owner_id: str, *, overdue_only: bool = False,
                             first_only: bool = False) -> list[Publication]:
        query = select(PublicationRow).join(ProjectRow).join(ProjectOrderRow).where(
            ProjectRow.owner_id == owner_id, PublicationRow.task_id.is_not(None),
        ).order_by(PublicationRow.publish_at, ProjectOrderRow.order)
        if overdue_only:
            query = query.where(PublicationRow.overdue.is_(True))
        if first_only:
            query = query.limit(1)
        return [row.dto() for row in self.session.scalars(query)]

    def project_publications(self, project_ids: list[str]) -> dict[str, Publication]:
        query = select(PublicationRow).where(
            PublicationRow.project_id.in_(project_ids),
        )
        return {row.project_id: row.dto() for row in self.session.scalars(query)}

    def unobserved_publications(self, owner_id: str, now: float) -> list[Publication]:
        query = select(PublicationRow).join(ProjectRow).where(
            ProjectRow.owner_id == owner_id, PublicationRow.task_id.is_not(None),
            PublicationRow.publish_at <= now, PublicationRow.due_observed.is_(False),
        )
        return [row.dto() for row in self.session.scalars(query)]

    def project(self, owner_id: str, project_id: str) -> Project | None:
        result = self.session.execute(select(ProjectRow, ProjectOrderRow.order).join(ProjectOrderRow).where(
            ProjectRow.id == project_id, ProjectRow.owner_id == owner_id,
        )).one_or_none()
        if result is None:
            return None
        row, order = result
        return row.dto(order)

    def rename_project(self, project_id: str, name: str) -> None:
        self.session.execute(update(ProjectRow).where(ProjectRow.id == project_id).values(name=name))

    def projects(self, owner_id: str, before: int | None, count: int) -> list[Project]:
        query = select(ProjectRow, ProjectOrderRow.order).join(ProjectOrderRow).where(ProjectRow.owner_id == owner_id)
        if before is not None:
            query = query.where(ProjectOrderRow.order < before)
        rows = self.session.execute(query.order_by(ProjectOrderRow.order.desc()).limit(count))
        return [row.dto(order) for row, order in rows]

    def completion_overrides(self, project_ids: list[str]) -> dict[str, bool]:
        return dict(self.session.execute(select(
            ProjectCompletionRow.project_id, ProjectCompletionRow.completed,
        ).where(ProjectCompletionRow.project_id.in_(project_ids))).all())

    def project_task_states(self, project_ids: list[str]) -> dict[str, set[ProjectTaskState]]:
        live_send = (TaskRow.kind == "send") & (TaskRow.inputs["mode"].as_string() == "live")
        send_started = (
            live_send & TaskRow.checkpoint["started"].as_boolean().is_(True)
            & TaskRow.checkpoint["waiting_after_priority"].as_boolean().is_not(True)
        )
        progress = TaskRow.progress
        send_complete = (
            live_send & (progress["done"].as_integer() == progress["total"].as_integer())
            & (func.coalesce(progress["failed"].as_integer(), 0) == 0)
            & (func.coalesce(progress["unknown"].as_integer(), 0) == 0)
        )
        unknown = func.coalesce(progress["unknown"].as_integer(), 0)
        send_needs_review = live_send & (unknown > 0) & (
            progress["done"].as_integer() + unknown == progress["total"].as_integer()
        )
        send_partial_failure = live_send & TaskRow.state.in_(("failed", "needs_review")) & (
            func.coalesce(progress["failed"].as_integer(), 0) > 0
        ) & (progress["done"].as_integer() + unknown == progress["total"].as_integer())
        state_columns = (TaskRow.project_id, TaskRow.kind, TaskRow.state, live_send, send_started,
                         send_complete, send_needs_review, send_partial_failure)
        rows = self.session.execute(select(*state_columns, func.max(TaskRow.order)).where(
            TaskRow.project_id.in_(project_ids),
        ).group_by(*state_columns))
        states: dict[str, set[ProjectTaskState]] = {}
        for project_id, kind, state, live, started, complete, needs_review, partial_failure, order in rows:
            states.setdefault(project_id, set()).add(
                ProjectTaskState(kind, state, bool(live), bool(started), bool(complete), bool(needs_review), bool(partial_failure), order)
            )
        return states

    def set_completion_override(self, project_id: str, completed: bool) -> None:
        self.session.execute(insert(ProjectCompletionRow).values(
            project_id=project_id, completed=completed,
        ).on_conflict_do_update(index_elements=[ProjectCompletionRow.project_id], set_={"completed": completed}))

    def has_active_project_tasks(self, project_id: str, kind: TaskKind | None = None) -> bool:
        query = select(TaskRow.id).where(
            TaskRow.project_id == project_id,
            TaskRow.state.not_in(TERMINAL_STATES | {"paused"}),
        )
        if kind is not None:
            query = query.where(TaskRow.kind == kind)
        return self.session.scalar(query.limit(1)) is not None

    def task(self, task_id: str, owner_id: str | None = None) -> TaskRecord | None:
        query = select(TaskRow).where(TaskRow.id == task_id)
        if owner_id is not None:
            query = query.join(ProjectRow).where(ProjectRow.owner_id == owner_id)
        row = self.session.scalar(query)
        return row.record() if row else None

    def tasks(
        self, project_id: str, after: str, count: int, state: TaskState | None, kind: str | None = None,
    ) -> list[Task]:
        query = select(TaskRow).where(TaskRow.project_id == project_id)
        if after:
            if after.isdecimal() and len(after) <= 19:
                cursor_order = int(after)
            else:
                cursor_order = select(TaskRow.order).where(
                    TaskRow.project_id == project_id, TaskRow.id == after,
                ).correlate(None).scalar_subquery()
            query = query.where(TaskRow.order < cursor_order)
        if state is not None:
            query = query.where(TaskRow.state == state)
        if kind is not None:
            query = query.where(TaskRow.kind == kind)
        rows = self.session.scalars(query.order_by(TaskRow.order.desc()).limit(count))
        return [row.snapshot() for row in rows]

    def by_key(self, owner_id: str, project_id: str, key: str) -> TaskRecord | None:
        row = self.session.scalar(select(TaskRow).where(
            TaskRow.project_id == project_id, TaskRow.created_by == owner_id,
            TaskRow.idempotency_key == key,
        ))
        return row.record() if row else None

    def save_task(self, record: TaskRecord) -> Task:
        snapshot = record.task.model_dump(mode="json", exclude={"creation_order"})
        values = {
            **snapshot,
            "next_attempt_at": schedule_key(record.task.next_attempt_at)
            if record.task.next_attempt_at is not None else None,
        }
        task = record.task
        if task.version == 1:
            row = TaskRow(**record.model_dump(exclude={"task"}), **values)
            self.session.add(row)
            self.session.flush()
            task = row.snapshot()
        else:
            changed = self.session.execute(update(TaskRow).where(
                TaskRow.id == record.task.id, TaskRow.version == record.task.version - 1,
            ).values(
                **values, attempts=record.attempts, terminal_batch_error=record.terminal_batch_error,
            ))
            if changed.rowcount != 1:
                raise WorkbenchError(409, "version_conflict", "任务版本已更新")
        self.session.add(TaskEventRow(
            task_id=record.task.id, seq=record.task.version, snapshot=snapshot,
        ))
        if task.kind == "send":
            if task.state == "queued" and task.checkpoint.publication != "pending":
                self.session.execute(insert(SendQueueRow).values(task_id=task.id).on_conflict_do_nothing(
                    index_elements=[SendQueueRow.task_id],
                ))
            elif task.state in TERMINAL_STATES:
                self.session.execute(delete(SendQueueRow).where(SendQueueRow.task_id == task.id))
                self.session.execute(delete(PublicationRow).where(PublicationRow.task_id == task.id))
        return task

    def due(
        self, now: datetime, *, sending: bool, ignore_deadline: bool = False, kind: TaskKind | None = None,
    ) -> TaskRecord | None:
        query = select(TaskRow).where(TaskRow.state.in_(("queued", "backoff")))
        if kind is not None:
            query = query.where(TaskRow.kind == kind)
        if not ignore_deadline:
            query = query.where(or_(TaskRow.next_attempt_at.is_(None),
                                    TaskRow.next_attempt_at <= schedule_key(now)))
        if sending:
            # A paused, delayed or in-flight head keeps its place until it finishes.
            query = query.where(TaskRow.id == self._send_queue_head_id())
        else:
            active = aliased(TaskRow)
            busy = select(active.id).where(
                active.project_id == TaskRow.project_id, active.kind == TaskRow.kind,
                active.state.in_(("running", "pausing")),
            ).exists()
            query = query.where(TaskRow.kind != "send", ~busy).order_by(TaskRow.order)
        row = self.session.scalar(query.limit(1))
        return row.record() if row else None

    @staticmethod
    def _send_queue_head_id():
        return select(SendQueueRow.task_id).order_by(SendQueueRow.position).limit(1).scalar_subquery()

    def send_queue_records(self, owner_id: str, project_id: str | None = None,
                           *, upgrades_only: bool = False) -> list[TaskRecord]:
        query = select(TaskRow).join(ProjectRow).join(SendQueueRow).where(ProjectRow.owner_id == owner_id)
        if project_id is not None:
            query = query.where(TaskRow.project_id == project_id)
        if upgrades_only:
            query = query.where(TaskRow.checkpoint["secondary_upgrade_id"].as_string().is_not(None))
        return [row.record() for row in self.session.scalars(query.order_by(SendQueueRow.position))]

    def send_queue_head(self, owner_id: str) -> Task | None:
        row = self.session.scalar(select(TaskRow).join(ProjectRow).where(
            TaskRow.id == self._send_queue_head_id(), ProjectRow.owner_id == owner_id,
        ))
        return row.snapshot() if row else None

    def send_dispatch_records(self) -> list[TaskRecord]:
        query = select(TaskRow).join(SendQueueRow).order_by(SendQueueRow.position).limit(2)
        return [row.record() for row in self.session.scalars(query)]

    def has_project_send_in_queue(self, project_id: str) -> bool:
        publication = self.publication(project_id)
        if publication is not None and publication.task_id is not None:
            return True
        query = select(SendQueueRow.task_id).join(TaskRow, TaskRow.id == SendQueueRow.task_id).where(
            TaskRow.project_id == project_id,
        )
        return self.session.scalar(query.limit(1)) is not None

    def has_dispatched_publication(self, owner_id: str) -> bool:
        query = select(TaskRow.id).join(SendQueueRow).where(
            TaskRow.created_by == owner_id, TaskRow.checkpoint["publication"].as_string() == "dispatch",
        )
        return self.session.scalar(query.limit(1)) is not None

    def move_send_to_front(self, task_id: str, *, after_head: bool) -> None:
        rows = self.session.execute(
            select(SendQueueRow.position, SendQueueRow.task_id).order_by(SendQueueRow.position)
        ).tuples().all()
        if len(rows) < 2:
            return
        positions, ids = map(list, zip(*rows[int(after_head):], strict=True))
        ids.remove(task_id)
        ids.insert(0, task_id)
        self.session.execute(delete(SendQueueRow).where(SendQueueRow.position.in_(positions)))
        self.session.execute(insert(SendQueueRow).values([
            {"position": position, "task_id": queue_id}
            for position, queue_id in zip(positions, ids, strict=True)
        ]))

    def swap_send_queue_positions(self, first_id: str, second_id: str) -> None:
        pair = SendQueueRow.task_id.in_((first_id, second_id))
        positions = dict(self.session.execute(
            select(SendQueueRow.task_id, SendQueueRow.position).where(pair)
        ).tuples().all())
        self.session.execute(delete(SendQueueRow).where(pair))
        self.session.execute(insert(SendQueueRow).values([
            {"task_id": first_id, "position": positions[second_id]},
            {"task_id": second_id, "position": positions[first_id]},
        ]))

    def project_send_queue(self, project_ids: list[str]) -> dict[str, SendQueueStatus]:
        started_head = (
            (SendQueueRow.task_id == self._send_queue_head_id().correlate(None))
            & TaskRow.checkpoint["started"].as_boolean().is_(True)
            & TaskRow.checkpoint["waiting_after_priority"].as_boolean().is_not(True)
        )
        queue = select(
            TaskRow.project_id,
            func.row_number().over(order_by=SendQueueRow.position).label("position"),
            func.count().over().label("total"),
        ).select_from(SendQueueRow).join(TaskRow, TaskRow.id == SendQueueRow.task_id).where(
            TaskRow.state == "queued", ~started_head,
        ).subquery()
        rows = self.session.execute(
            select(queue.c.project_id, func.min(queue.c.position), queue.c.total)
            .where(queue.c.project_id.in_(project_ids))
            .group_by(queue.c.project_id, queue.c.total)
        )
        return {
            project_id: SendQueueStatus(position=position, total=total)
            for project_id, position, total in rows
        }

    def retired_send_tasks(self) -> list[TaskRecord]:
        rows = self.session.scalars(select(TaskRow).where(
            TaskRow.kind == "send", TaskRow.state.not_in(TERMINAL_STATES),
            or_(TaskRow.inputs["mode"].as_string() == "mock", TaskRow.inputs["mode"].as_string().is_(None)),
        ))
        return [row.record() for row in rows]

    def interrupted(self) -> list[TaskRecord]:
        rows = self.session.scalars(select(TaskRow).where(or_(
            TaskRow.state.in_(("running", "pausing", "cancelling")),
            (TaskRow.kind == "classify") & TaskRow.state.in_(("queued", "backoff"))
            & (TaskRow.error["retryable"].as_boolean().is_(True)),
            (TaskRow.kind == "send") & TaskRow.state.in_(("queued", "backoff"))
            & (TaskRow.inputs["mode"].as_string() == "live"),
        )))
        return [row.record() for row in rows]

    def paused_sends_with_history(self, owner_id: str, error_code: str) -> list[tuple[TaskRecord, Task]]:
        event = aliased(TaskEventRow)
        # Edits to pending deliveries can add events while the batch stays paused.
        before_pause = select(event.seq).where(
            event.task_id == TaskRow.id, event.seq < TaskRow.version,
            event.snapshot["state"].as_string() != "paused",
        ).order_by(event.seq.desc()).limit(1).correlate(TaskRow).scalar_subquery()
        rows = self.session.execute(
            select(TaskRow, TaskEventRow.snapshot).join(ProjectRow)
            .join(SendQueueRow, SendQueueRow.task_id == TaskRow.id)
            .join(TaskEventRow, (TaskEventRow.task_id == TaskRow.id) & (TaskEventRow.seq == before_pause))
            .where(
                ProjectRow.owner_id == owner_id, TaskRow.kind == "send", TaskRow.state == "paused",
                TaskRow.inputs["mode"].as_string() == "live", TaskRow.error["code"].as_string() == error_code,
            ).order_by(SendQueueRow.position)
        )
        return [
            (row.record(), Task.model_validate({**snapshot, "creation_order": row.order}))
            for row, snapshot in rows
        ]

    def events(self, task_id: str, after: int, count: int) -> list[Task]:
        rows = self.session.execute(select(TaskEventRow.snapshot, TaskRow.order).join(
            TaskRow, TaskRow.id == TaskEventRow.task_id,
        ).where(
            TaskEventRow.task_id == task_id, TaskEventRow.seq > after,
        ).order_by(TaskEventRow.seq).limit(count))
        return [Task.model_validate({**snapshot, "creation_order": order}) for snapshot, order in rows]


def schedule_key(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")

