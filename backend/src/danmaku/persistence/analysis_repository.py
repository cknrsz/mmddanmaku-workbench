from sqlalchemy import JSON, ForeignKey, ForeignKeyConstraint, Index, UniqueConstraint, delete, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from danmaku.domain.analysis import AnalysisInput, AnalysisRun, Classification, ModelConfig
from danmaku.domain.inputs import Cue, NormalizedItem, REPOST_NOTICE, Snapshot
from danmaku.domain.workbench import TaskState
from danmaku.persistence.input_repository import InputRepository
from danmaku.persistence.input_rows import CueRow, SnapshotRow, SourceItemRow
from danmaku.persistence.repository import Base, TaskRow


class ModelConfigRow(Base):
    __tablename__ = "llm_configs"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)


class ModelCredentialRow(Base):
    __tablename__ = "llm_credentials"

    owner_id: Mapped[str] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(primary_key=True)
    reference: Mapped[str]


class AnalysisRunRow(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        Index("ix_analysis_project", "project_id", "id"),
        UniqueConstraint("id", "snapshot_id"),
    )

    id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"))
    primary_subtitle_id: Mapped[str | None] = mapped_column("subtitle_version_id", ForeignKey("subtitle_versions.id"))
    inputs: Mapped[dict] = mapped_column(JSON)


class AnalysisBatchRow(Base):
    """Retain the historical table for existing databases and backups."""
    __tablename__ = "analysis_batches"

    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    index: Mapped[int] = mapped_column(primary_key=True)
    spec: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[str | None]
    diagnostic: Mapped[str | None]


class ClassificationRow(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "snapshot_id"], ["analysis_runs.id", "analysis_runs.snapshot_id"]),
        ForeignKeyConstraint(["snapshot_id", "id"], ["source_items.snapshot_id", "source_items.source_key"]),
    )

    run_id: Mapped[str] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str]
    result: Mapped[dict] = mapped_column(JSON)


class AnalysisRepository(InputRepository):
    def model_config(self, owner_id: str, version: int | None = None) -> ModelConfig | None:
        query = select(ModelConfigRow.config).where(ModelConfigRow.owner_id == owner_id)
        if version is None:
            query = query.order_by(ModelConfigRow.version.desc()).limit(1)
        else:
            query = query.where(ModelConfigRow.version == version)
        config = self.session.scalar(query)
        return ModelConfig.model_validate(config) if config is not None else None

    def save_model_config(self, owner_id: str, config: ModelConfig) -> None:
        self.session.add(ModelConfigRow(owner_id=owner_id, version=config.version, config=config.model_dump()))

    def model_credential(self, owner_id: str, endpoint: str) -> str | None:
        row = self.session.get(ModelCredentialRow, (owner_id, endpoint))
        return row.reference if row else None

    def save_model_credential(self, owner_id: str, endpoint: str, reference: str) -> None:
        self.session.merge(ModelCredentialRow(owner_id=owner_id, endpoint=endpoint, reference=reference))

    def remove_model_credential(self, owner_id: str, endpoint: str) -> None:
        self.session.execute(delete(ModelCredentialRow).where(
            ModelCredentialRow.owner_id == owner_id, ModelCredentialRow.endpoint == endpoint,
        ))

    def add_run(self, run: AnalysisInput) -> None:
        self.session.add(AnalysisRunRow(
            id=run.id, project_id=run.project_id, snapshot_id=run.snapshot_id,
            primary_subtitle_id=run.subtitle_version_id or run.japanese_subtitle_version_id, inputs=run.model_dump(),
        ))

    def subtitle_in_use(self, project_id: str, version_id: str) -> bool:
        reference = select(AnalysisRunRow.id).where(
            AnalysisRunRow.project_id == project_id,
            or_(AnalysisRunRow.primary_subtitle_id == version_id,
                AnalysisRunRow.inputs["japanese_subtitle_version_id"].as_string() == version_id),
        )
        return bool(self.session.scalar(select(reference.exists())))

    def analysis_input(self, run_id: str) -> AnalysisInput:
        row = self.session.get(AnalysisRunRow, run_id)
        assert row is not None
        return AnalysisInput.model_validate(row.inputs)

    def _runs(self, project_id: str):
        return (select(AnalysisRunRow.inputs, TaskRow.state, SnapshotRow.summary)
                .join(TaskRow, TaskRow.id == AnalysisRunRow.id)
                .join(SnapshotRow, SnapshotRow.id == AnalysisRunRow.snapshot_id)
                .where(AnalysisRunRow.project_id == project_id))

    def run(self, project_id: str, run_id: str) -> AnalysisRun | None:
        row = self.session.execute(self._runs(project_id).where(AnalysisRunRow.id == run_id)).first()
        return self._run(*row) if row else None

    def runs(self, project_id: str, after: str = "", count: int | None = None) -> list[AnalysisRun]:
        rows = self.session.execute(self._runs(project_id).where(
            AnalysisRunRow.id > after,
        ).order_by(AnalysisRunRow.id).limit(count))
        return [self._run(*row) for row in rows]

    @staticmethod
    def _run(inputs: dict, state: TaskState, summary: dict) -> AnalysisRun:
        name = Snapshot.model_validate(summary).name
        title = f"{name}（已跳过分析）" if inputs.get("strategy") == "skipped" else f"{name}分析"
        return AnalysisRun(**inputs, state=state, name=title)

    def run_ids(self, project_id: str) -> list[str]:
        return list(self.session.scalars(select(AnalysisRunRow.id).where(
            AnalysisRunRow.project_id == project_id,
        ).order_by(AnalysisRunRow.id)))

    def analysis_source_items(self, snapshot_id: str) -> list[NormalizedItem]:
        item = SourceItemRow
        rows = self.session.scalars(
            select(item.normalized).where(
                item.snapshot_id == snapshot_id, item.source_key != REPOST_NOTICE.source_key,
            ).order_by(item.time_ms, item.source_key)
        )
        return [NormalizedItem.model_validate(row) for row in rows]

    def cues(self, version_id: str) -> list[Cue]:
        rows = self.session.scalars(select(CueRow.cue).where(CueRow.version_id == version_id)
                                    .order_by(CueRow.order))
        return [Cue.model_validate(row) for row in rows]

    def classifications(self, run_id: str, after: str, count: int) -> list[Classification]:
        rows = self.session.scalars(select(ClassificationRow.result).where(
            ClassificationRow.run_id == run_id, ClassificationRow.id > after,
        ).order_by(ClassificationRow.id).limit(count))
        return [Classification.model_validate(row) for row in rows]

    def save_classifications(self, run: AnalysisInput, results: list[Classification]) -> None:
        if results:
            self.session.execute(ClassificationRow.__table__.insert(), [
                {"run_id": run.id, "snapshot_id": run.snapshot_id, "id": result.id, "result": result.model_dump()}
                for result in results
            ])
