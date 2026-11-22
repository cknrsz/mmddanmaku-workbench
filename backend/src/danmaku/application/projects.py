from json import dumps, loads
from time import time
from uuid import uuid4

from danmaku.application.ports import Repository, UnitOfWork
from danmaku.application.pagination import decode_cursor, encode_cursor, valid_creation_cursor
from danmaku.domain.inputs import ProjectInputs, ProjectParts, ReviewReady, Snapshot, SnapshotItem, SnapshotMerge, SourcePart, VideoPart, Workflow
from danmaku.domain.publication import Publication
from danmaku.domain.workbench import (
    TERMINAL_STATES, DownloadCheckpoint, Page, Principal, Project, ProjectActivity, ProjectCompletion, ProjectCreate, ProjectDetail,
    ProjectRename, ProjectTaskState, Task, TaskCreate, TaskKind, TaskProgress, TaskRecord, WorkbenchError,
)

ACTIVE_STATES = {"queued", "backoff", "running", "pausing", "cancelling"}


def require_project(repository: Repository, actor: Principal, project_id: str) -> Project:
    result = repository.project(actor.id, project_id)
    if result is None:
        raise WorkbenchError(404, "project_not_found", "找不到项目")
    return result


def require_snapshot(repository: Repository, project_id: str, snapshot_id: str) -> Snapshot:
    snapshot = repository.snapshot(project_id, snapshot_id)
    if snapshot is None:
        raise WorkbenchError(404, "snapshot_not_found", "找不到弹幕快照")
    if snapshot.state != "complete":
        raise WorkbenchError(409, "snapshot_incomplete", "弹幕快照尚未完成")
    return snapshot


def _valid_item_cursor(value: str) -> bool:
    time, key = loads(value)
    return type(time) is int and time >= 0 and isinstance(key, str) and bool(key)


