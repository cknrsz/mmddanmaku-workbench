from datetime import datetime

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, and_, distinct, func, literal, or_, select, tuple_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from danmaku.domain.analysis import AnalysisRun, Classification
from danmaku.domain.inputs import NormalizedItem, REPOST_NOTICE
from danmaku.domain.review import ReviewFilter
from danmaku.domain.settings import ReviewSettings
from danmaku.domain.workbench import WorkbenchError
from danmaku.persistence.analysis_repository import AnalysisRepository, ClassificationRow
from danmaku.persistence.input_rows import SourceItemRow, WorkflowRow
from danmaku.persistence.repository import Base, ProjectRow


class ReviewDecisionRow(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "snapshot_id"], ["analysis_runs.id", "analysis_runs.snapshot_id"]),
        ForeignKeyConstraint(["snapshot_id", "item_id"], ["source_items.snapshot_id", "source_items.source_key"]),
        Index("ix_review_item_revision", "run_id", "item_id", "revision"),
    )

    project_id: Mapped[str] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    snapshot_id: Mapped[str]
    selected: Mapped[bool]
    actor_id: Mapped[str]
    changed_at: Mapped[str]


class ReviewOffsetRow(Base):
    __tablename__ = "review_offsets"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "snapshot_id"], ["analysis_runs.id", "analysis_runs.snapshot_id"],
                             ondelete="CASCADE"),
        ForeignKeyConstraint(["snapshot_id", "item_id"], ["source_items.snapshot_id", "source_items.source_key"]),
    )

    run_id: Mapped[str] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str]
    offset_ms: Mapped[int]


class ReviewTextRow(Base):
    __tablename__ = "review_texts"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "snapshot_id"], ["analysis_runs.id", "analysis_runs.snapshot_id"],
                             ondelete="CASCADE"),
        ForeignKeyConstraint(["snapshot_id", "item_id"], ["source_items.snapshot_id", "source_items.source_key"]),
    )

    run_id: Mapped[str] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str]
    text: Mapped[str]


class ReviewRevisionRow(Base):
    __tablename__ = "review_revisions"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int]


class ReviewResetRow(Base):
    __tablename__ = "review_resets"

    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[str]
    changed_at: Mapped[str]


class ReviewSettingsRow(Base):
    __tablename__ = "review_settings"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    uncheck_small_by_default: Mapped[bool]


