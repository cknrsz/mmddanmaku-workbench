from danmaku.application.ports import Repository
from danmaku.domain.workbench import Principal, Task, TaskRecord, WorkbenchError


def require_task(repository: Repository, actor: Principal, task_id: str) -> TaskRecord:
    record = repository.task(task_id, actor.id)
    if record is None:
        raise WorkbenchError(404, "task_not_found", "找不到任务")
    return record


def advance(repository: Repository, record: TaskRecord, **changes) -> Task:
    record.task = record.task.model_copy(update={"version": record.task.version + 1, **changes})
    repository.save_task(record)
    return record.task
