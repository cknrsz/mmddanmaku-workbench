from sqlalchemy import JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from danmaku.persistence.repository import Base


class ProjectPartsRow(Base):
    __tablename__ = "project_parts"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    parts: Mapped[dict] = mapped_column(JSON)


class WorkflowRow(Base):
    __tablename__ = "project_workflows"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class SnapshotRow(Base):
    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    summary: Mapped[dict] = mapped_column(JSON)


class SourceItemRow(Base):
    __tablename__ = "source_items"
    __table_args__ = (Index("ix_source_items_time", "snapshot_id", "time_ms", "source_key"),)

    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"), primary_key=True)
    source_key: Mapped[str] = mapped_column(primary_key=True)
    time_ms: Mapped[int]
    raw: Mapped[dict] = mapped_column(JSON)
    normalized: Mapped[dict] = mapped_column(JSON)


class SubtitleRow(Base):
    __tablename__ = "subtitle_versions"
    __table_args__ = (Index("ix_subtitles_project", "project_id", "id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    summary: Mapped[dict] = mapped_column(JSON)
    original: Mapped[bytes]


class CueRow(Base):
    __tablename__ = "subtitle_cues"

    version_id: Mapped[str] = mapped_column(ForeignKey("subtitle_versions.id"), primary_key=True)
    order: Mapped[int] = mapped_column(primary_key=True)
    cue: Mapped[dict] = mapped_column(JSON)
