from datetime import UTC, datetime, timedelta
import sqlite3

from pydantic import SecretStr
import pytest
from sqlalchemy.exc import IntegrityError

from danmaku.adapters.credentials import MemoryCredentialStore
from danmaku.application.projects import Projects
from danmaku.application.tasks.service import BatchError, MAX_ATTEMPTS, Tasks
from danmaku.domain.workbench import ErrorInfo, Principal, ProjectCreate, TaskCreate, WorkbenchError
from danmaku.persistence.database import Database, SCHEMA_VERSION


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    try:
        yield db
    finally:
        db.close()


def create_task(database, tasks):
    owner = Principal(id="synthetic-owner")
    project = Projects(database).create(owner, ProjectCreate(
        name="合成项目", source_bvid="BV0000000000", source_page=1,
        target_bvid="BV0000000001", target_page=1,
    ))
    return owner, tasks.create(owner, project.id, "key", TaskCreate(kind="download"))


def test_backoff_and_paused_retry_respect_exact_deadline_and_budget(database):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tasks = Tasks(database, clock=lambda: now)
    owner, initial = create_task(database, tasks)
    failure = BatchError(
        ErrorInfo(code="synthetic_limit", message="合成限流", retryable=True),
        retry_after=timedelta(seconds=5),
    )
    record = tasks.claim()
    first_failure = tasks.settle(record, failure)
    assert first_failure.state == "backoff"
    assert first_failure.next_attempt_at == now + timedelta(seconds=5)
    paused = tasks.control(owner, initial.id, first_failure.version, "pause")
    assert paused.state == "paused"
    resumed = tasks.control(owner, initial.id, paused.version, "resume")
    assert resumed.state == "queued"
    now += timedelta(seconds=4, microseconds=999999)
    assert tasks.claim() is None
    now += timedelta(microseconds=1)
    record = tasks.claim()
    assert record.task.state == "running"
    for attempt in range(2, MAX_ATTEMPTS + 1):
        result = tasks.settle(record, failure)
        if attempt < MAX_ATTEMPTS:
            assert result.state == "backoff"
            now += timedelta(seconds=5)
            assert tasks.claim() is None  # Due backoff first commits its queued transition.
            record = tasks.claim()
    assert result.state == "failed"
    assert result.error.retryable is False
    assert result.progress.done == 0
    assert tasks.claim() is None


def test_restart_from_pausing_keeps_checkpoint_and_is_idempotent(database):
    tasks = Tasks(database)
    owner, initial = create_task(database, tasks)
    first = tasks.settle(tasks.claim(), None)
    pausing = tasks.control(owner, initial.id, first.version, "pause")
    assert pausing.state == "pausing"
    tasks.recover()
    recovered = tasks.get(owner, initial.id)
    assert recovered.state == "paused"
    assert recovered.checkpoint == first.checkpoint
    assert recovered.version == pausing.version + 1
    assert recovered.error.code == "interrupted"
    tasks.recover()
    assert tasks.get(owner, initial.id) == recovered


def test_pausing_batch_with_permanent_failure_stays_paused_until_resume(database):
    tasks = Tasks(database)
    owner, initial = create_task(database, tasks)
    claimed = tasks.claim()
    tasks.control(owner, initial.id, claimed.task.version, "pause")
    failure = BatchError(ErrorInfo(code="synthetic_permanent", message="合成批次失败"))
    paused = tasks.settle(claimed, failure)
    assert paused.state == "paused"
    assert paused.error == failure.info
    tasks.control(owner, initial.id, paused.version, "resume")
    assert tasks.claim().task.state == "running"
    assert tasks.next_batch(initial.id) is None
    failed = tasks.get(owner, initial.id)
    assert failed.state == "failed"
    assert failed.error == failure.info


def test_schema_version_wal_foreign_keys_and_busy_rollback(database):
    path = database.engine.url.database
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
        assert other.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        tasks = Tasks(database)
        owner, initial = create_task(database, tasks)
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(WorkbenchError) as busy:
            tasks.control(owner, initial.id, initial.version, "pause")
        assert busy.value.status == 503 and busy.value.info.retryable
        assert tasks.get(owner, initial.id) == initial
        other.rollback()
        paused = tasks.control(owner, initial.id, initial.version, "pause")
        assert paused.version == initial.version + 1
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        other.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="Unsupported database schema version"):
        database.initialize()


def test_failed_event_insert_rolls_back_the_task_transition(database):
    tasks = Tasks(database)
    owner, initial = create_task(database, tasks)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_synthetic_event BEFORE INSERT ON task_events "
            "WHEN NEW.seq = 2 BEGIN SELECT RAISE(ABORT, 'synthetic error'); END"
        )
    with pytest.raises(IntegrityError):
        tasks.control(owner, initial.id, initial.version, "pause")
    latest, history = tasks.replay(owner, initial.id, 0)
    assert latest == initial
    assert history == [initial]


def test_credential_store_uses_opaque_references_and_idempotent_delete():
    store = MemoryCredentialStore()
    secret = SecretStr("synthetic-credential")
    reference = store.put(secret)
    assert reference != secret.get_secret_value()
    assert store.get(reference) == secret
    assert store.put(secret) != reference
    store.delete(reference)
    store.delete(reference)
    assert store.get(reference) is None

