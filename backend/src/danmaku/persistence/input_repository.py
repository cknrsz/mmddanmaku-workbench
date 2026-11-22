from sqlalchemy import JSON, delete, func, literal, select, tuple_
from sqlalchemy.dialects.sqlite import insert

from danmaku.domain.inputs import (
    Cue, NormalizedItem, ProjectParts, REPOST_NOTICE, Snapshot, SnapshotItem, SourceItem, SubtitleVersion, Workflow,
)
from danmaku.persistence.input_rows import CueRow, ProjectPartsRow, SnapshotRow, SourceItemRow, SubtitleRow, WorkflowRow
from danmaku.persistence.repository import SqliteRepository, TaskRow


class InputRepository(SqliteRepository):
    def save_video_parts(self, project_id: str, parts: ProjectParts) -> None:
        self.session.flush()
        self.session.add(ProjectPartsRow(project_id=project_id, parts=parts.model_dump()))

    def video_parts(self, project_id: str) -> ProjectParts | None:
        row = self.session.get(ProjectPartsRow, project_id)
        return ProjectParts.model_validate(row.parts) if row else None

    def _snapshots(self, project_id: str):
        return (select(SnapshotRow).join(TaskRow, TaskRow.id == SnapshotRow.id)
                .where(TaskRow.project_id == project_id))

    def snapshots(self, project_id: str) -> list[Snapshot]:
        rows = self.session.scalars(self._snapshots(project_id).order_by(TaskRow.order.desc()))
        return [Snapshot.model_validate(row.summary) for row in rows]

    def next_snapshot_sequence(self, project_id: str) -> int:
        maximum = self.session.scalar(self._snapshots(project_id).with_only_columns(
            func.max(SnapshotRow.summary["sequence"].as_integer()),
        ))
        return (maximum or 0) + 1

    def snapshot(self, project_id: str, snapshot_id: str) -> Snapshot | None:
        row = self.session.scalar(self._snapshots(project_id).where(SnapshotRow.id == snapshot_id))
        return Snapshot.model_validate(row.summary) if row else None

    def save_snapshot(self, snapshot: Snapshot) -> None:
        summary = snapshot.model_dump(exclude={"name"})
        if snapshot.merged_name is not None:
            summary["merged_name"] = snapshot.merged_name
        row = self.session.get(SnapshotRow, snapshot.id)
        if row:
            row.summary = summary
        else:
            self.session.add(SnapshotRow(id=snapshot.id, summary=summary))
        self.session.flush()

    def snapshot_items(
        self, snapshot_id: str, after: tuple[int, str] | None, limit: int,
    ) -> list[SnapshotItem]:
        item = SourceItemRow
        query = select(item.normalized).where(item.snapshot_id == snapshot_id, item.source_key != REPOST_NOTICE.source_key)
        if after is not None:
            query = query.where(tuple_(item.time_ms, item.source_key) > after)
        rows = self.session.scalars(query.order_by(item.time_ms, item.source_key).limit(limit))
        return [SnapshotItem(id=row["source_key"], **row) for row in rows]

    def copy_snapshot_items(self, target_id: str, source_id: str) -> None:
        item = SourceItemRow
        key = literal(f"{source_id}:") + item.source_key
        rows = select(literal(target_id), key, item.time_ms, item.raw,
                      func.json_set(item.normalized, "$.source_key", key, "$.delivery_key", item.source_key)).where(
            item.snapshot_id == source_id, item.source_key != REPOST_NOTICE.source_key,
        )
        self.session.execute(insert(item.__table__).from_select(
            ["snapshot_id", "source_key", "time_ms", "raw", "normalized"], rows,
        ))

    def workflow(self, project_id: str) -> Workflow | None:
        row = self.session.get(WorkflowRow, project_id)
        return Workflow.model_validate(row.data) if row else None

    def save_workflow(self, project_id: str, workflow: Workflow) -> None:
        self.session.merge(WorkflowRow(project_id=project_id, data=workflow.model_dump()))

    def add_source_items(
        self, snapshot_id: str, items: list[tuple[SourceItem, NormalizedItem]],
    ) -> int:
        if not items:
            return 0
        statement = insert(SourceItemRow.__table__).on_conflict_do_nothing(
            index_elements=["snapshot_id", "source_key"],
        )
        return self.session.execute(statement, [
            {"snapshot_id": snapshot_id, "source_key": normalized.source_key,
             "time_ms": normalized.time_ms, "raw": raw.model_dump(),
             "normalized": normalized.model_dump()}
            for raw, normalized in items
        ]).rowcount

    def add_repost_notice(self, snapshot_id: str | None = None) -> None:
        item = SourceItemRow
        snapshot = SnapshotRow
        notice_present = select(item.source_key).where(
            item.snapshot_id == snapshot.id,
            item.normalized["text"].as_string() == REPOST_NOTICE.text,
        ).exists()
        candidates = select(
            snapshot.id, literal(REPOST_NOTICE.source_key), literal(REPOST_NOTICE.time_ms),
            literal({}, type_=JSON), literal(REPOST_NOTICE.model_dump(), type_=JSON),
        ).where(snapshot.summary["mode"].as_string() == "bilibili",
                snapshot.summary["state"].as_string() == "complete", ~notice_present)
        if snapshot_id is not None:
            candidates = candidates.where(snapshot.id == snapshot_id)
        self.session.execute(insert(item.__table__).from_select(
            ["snapshot_id", "source_key", "time_ms", "raw", "normalized"], candidates,
        ).on_conflict_do_nothing(index_elements=["snapshot_id", "source_key"]))

    def subtitles(self, project_id: str) -> list[SubtitleVersion]:
        rows = self.session.scalars(select(SubtitleRow).where(
            SubtitleRow.project_id == project_id,
        ).order_by(SubtitleRow.id))
        return [SubtitleVersion.model_validate(row.summary) for row in rows]

    def subtitle(self, project_id: str, version_id: str) -> SubtitleVersion | None:
        summary = self.session.scalar(select(SubtitleRow.summary).where(
            SubtitleRow.project_id == project_id, SubtitleRow.id == version_id,
        ))
        return SubtitleVersion.model_validate(summary) if summary is not None else None

    def save_subtitle(
        self, project_id: str, version: SubtitleVersion, original: bytes, cues: list[Cue],
    ) -> SubtitleVersion:
        row = self.session.get(SubtitleRow, version.subtitle_version_id)
        if row is not None:
            saved = SubtitleVersion.model_validate(row.summary)
            if saved.filename is None and version.filename:
                saved.filename = version.filename
                row.summary = saved.model_dump()
            return saved
        self.session.add(SubtitleRow(id=version.subtitle_version_id, project_id=project_id,
                                    summary=version.model_dump(), original=original))
        self.session.flush()
        self.session.add_all(CueRow(version_id=version.subtitle_version_id, order=cue.order,
                                    cue=cue.model_dump()) for cue in cues)
        return version

    def delete_subtitle(self, project_id: str, version_id: str) -> None:
        version = select(SubtitleRow.id).where(SubtitleRow.project_id == project_id, SubtitleRow.id == version_id)
        self.session.execute(delete(CueRow).where(CueRow.version_id.in_(version)))
        self.session.execute(delete(SubtitleRow).where(SubtitleRow.project_id == project_id, SubtitleRow.id == version_id))
