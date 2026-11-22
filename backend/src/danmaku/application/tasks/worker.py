import asyncio
from collections.abc import Awaitable, Callable
import logging

from danmaku.application.sending.connection import SendConnection
from danmaku.application.tasks.service import BatchError, BatchResult, BatchWork, Tasks
from danmaku.domain.sending import SendResult, SendWork
from danmaku.domain.workbench import ErrorInfo, TaskRecord, WorkbenchError

BatchExecutor = Callable[[TaskRecord], Awaitable[BatchResult]]
WorkExecutor = Callable[[BatchWork], Awaitable[BatchResult]]
PARALLEL_DOWNLOADS = 4
logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, tasks: Tasks, execute: WorkExecutor, poll_interval: float = 0.2,
                 connection: SendConnection | None = None):
        self.tasks = tasks
        self.execute = execute
        self.poll_interval = poll_interval
        self.connection = connection if connection is not None else SendConnection()

    async def _database(self, action, *args):
        while True:
            transaction = asyncio.create_task(asyncio.to_thread(action, *args))
            try:
                return await asyncio.shield(transaction)
            except asyncio.CancelledError:
                try:
                    await transaction
                except WorkbenchError as exc:
                    if exc.info.code != "database_busy":
                        raise
                raise
            except WorkbenchError as exc:
                if exc.info.code != "database_busy":
                    raise
                await asyncio.sleep(self.poll_interval)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            for _ in range(PARALLEL_DOWNLOADS):
                group.create_task(self._run_download_queue())
            group.create_task(self._run_analysis_queue())
            group.create_task(self._run_send_queue())

    async def _run_download_queue(self) -> None:
        while True:
            record = await self._database(self.tasks.claim, False, "download")
            if record is None:
                await asyncio.sleep(self.poll_interval)
                continue
            await self._run_task(record)

    async def _run_analysis_queue(self) -> None:
        async with asyncio.TaskGroup() as group:
            while True:
                record = await self._database(self.tasks.claim, False, "classify")
                if record is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                group.create_task(self._run_task(record))

    async def _run_task(self, record: TaskRecord) -> None:
        while True:
            batch = await self._database(self.tasks.next_batch, record.task.id)
            if batch is None:
                return
            settled = await self._execute_batch(batch)
            if settled is None or settled.state != "running":
                return

    async def _run_send_queue(self) -> None:
        async with asyncio.TaskGroup() as group:
            active = {}
            while True:
                if not active:
                    await self.connection.ready.wait()
                if len(active) < 2 and self.connection.ready.is_set():
                    work = await self._database(self.tasks.next_send_work, tuple(active.values()))
                    if work is not None:
                        active[group.create_task(self._execute_batch(work))] = work
                        continue
                if not active:
                    await asyncio.sleep(self.poll_interval)
                    continue
                done, _ = await asyncio.wait(
                    active, timeout=self.poll_interval if len(active) == 1 else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for worker in done:
                    del active[worker]

    async def _execute_batch(self, batch: BatchWork):
        failure = None
        result = None
        try:
            if isinstance(batch, SendWork) and not self.connection.ready.is_set():
                result = SendResult(outcome="deferred")
            else:
                result = await self.execute(batch)
        except BatchError as exc:
            failure = exc
        except Exception:
            kind, task_id = ("send", batch.task_id) if isinstance(batch, SendWork) else (batch.task.kind, batch.task.id)
            logger.error("%s batch execution failed for task %s", kind, task_id)
            failure = BatchError(ErrorInfo(code="batch_failed", message="任务批次执行失败"))
        return await self._database(self.tasks.settle, batch, failure, result)

