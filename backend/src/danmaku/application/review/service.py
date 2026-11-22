from bisect import bisect_right
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps, loads

from danmaku.application.pagination import decode_cursor, encode_cursor
from danmaku.application.ports import Repository
from danmaku.application.projects import require_project
from danmaku.domain.analysis import AnalysisRun
from danmaku.domain.review import (
    ReviewChange, ReviewCommit, ReviewFilter, ReviewOffset, ReviewPage, ReviewRevision, ReviewRow, ReviewText,
)
from danmaku.domain.workbench import Principal, WorkbenchError
from danmaku.persistence.database import Database

CUE_WINDOW_MS = 5_000
NEARBY_LIMIT = 12


def review_run(repository: Repository, project_id: str, run_id: str) -> AnalysisRun:
    run = repository.run(project_id, run_id)
    if run is None:
        raise WorkbenchError(404, "not_found", "分析运行不存在")
    if run.state not in {"succeeded", "failed"}:
        raise WorkbenchError(409, "run_in_progress", "请等待分析结束后再审核")
    return run


def valid_position(value: str) -> bool:
    position = loads(value)
    return (isinstance(position, list) and len(position) == 2 and type(position[0]) is int
            and isinstance(position[1], str) and bool(position[1]))


def require_review_revision(repository: Repository, project_id: str, expected_revision: int) -> int:
    revision = repository.review_revision(project_id)
    if revision != expected_revision:
        raise WorkbenchError(409, "revision_conflict", "审核版本已更新")
    return revision


class ReviewService:
    def __init__(self, database: Database):
        self.database = database

    def list(
        self, actor: Principal, project_id: str, run_id: str, filters: ReviewFilter,
        cursor: str | None, limit: int,
    ) -> ReviewPage:
        scope = ["review", project_id, run_id, sha256(filters.model_dump_json().encode()).hexdigest()]
        token = decode_cursor(scope, cursor, valid_id=valid_position)
        after = tuple(loads(token)) if token else None
        with self.database() as repository:
            require_project(repository, actor, project_id)
            run = review_run(repository, project_id, run_id)
            revision = repository.review_revision(project_id)
            selected_count = repository.review_selected_count(run)
            rows = repository.review_items(run, filters, after, limit + 1)
            cues = repository.cues(run.subtitle_version_id) if rows and run.subtitle_version_id else []
            if run.japanese_subtitle_version_id or run.strategy in {"single_subtitle", "skipped"}:
                cues = [cue.model_copy(update={"id": f"zh:{cue.id}"}) for cue in cues]
            if rows and run.japanese_subtitle_version_id:
                cues.extend(cue.model_copy(update={"id": f"ja:{cue.id}"})
                            for cue in repository.cues(run.japanese_subtitle_version_id))

        cues.sort(key=lambda cue: (cue.start_ms, cue.order))
        cues_by_id = {cue.id: cue for cue in cues}
        starts = [cue.start_ms for cue in cues]
        # Prefix maximum end times retain long cues that overlap later dialogue.
        max_ends: list[int] = []
        for cue in cues:
            max_ends.append(max(cue.end_ms, max_ends[-1] if max_ends else 0))

        items = []
        for item, classification, selected, manual, offset_ms, text_edited in rows[:limit]:
            low = item.time_ms - CUE_WINDOW_MS
            left = bisect_right(max_ends, low - 1)
            right = bisect_right(starts, item.time_ms + CUE_WINDOW_MS)
            nearby = [cue for cue in cues[left:right] if cue.end_ms >= low]
            nearby.sort(key=lambda cue: (max(cue.start_ms - item.time_ms, item.time_ms - cue.end_ms, 0),
                                         cue.start_ms, cue.order))
            evidence = [cues_by_id[cue_id] for cue_id in classification.cue_ids if cue_id in cues_by_id
                        ] if classification else []
            items.append(ReviewRow(
                item_id=item.source_key, time_ms=item.time_ms, offset_ms=offset_ms, text=item.text, color_rgb=item.color_rgb,
                text_edited=text_edited,
                font_size=item.font_size, mode=item.mode, checkbox_type=item.checkbox_type,
                classification=classification,
                nearby_cues=nearby[:NEARBY_LIMIT], evidence_cues=evidence, selected=selected,
                selection_source="manual" if manual else "suggestion",
            ))

        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor(scope, dumps([last.time_ms, last.item_id]))
        return ReviewPage(items=items, next_cursor=next_cursor, revision=revision, selected_count=selected_count)

    def apply(self, actor: Principal, project_id: str, change: ReviewChange) -> ReviewCommit:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            run = review_run(repository, project_id, change.run_id)
            revision = require_review_revision(repository, project_id, change.expected_revision)
            ids = set(change.item_ids)
            choices = repository.review_choices(run, ids)
            if choices.keys() != ids:
                raise WorkbenchError(404, "not_found", "审核条目不存在")
            changed = sorted(item_id for item_id in ids if choices[item_id] is not change.selected)
            if changed:
                revision += 1
                repository.append_review(run, revision, changed, change.selected, actor.id, datetime.now(UTC))
            return ReviewCommit(revision=revision, changed_count=len(changed),
                                selected_count=repository.review_selected_count(run))

    def set_offset(self, actor: Principal, project_id: str, command: ReviewOffset) -> ReviewCommit:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            run = review_run(repository, project_id, command.run_id)
            revision = require_review_revision(repository, project_id, command.expected_revision)
            count = repository.save_review_offset(
                run, revision + 1, command.item_id, command.offset_ms, command.apply_to_following,
            )
            return ReviewCommit(revision=revision + bool(count), changed_count=count,
                                selected_count=repository.review_selected_count(run))

    def set_text(self, actor: Principal, project_id: str, command: ReviewText) -> ReviewCommit:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            run = review_run(repository, project_id, command.run_id)
            revision = require_review_revision(repository, project_id, command.expected_revision)
            changed = repository.save_review_text(run, revision + 1, command.item_id, command.text)
            return ReviewCommit(revision=revision + changed, changed_count=int(changed),
                                selected_count=repository.review_selected_count(run))

    def reset(self, actor: Principal, project_id: str, command: ReviewRevision) -> ReviewCommit:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            run = review_run(repository, project_id, command.run_id)
            revision = require_review_revision(repository, project_id, command.expected_revision)
            changed_count = repository.review_manual_count(run)
            if changed_count:
                revision += 1
                repository.append_review_reset(run, revision, actor.id, datetime.now(UTC))
            return ReviewCommit(revision=revision, changed_count=changed_count,
                                selected_count=repository.review_selected_count(run))
