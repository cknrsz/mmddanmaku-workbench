from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from sqlite3 import SQLITE_BUSY, SQLITE_LOCKED, connect
from tempfile import TemporaryDirectory

from sqlalchemy import URL, bindparam, create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from danmaku.domain.workbench import ErrorInfo, TERMINAL_STATES, WorkbenchError
from danmaku.persistence.send_repository import SendRepository, priority_resend_index
from danmaku.persistence.repository import Base

SCHEMA_VERSION = 28


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.engine = create_engine(
            URL.create("sqlite", database=str(path)),
            hide_parameters=True,
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=250")

        @event.listens_for(self.engine, "begin")
        def begin(connection):
            statement = "BEGIN IMMEDIATE" if connection.get_execution_options().get("write") else "BEGIN"
            connection.exec_driver_sql(statement)

    def initialize(self) -> None:
        raw = self.engine.raw_connection()
        try:
            mode = raw.driver_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise RuntimeError("SQLite WAL initialization failed")
        finally:
            raw.close()

        with self._schema_transaction() as connection:
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
            ))
            version = connection.scalar(text("SELECT version FROM schema_version WHERE id=1"))
            if version is None:
                Base.metadata.create_all(connection)
                connection.execute(text("INSERT INTO schema_version VALUES (1, :version)"),
                                   {"version": SCHEMA_VERSION})
            elif 1 <= version < SCHEMA_VERSION:
                self._backup_before_upgrade(version)
                Base.metadata.create_all(connection)
                if version == 6:
                    connection.exec_driver_sql(
                        "ALTER TABLE live_send_cooldowns ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.exec_driver_sql("ALTER TABLE live_send_cooldowns ADD COLUMN reason VARCHAR")
                    connection.exec_driver_sql("UPDATE live_send_cooldowns SET reason = 'rate_limit'")
                if version < 9:
                    with Session(connection) as session, session.begin():
                        SendRepository(session).add_repost_notice()
                if version < 10:
                    connection.exec_driver_sql(
                        'INSERT OR IGNORE INTO send_queue (task_id) SELECT id FROM tasks '
                        "WHERE kind = 'send' AND state IN ('queued', 'running', 'pausing', 'backoff', 'paused') "
                        'ORDER BY "order"'
                    )
                if version < 12:
                    connection.exec_driver_sql(
                        "INSERT OR IGNORE INTO review_revisions (project_id, revision) "
                        "SELECT project_id, MAX(revision) FROM review_decisions GROUP BY project_id"
                    )
                if version < 15:
                    connection.exec_driver_sql(
                        'WITH numbered AS (SELECT s.id, ROW_NUMBER() OVER (PARTITION BY t.project_id '
                        'ORDER BY t."order") AS sequence FROM source_snapshots s JOIN tasks t ON s.id = t.id '
                        "WHERE json_extract(s.summary, '$.state') = 'complete' "
                        "AND COALESCE(json_extract(s.summary, '$.range_kind'), '') <> 'merged') "
                        "UPDATE source_snapshots SET summary = json_set(summary, '$.sequence', "
                        "(SELECT sequence FROM numbered WHERE numbered.id = source_snapshots.id)) "
                        "WHERE json_extract(summary, '$.sequence') IS NULL AND id IN (SELECT id FROM numbered)"
                    )
                    connection.exec_driver_sql(
                        'WITH latest AS (SELECT r.project_id, r.id, r.snapshot_id, r.inputs, ROW_NUMBER() OVER ('
                        'PARTITION BY r.project_id ORDER BY t."order" DESC) AS rank '
                        'FROM analysis_runs r JOIN tasks t ON t.id = r.id) '
                        "INSERT OR IGNORE INTO project_workflows (project_id, data) "
                        "SELECT project_id, json_object('snapshot_id', snapshot_id, 'analysis_id', id, "
                        "'chinese_subtitle_id', json_extract(inputs, '$.subtitle_version_id'), "
                        "'japanese_subtitle_id', json_extract(inputs, '$.japanese_subtitle_version_id')) "
                        "FROM latest WHERE rank = 1"
                    )
                if version < 16:
                    connection.exec_driver_sql(
                        "INSERT OR IGNORE INTO project_creation_order (project_id) SELECT id FROM projects ORDER BY rowid"
                    )
                if version < 17:
                    connection.exec_driver_sql(
                        "UPDATE tasks SET checkpoint = json_set(checkpoint, '$.started', json('true')) "
                        "WHERE kind = 'send' AND json_extract(inputs, '$.mode') = 'live' "
                        "AND (state IN ('running', 'pausing', 'backoff', 'succeeded') OR EXISTS ("
                        "SELECT 1 FROM task_events WHERE task_id = tasks.id "
                        "AND json_extract(snapshot, '$.state') = 'running'))"
                    )
                    connection.exec_driver_sql(
                        "WITH starts AS (SELECT e.task_id, MIN(e.seq) AS seq FROM task_events e "
                        "JOIN tasks t ON t.id = e.task_id "
                        "WHERE t.kind = 'send' AND json_extract(t.checkpoint, '$.started') = 1 "
                        "AND json_extract(e.snapshot, '$.state') = 'running' GROUP BY e.task_id) "
                        "UPDATE task_events SET snapshot = json_set(snapshot, '$.checkpoint.started', json('true')) "
                        "FROM starts WHERE task_events.task_id = starts.task_id AND task_events.seq >= starts.seq"
                    )
                if version < 20:
                    connection.exec_driver_sql("DROP TABLE IF EXISTS send_queue_wait")
                    connection.exec_driver_sql(
                        "UPDATE send_timing SET data = json_remove(data, '$.task_interval_seconds') "
                        "WHERE json_type(data, '$.task_interval_seconds') IS NOT NULL"
                    )
                    connection.exec_driver_sql(
                        "UPDATE send_timing SET data = json_remove(data, '$.rate_limit_seconds') "
                        "WHERE json_extract(data, '$.rate_limit_seconds') = 30"
                    )
                if version < 19:
                    self._migrate_confirmed_send_keys(connection)
                if version < 20:
                    self._allow_subtitle_free_runs(connection)
                if version < 24:
                    self._reclassify_unsupported_deliveries(connection)
                if version < 28 and not any(
                    column["name"] == "time_checked" for column in inspect(connection).get_columns("publications")
                ):
                    connection.exec_driver_sql(
                        "ALTER TABLE publications ADD COLUMN time_checked BOOLEAN NOT NULL DEFAULT 0"
                    )
                connection.execute(text("UPDATE schema_version SET version = :version WHERE id=1"),
                                   {"version": SCHEMA_VERSION})
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported database schema version: {version}")
            priority_resend_index.create(connection, checkfirst=True)

    @contextmanager
    def _schema_transaction(self) -> Iterator[Connection]:
        with self.engine.connect().execution_options(write=True) as connection:
            driver = connection.connection.driver_connection
            # Rebuilding analysis_runs must preserve foreign keys in its dependent tables.
            driver.execute("PRAGMA foreign_keys=OFF")
            try:
                with connection.begin():
                    yield connection
            finally:
                driver.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _allow_subtitle_free_runs(connection: Connection) -> None:
        connection.exec_driver_sql("""
            CREATE TABLE analysis_runs_new (
                id VARCHAR NOT NULL PRIMARY KEY REFERENCES tasks (id),
                project_id VARCHAR NOT NULL REFERENCES projects (id),
                snapshot_id VARCHAR NOT NULL REFERENCES source_snapshots (id),
                subtitle_version_id VARCHAR REFERENCES subtitle_versions (id),
                inputs JSON NOT NULL,
                UNIQUE (id, snapshot_id)
            )
        """)
        connection.exec_driver_sql("""
            INSERT INTO analysis_runs_new (id, project_id, snapshot_id, subtitle_version_id, inputs)
            SELECT id, project_id, snapshot_id, subtitle_version_id, inputs FROM analysis_runs
        """)
        connection.exec_driver_sql("DROP TABLE analysis_runs")
        connection.exec_driver_sql("ALTER TABLE analysis_runs_new RENAME TO analysis_runs")
        connection.exec_driver_sql("CREATE INDEX ix_analysis_project ON analysis_runs (project_id, id)")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("Foreign key check failed after migrating analysis runs")

    @staticmethod
    def _migrate_confirmed_send_keys(connection: Connection) -> None:
        connection.exec_driver_sql("DELETE FROM live_send_keys")
        connection.exec_driver_sql("""
            INSERT OR IGNORE INTO live_send_keys (project_id, target_cid, source_key)
            SELECT t.project_id, json_extract(b.data, '$.target.cid'),
                   COALESCE(json_extract(d.data, '$.item.delivery_key'), json_extract(d.data, '$.item.item_id'))
            FROM send_deliveries d JOIN send_batches b ON b.id = d.batch_id JOIN tasks t ON t.id = b.id
            WHERE d.status = 'confirmed' AND json_extract(b.data, '$.mode') = 'live'
        """)
        restored = connection.execute(text("""
            UPDATE send_deliveries AS d
            SET status = 'pending', data = json_set(d.data, '$.status', 'pending', '$.error_code', NULL)
            WHERE d.status = 'skipped' AND json_extract(d.data, '$.error_code') = 'duplicate_live_delivery'
              AND EXISTS (
                  SELECT 1 FROM tasks t JOIN send_batches b ON b.id = t.id
                  WHERE t.id = d.batch_id AND t.state NOT IN :terminal
                    AND json_extract(b.data, '$.mode') = 'live'
                    AND NOT EXISTS (
                        SELECT 1 FROM live_send_keys k WHERE k.project_id = t.project_id
                          AND k.target_cid = json_extract(b.data, '$.target.cid')
                          AND k.source_key = COALESCE(json_extract(d.data, '$.item.delivery_key'),
                                                     json_extract(d.data, '$.item.item_id'))
                    )
              )
            RETURNING batch_id
        """).bindparams(bindparam("terminal", expanding=True)), {"terminal": tuple(TERMINAL_STATES)})
        counts = Counter(restored.scalars())
        with Session(connection) as session, session.begin():
            repository = SendRepository(session)
            for task_id, count in counts.items():
                record = repository.task(task_id)
                assert record is not None
                task = record.task
                progress = task.progress.model_copy(update={
                    "done": task.progress.done - count, "skipped": task.progress.skipped - count,
                })
                error = None if task.error and task.error.code == "send_failed_review" else task.error
                record.task = task.model_copy(update={"version": task.version + 1, "progress": progress, "error": error})
                repository.save_task(record)

    @staticmethod
    def _reclassify_unsupported_deliveries(connection: Connection) -> None:
        reclassified = connection.exec_driver_sql("""
            UPDATE send_deliveries AS d
            SET status = 'failed', data = json_set(d.data, '$.status', 'failed')
            WHERE d.status = 'skipped'
              AND json_extract(d.data, '$.error_code') IN ('unsupported_effect', 'unstable_source')
              AND EXISTS (SELECT 1 FROM send_batches b WHERE b.id = d.batch_id
                          AND json_extract(b.data, '$.mode') = 'live')
            RETURNING batch_id
        """)
        counts = Counter(reclassified.scalars())
        with Session(connection) as session, session.begin():
            repository = SendRepository(session)
            for task_id, count in counts.items():
                record = repository.task(task_id)
                assert record is not None
                task = record.task
                progress = task.progress.model_copy(update={
                    "failed": task.progress.failed + count, "skipped": task.progress.skipped - count,
                })
                changes = {"version": task.version + 1, "progress": progress}
                if task.state == "succeeded":
                    changes.update(state="failed", error=ErrorInfo(code="send_partial", message="部分投递失败"))
                record.task = task.model_copy(update=changes)
                repository.save_task(record)

    def _backup_before_upgrade(self, version: int) -> None:
        backup = self.path.with_name(f"{self.path.name}.v{version}.bak")
        if backup.exists():
            return
        with TemporaryDirectory(dir=self.path.parent) as directory:
            temporary = Path(directory) / backup.name
            with closing(connect(self.path)) as source, closing(connect(temporary)) as target:
                source.backup(target)
            temporary.rename(backup)

    @contextmanager
    def __call__(self, *, write: bool = False) -> Iterator[SendRepository]:
        try:
            with self.engine.connect().execution_options(write=write) as connection:
                with Session(connection) as session, session.begin():
                    yield SendRepository(session)
        except OperationalError as exc:
            code = getattr(exc.orig, "sqlite_errorcode", 0) or 0
            if code & 0xff in {SQLITE_BUSY, SQLITE_LOCKED}:
                raise WorkbenchError(503, "database_busy", "数据库繁忙，请稍后重试", True) from None
            raise

    def close(self) -> None:
        self.engine.dispose()