class ReviewRepository(AnalysisRepository):
    def review_settings(self, owner_id: str) -> ReviewSettings:
        row = self.session.get(ReviewSettingsRow, owner_id)
        if row is None:
            return ReviewSettings()
        return ReviewSettings(uncheck_small_by_default=row.uncheck_small_by_default)

    def save_review_settings(self, owner_id: str, settings: ReviewSettings) -> None:
        self.session.merge(ReviewSettingsRow(owner_id=owner_id,
                                             uncheck_small_by_default=settings.uncheck_small_by_default))
        # An open preview must not submit a selection computed under a previous default.
        projects = select(ProjectRow.id, literal(1)).where(ProjectRow.owner_id == owner_id)
        statement = insert(ReviewRevisionRow).from_select(["project_id", "revision"], projects)
        self.session.execute(statement.on_conflict_do_update(
            index_elements=[ReviewRevisionRow.project_id],
            set_={"revision": ReviewRevisionRow.revision + 1},
        ))

    def review_revision(self, project_id: str) -> int:
        row = self.session.get(ReviewRevisionRow, project_id)
        return row.revision if row is not None else 0

    def projects_with_current_review(self, project_ids: list[str]) -> set[str]:
        review_revision = WorkflowRow.data["review"]["revision"].as_integer()
        query = select(WorkflowRow.project_id).outerjoin(
            ReviewRevisionRow, ReviewRevisionRow.project_id == WorkflowRow.project_id,
        ).where(
            WorkflowRow.project_id.in_(project_ids),
            review_revision == func.coalesce(ReviewRevisionRow.revision, 0),
        )
        return set(self.session.scalars(query))

    def _manual_choice(self, run: AnalysisRun):
        return (select(ReviewDecisionRow.selected)
                .where(ReviewDecisionRow.run_id == run.id, ReviewDecisionRow.item_id == SourceItemRow.source_key,
                       ReviewDecisionRow.revision > self._latest_reset(run))
                .order_by(ReviewDecisionRow.revision.desc()).limit(1).scalar_subquery())

    def _latest_reset(self, run: AnalysisRun):
        return (select(func.coalesce(func.max(ReviewResetRow.revision), 0))
                .where(ReviewResetRow.run_id == run.id).scalar_subquery())

    def _review_query(self, run: AnalysisRun, *columns):
        return (select(*columns).select_from(SourceItemRow)
                .outerjoin(ClassificationRow, and_(ClassificationRow.run_id == run.id,
                                                   ClassificationRow.id == SourceItemRow.source_key))
                .where(SourceItemRow.snapshot_id == run.snapshot_id))

    def _suggestion(self):
        result = ClassificationRow.result
        return and_(
            result["category"].as_string() == "other",
            result["diagnostic"].as_string().is_(None),
            # Preserve the default selection encoded by historic results.
            func.coalesce(result["harm_assessment"]["status"].as_string() == "unflagged", True),
        )

    def _selected(self, run: AnalysisRun):
        small_exclusion = (
            select(ReviewSettingsRow.uncheck_small_by_default)
            .join(ProjectRow, ProjectRow.owner_id == ReviewSettingsRow.owner_id)
            .where(ProjectRow.id == run.project_id)
            .scalar_subquery()
        )
        default = True if run.strategy == "skipped" else and_(
            or_(SourceItemRow.normalized["font_size"].as_integer() != 18,
                func.coalesce(small_exclusion, True).is_(False)),
            or_(SourceItemRow.source_key == REPOST_NOTICE.source_key, self._suggestion()),
        )
        return func.coalesce(
            self._manual_choice(run),
            default,
            False,
        )

    def review_selected_count(self, run: AnalysisRun) -> int:
        return self.session.scalar(self._review_query(run, func.count()).where(self._selected(run))) or 0

    def _offset(self, run: AnalysisRun):
        value = select(ReviewOffsetRow.offset_ms).where(
            ReviewOffsetRow.run_id == run.id, ReviewOffsetRow.item_id == SourceItemRow.source_key,
        ).scalar_subquery()
        return func.coalesce(value, 0)

    def _text(self, run: AnalysisRun):
        override = select(ReviewTextRow.text).where(
            ReviewTextRow.run_id == run.id, ReviewTextRow.item_id == SourceItemRow.source_key,
        ).scalar_subquery()
        return func.coalesce(override, SourceItemRow.normalized["text"].as_string())

    def save_review_text(self, run: AnalysisRun, revision: int, item_id: str, text: str | None) -> bool:
        item = self.session.get(SourceItemRow, (run.snapshot_id, item_id))
        if item is None:
            raise WorkbenchError(404, "not_found", "审核条目不存在")
        override = self.session.get(ReviewTextRow, (run.id, item_id))
        original = item.normalized["text"]
        if text is None:
            text = original
        if (override.text if override else original) == text:
            return False
        if text == original:
            self.session.delete(override)
        elif override is None:
            self.session.add(ReviewTextRow(run_id=run.id, item_id=item_id, snapshot_id=run.snapshot_id, text=text))
        else:
            override.text = text
        self._save_review_revision(run.project_id, revision)
        return True

    def selected_items(self, run: AnalysisRun) -> list[tuple[NormalizedItem, int]]:
        rows = self.session.execute(
            self._review_query(run, SourceItemRow.normalized, self._offset(run), self._text(run))
            .where(self._selected(run))
        )
        return [(NormalizedItem.model_validate({**data, "text": text}), offset_ms) for data, offset_ms, text in rows]

    def save_review_offset(
        self, run: AnalysisRun, revision: int, item_id: str, offset_ms: int, apply_to_following: bool,
    ) -> int:
        item = SourceItemRow
        anchor = self.session.get(item, (run.snapshot_id, item_id))
        if anchor is None:
            raise WorkbenchError(404, "not_found", "审核条目不存在")
        scope = item.source_key == item_id
        if apply_to_following:
            scope = tuple_(item.time_ms, item.source_key) >= (anchor.time_ms, item_id)
        changed = select(literal(run.id), item.source_key, item.snapshot_id, literal(offset_ms)).where(
            item.snapshot_id == run.snapshot_id, scope, self._offset(run) != offset_ms,
        )
        statement = insert(ReviewOffsetRow).from_select(
            ["run_id", "item_id", "snapshot_id", "offset_ms"], changed,
        ).on_conflict_do_update(
            index_elements=[ReviewOffsetRow.run_id, ReviewOffsetRow.item_id], set_={"offset_ms": offset_ms},
        )
        count = self.session.execute(statement).rowcount
        if count:
            self._save_review_revision(run.project_id, revision)
        return count

    def review_items(
        self, run: AnalysisRun, filters: ReviewFilter, after: tuple[int, str] | None, count: int,
    ) -> list[tuple[NormalizedItem, Classification | None, bool, bool, int, bool]]:
        item = SourceItemRow
        result = ClassificationRow.result
        text = self._text(run)
        statement = self._review_query(run, item.normalized, result, self._selected(run),
                                       self._manual_choice(run).is_not(None), self._offset(run), text)
        if filters.category is not None:
            statement = statement.where(result["category"].as_string() == filters.category)
        if filters.selected is not None:
            statement = statement.where(self._selected(run) == filters.selected)
        if filters.q:
            statement = statement.where(func.instr(
                func.lower(text), func.lower(filters.q),
            ) > 0)
        if after is not None:
            time_ms, key = after
            statement = statement.where(or_(item.time_ms > time_ms,
                                           and_(item.time_ms == time_ms, item.source_key > key)))

        return [
            (NormalizedItem.model_validate({**data, "text": content}),
             Classification.model_validate(result) if result else None,
             bool(selected), manual, offset_ms, content != data["text"])
            for data, result, selected, manual, offset_ms, content in self.session.execute(
                statement.order_by(item.time_ms, item.source_key).limit(count)
            )
        ]

    def review_choices(self, run: AnalysisRun, item_ids: set[str]) -> dict[str, bool | None]:
        rows = self.session.execute(
            select(SourceItemRow.source_key, self._manual_choice(run)).where(
                SourceItemRow.snapshot_id == run.snapshot_id, SourceItemRow.source_key.in_(item_ids),
            )
        )
        return dict(rows.tuples().all())

    def _save_review_revision(self, project_id: str, revision: int) -> None:
        self.session.execute(insert(ReviewRevisionRow).values(project_id=project_id, revision=revision)
                             .on_conflict_do_update(index_elements=[ReviewRevisionRow.project_id],
                                                    set_={"revision": revision}))

    def append_review(
        self, run: AnalysisRun, revision: int, item_ids: list[str], selected: bool,
        actor_id: str, changed_at: datetime,
    ) -> None:
        self._save_review_revision(run.project_id, revision)
        self.session.add_all([
            ReviewDecisionRow(project_id=run.project_id, run_id=run.id, snapshot_id=run.snapshot_id,
                              item_id=item_id, revision=revision, selected=selected,
                              actor_id=actor_id, changed_at=changed_at.isoformat())
            for item_id in item_ids
        ])
        self.session.flush()

    def review_manual_count(self, run: AnalysisRun) -> int:
        return self.session.scalar(select(func.count(distinct(ReviewDecisionRow.item_id))).where(
            ReviewDecisionRow.run_id == run.id, ReviewDecisionRow.revision > self._latest_reset(run),
        )) or 0

    def append_review_reset(self, run: AnalysisRun, revision: int, actor_id: str, changed_at: datetime) -> None:
        self._save_review_revision(run.project_id, revision)
        self.session.add(ReviewResetRow(run_id=run.id, revision=revision, actor_id=actor_id,
                                        changed_at=changed_at.isoformat()))
        self.session.flush()