def _format_duration(milliseconds: int) -> str:
    minutes, seconds = divmod((milliseconds + 999) // 1000, 60)
    return f"{minutes}分{seconds}秒"


def duration_mismatch_message(source: SourcePart, target: VideoPart) -> str | None:
    if source.duration_ms is None:
        return None
    difference_ms = abs(source.duration_ms - target.duration_ms)
    if difference_ms <= 1000:
        return None
    return (
        f"源视频分 P 时长 {_format_duration(source.duration_ms)}，目标视频分 P 时长 "
        f"{_format_duration(target.duration_ms)}，相差约 {_format_duration(difference_ms)}。"
    )


def project_activity(states: set[ProjectTaskState], review_ready: bool) -> ProjectActivity:
    """Summarize persisted tasks and the current review confirmation."""
    stages: tuple[TaskKind, ...] = ("send", "classify", "download")
    active = any(task.state in ACTIVE_STATES for task in states)
    running_stages = {
        task.kind for task in states
        if (task.send_started and not task.send_complete and not task.send_needs_review and task.state not in TERMINAL_STATES)
        or (task.kind != "send" and task.state in {"running", "pausing"})
    }
    waiting_stages = {task.kind for task in states if task.state in {"queued", "backoff", "paused"}}
    for relevant_stages, running in ((running_stages, True), (waiting_stages, False)):
        for stage in stages:
            if stage in relevant_stages:
                return ProjectActivity(stage=stage, running=running, active=active)

    latest_delivery_step = max(
        (task for task in states if task.kind == "send" or (task.kind == "classify" and task.state == "succeeded")),
        key=lambda task: task.creation_order, default=None,
    )
    if review_ready or (latest_delivery_step and latest_delivery_step.kind == "send"):
        return ProjectActivity(stage="send", active=active)
    if latest_delivery_step:
        return ProjectActivity(stage="review", active=active)
    if any(task.kind == "classify" or (task.kind == "download" and task.state == "succeeded") for task in states):
        return ProjectActivity(stage="classify", active=active)
    return ProjectActivity(active=active)


def with_project_status(repository: Repository, projects: list[Project]) -> list[Project]:
    if not projects:
        return []
    ids = [project.id for project in projects]
    overrides = repository.completion_overrides(ids)
    states_by_project = repository.project_task_states(ids)
    ready_projects = repository.projects_with_current_review(ids)
    queue_by_project = repository.project_send_queue(ids)
    publications = repository.project_publications(ids)
    queued_publications = repository.projects_waiting_for_first_send(ids)
    now = time()
    results = []
    for project in projects:
        states = states_by_project.get(project.id, set())
        live_tasks = [task for task in states if task.live and task.state != "cancelled"]
        needs_review = any(task.send_needs_review for task in states)
        publication = publications.get(project.id)
        publication_status = "blocked" if project.id in queued_publications else None
        if publication is not None and publication.task_id is not None:
            publication_status = (
                "checking" if publication.checking and not publication.overdue else
                "waiting" if publication.publish_at > now or publication.time_checked and not publication.overdue
                else "blocked"
            )
        results.append(project.model_copy(update={
            "completed": overrides.get(
                project.id, bool(live_tasks) and not needs_review and all(task.send_complete for task in live_tasks),
            ),
            "send_needs_review": needs_review,
            "send_partial_failure": any(task.send_partial_failure for task in live_tasks),
            "activity": project_activity(states, project.id in ready_projects),
            "send_queue": queue_by_project.get(project.id),
            "publication_status": publication_status,
            "preload_publish_at": publication.publish_at if publication is not None else None,
        }))
    return results


class Projects:
    def __init__(self, database: UnitOfWork):
        self.database = database

    def create(
        self, actor: Principal, command: ProjectCreate, parts: ProjectParts | None = None,
        *, confirm_duration_mismatch: bool = False, target_part_count: int | None = None,
        preload: tuple[str, int] | None = None,
    ) -> Project:
        if parts is not None and parts.source is not None and not confirm_duration_mismatch:
            message = duration_mismatch_message(parts.source, parts.target)
            if message is not None:
                raise WorkbenchError(409, "project_duration_mismatch", message)
        name = command.name
        if not name:
            video_title = command.target_bvid
            part_title = f"P{command.target_page}"
            if parts is not None:
                video_title = (parts.target.video_title or "").strip() or video_title
                part_title = parts.target.title.strip() or part_title
            name = video_title if target_part_count == 1 else f"{video_title} - {part_title}"
        project = Project(id=uuid4().hex, name=name, **command.model_dump(exclude={"name"}))
        with self.database(write=True) as repository:
            if preload is not None:
                account_id, publish_at = preload
                account = repository.bilibili_account(actor.id)
                if account is None or account.account_id != account_id:
                    raise WorkbenchError(409, "bilibili_account_changed", "主账号已变更，请重新读取稿件")
                if (command.target_bvid, command.target_page) in repository.existing_target_parts(
                    actor.id, {command.target_bvid},
                ):
                    raise WorkbenchError(409, "preload_project_exists", "项目已创建")
            project = repository.add_project(actor.id, project)
            if parts:
                repository.save_video_parts(project.id, parts)
            if preload is not None:
                repository.save_publication(Publication(project.id, account_id, publish_at))
        return project.model_copy(update={"preload_publish_at": publish_at}) if preload is not None else project

    def require(self, actor: Principal, project_id: str) -> None:
        with self.database() as repository:
            require_project(repository, actor, project_id)

    def delete(self, actor: Principal, project_id: str) -> None:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            if repository.has_active_project_tasks(project_id):
                raise WorkbenchError(409, "project_tasks_active", "请先暂停项目中的活动任务，或等待任务结束")
            repository.delete_project(project_id)

    def inputs(self, actor: Principal, project_id: str) -> ProjectInputs:
        with self.database() as repository:
            require_project(repository, actor, project_id)
            workflow = repository.workflow(project_id)
            if workflow and workflow.review and workflow.review.revision != repository.review_revision(project_id):
                workflow.review = None
            return ProjectInputs(
                parts=repository.video_parts(project_id),
                snapshots=repository.snapshots(project_id),
                subtitles=repository.subtitles(project_id),
                send_in_queue=repository.has_project_send_in_queue(project_id),
                workflow=workflow,
            )

    def snapshot_items(
        self, actor: Principal, project_id: str, snapshot_id: str, cursor: str | None, limit: int,
    ) -> Page[SnapshotItem]:
        scope = ["snapshot-items", project_id, snapshot_id]
        after = decode_cursor(scope, cursor, valid_id=_valid_item_cursor)
        with self.database() as repository:
            require_project(repository, actor, project_id)
            require_snapshot(repository, project_id, snapshot_id)
            items = repository.snapshot_items(snapshot_id, tuple(loads(after)) if after else None, limit + 1)
        shown = items[:limit]
        next_cursor = encode_cursor(scope, dumps([shown[-1].time_ms, shown[-1].id])) if len(items) > limit else None
        return Page(items=shown, next_cursor=next_cursor)

    def merge_snapshots(self, actor: Principal, project_id: str, command: SnapshotMerge) -> Snapshot:
        with self.database(write=True) as repository:
            project = require_project(repository, actor, project_id)
            snapshots = repository.snapshots(project_id)
            sources = sorted(
                (snapshot for snapshot in snapshots if snapshot.state == "complete" and snapshot.range_kind != "merged"),
                key=lambda snapshot: snapshot.sequence or 0,
            )
            members = [snapshot.id for snapshot in sources]
            if len(command.snapshot_ids) != len(members) or set(command.snapshot_ids) != set(members):
                raise WorkbenchError(409, "snapshot_set_changed", "可合并的原始快照已更新，请刷新后重试")
            for snapshot in snapshots:
                if snapshot.members == members:
                    return snapshot
            known = [source for source in sources if source.part.duration_ms is not None]
            unknown = [source for source in sources if source.part.duration_ms is None]
            shortest = min(known, key=lambda snapshot: snapshot.part.duration_ms, default=None)
            longest = max(known, key=lambda snapshot: snapshot.part.duration_ms, default=None)
            difference = longest.part.duration_ms - shortest.part.duration_ms if known else 0
            if difference > 1000 and not command.confirm_duration_mismatch:
                raise WorkbenchError(409, "snapshot_duration_mismatch", (
                    f"「{shortest.name}」时长 {_format_duration(shortest.part.duration_ms)}，"
                    f"「{longest.name}」时长 {_format_duration(longest.part.duration_ms)}，"
                    f"相差约 {_format_duration(difference)}。"
                ))
            task_id = uuid4().hex
            count = sum(snapshot.item_count for snapshot in sources)
            mode = "bilibili" if any(snapshot.mode == "bilibili" for snapshot in sources) else "mock"
            representative = max(unknown, key=lambda snapshot: snapshot.covered_until_ms) if unknown else longest
            coverage = max(source.covered_until_ms for source in sources)
            snapshot = Snapshot(
                id=task_id, mode=mode, part=representative.part, range_kind="merged", state="complete",
                merged_name="合并" + "+".join(f"{source.sequence:02d}" for source in sources),
                members=members, segment_count=0, item_count=count, covered_until_ms=coverage,
            )
            repository.save_task(TaskRecord(
                task=Task(id=task_id, project_id=project_id, creation_order=0, kind="download", state="succeeded",
                          version=1, progress=TaskProgress(done=count, total=count), checkpoint=DownloadCheckpoint()),
                inputs=TaskCreate(kind="download", mode=mode), created_by=actor.id, idempotency_key=f"merge:{task_id}",
                source_bvid=longest.part.bvid if longest else project.target_bvid,
                source_page=longest.part.page if longest else project.target_page,
            ))
            repository.save_snapshot(snapshot)
            for source in sources:
                repository.copy_snapshot_items(snapshot.id, source.id)
            if mode == "bilibili":
                repository.add_repost_notice(snapshot.id)
            return snapshot

    def select_snapshot(self, actor: Principal, project_id: str, snapshot_id: str) -> Workflow:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            require_snapshot(repository, project_id, snapshot_id)
            workflow = repository.workflow(project_id) or Workflow()
            if workflow.snapshot_id != snapshot_id:
                workflow = workflow.model_copy(update={"snapshot_id": snapshot_id, "analysis_id": None, "review": None})
                repository.save_workflow(project_id, workflow)
            return workflow

    def mark_review_ready(self, actor: Principal, project_id: str, ready: ReviewReady) -> Workflow:
        with self.database(write=True) as repository:
            require_project(repository, actor, project_id)
            run = repository.run(project_id, ready.run_id)
            if run is None or run.state != "succeeded":
                raise WorkbenchError(409, "analysis_required", "请先完成分析")
            if ready.revision != repository.review_revision(project_id):
                raise WorkbenchError(409, "revision_conflict", "审核版本已更新，请重新核对")
            workflow = (repository.workflow(project_id) or Workflow()).model_copy(update={"review": ready})
            repository.save_workflow(project_id, workflow)
            return workflow

    def get(self, actor: Principal, project_id: str) -> ProjectDetail:
        with self.database() as repository:
            (project,) = with_project_status(repository, [require_project(repository, actor, project_id)])
            return ProjectDetail(
                **project.model_dump(),
                snapshot_ids=[snapshot.id for snapshot in repository.snapshots(project_id)],
                subtitle_version_ids=[version.subtitle_version_id for version in repository.subtitles(project_id)],
                run_ids=repository.run_ids(project_id),
            )

    def rename(self, actor: Principal, project_id: str, command: ProjectRename) -> Project:
        with self.database(write=True) as repository:
            project = require_project(repository, actor, project_id)
            repository.rename_project(project_id, command.name)
            (updated,) = with_project_status(repository, [project.model_copy(update={"name": command.name})])
        return updated

    def set_completion(self, actor: Principal, project_id: str, command: ProjectCompletion) -> Project:
        with self.database(write=True) as repository:
            project = require_project(repository, actor, project_id)
            if command.completed and repository.has_active_project_tasks(project_id):
                raise WorkbenchError(409, "project_tasks_active", "请等待当前任务结束后再标记为完成")
            repository.set_completion_override(project_id, command.completed)
            (updated,) = with_project_status(repository, [project])
        return updated

    def list(self, actor: Principal, cursor: str | None, limit: int) -> Page[Project]:
        scope = ["projects-by-creation", actor.id]
        after = decode_cursor(scope, cursor, valid_id=valid_creation_cursor)
        before = int(after) if after else None
        with self.database() as repository:
            projects = with_project_status(repository, repository.projects(actor.id, before, limit + 1))
        next_cursor = encode_cursor(scope, str(projects[limit - 1].creation_order)) if len(projects) > limit else None
        return Page(items=projects[:limit], next_cursor=next_cursor)

