import sqlite3
import threading
import time

import pytest

from parsing_core.models.dataclasses import AIArtifact, Section, Task
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


class PausingConnection(sqlite3.Connection):
    pause_note_delete = False
    note_deleted: threading.Event
    resume_topic_write: threading.Event

    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        if self.pause_note_delete and sql.lstrip().startswith("DELETE FROM wb_topic_note_blocks"):
            self.note_deleted.set()
            assert self.resume_topic_write.wait(timeout=2)
        return cursor


class ReleaseFailingConnection(sqlite3.Connection):
    fail_next_release = False

    def execute(self, sql, parameters=()):
        if self.fail_next_release and sql.startswith("RELEASE SAVEPOINT"):
            self.fail_next_release = False
            raise sqlite3.OperationalError("forced savepoint release failure")
        return super().execute(sql, parameters)


class BeginFailingConnection(sqlite3.Connection):
    fail_begin_sql: str | None = None
    rollback_calls = 0

    def execute(self, sql, parameters=()):
        if sql == self.fail_begin_sql:
            raise sqlite3.OperationalError(f"forced {sql} failure")
        return super().execute(sql, parameters)

    def rollback(self):
        self.rollback_calls += 1
        return super().rollback()


class RollbackFailingConnection(sqlite3.Connection):
    def rollback(self):
        raise sqlite3.OperationalError("forced rollback cleanup failure")


def make_task(tid="t1", sha="h1"):
    return Task(
        id=tid,
        file_path="/a/b",
        snapshot_path="/tmp/snap",
        file_sha256=sha,
        status="PENDING",
        model_tier="stub",
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )


def test_create_and_get_task(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    t = make_task()
    repo.create_task(t)
    fetched = repo.get_task("t1")
    assert fetched is not None
    assert fetched.status == "PENDING"
    conn.close()


def test_update_task_status(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.update_task_status("t1", "COMPLETED")
    assert repo.get_task("t1").status == "COMPLETED"
    conn.close()


def test_create_and_list_sections(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x/0.raw.md",
            sha256="a",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    repo.create_section(
        Section(
            id="s2",
            task_id="t1",
            seq=1,
            raw_md_path="/x/1.raw.md",
            sha256="b",
            char_count=20,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    sections = repo.list_sections("t1")
    assert len(sections) == 2
    assert sections[0].seq == 0
    conn.close()


def test_update_section_ai_status(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x/0.raw.md",
            sha256="a",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    repo.update_section_ai_status("s1", "COMPLETED")
    assert repo.get_section("s1").ai_status == "COMPLETED"
    conn.close()


def test_create_and_get_artifact(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x/0.raw.md",
            sha256="a",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    repo.create_artifact(
        AIArtifact(
            id="a1",
            section_id="s1",
            ai_md_path="/x/0.ai.md",
            ai_md="",
            tokens_in=5,
            tokens_out=3,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )
    )
    a = repo.get_artifact_by_section("s1")
    assert a is not None
    assert a.ai_md_path == "/x/0.ai.md"
    assert a.ai_md == ""  # 重建后默认空
    conn.close()


def test_increment_retry(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x/0.raw.md",
            sha256="a",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    repo.create_artifact(
        AIArtifact(
            id="a1",
            section_id="s1",
            ai_md_path="/x/0.ai.md",
            ai_md="",
            tokens_in=5,
            tokens_out=3,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )
    )
    repo.increment_retry("a1")
    repo.increment_retry("a1")
    assert repo.get_artifact_by_section("s1").retry_count == 2
    conn.close()


def test_find_task_by_sha256_completed(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    t = make_task(sha="hashX")
    repo.create_task(t)
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x.raw.md",
            sha256="h",
            char_count=1,
            ai_status="COMPLETED",
            created_at=int(time.time()),
        )
    )
    repo.update_task_status("t1", "COMPLETED")
    found = repo.find_completed_task_by_file_sha256("hashX")
    assert found is not None
    assert found.id == "t1"
    conn.close()


def test_find_section_by_sha256_completed(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task(sha="F"))
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x.raw.md",
            sha256="SECX",
            char_count=1,
            ai_status="COMPLETED",
            created_at=int(time.time()),
        )
    )
    repo.create_artifact(
        AIArtifact(
            id="a1",
            section_id="s1",
            ai_md_path="/y.ai.md",
            ai_md="",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )
    )
    hit = repo.find_completed_artifact_by_section_sha256("SECX")
    assert hit is not None
    assert hit.ai_md_path == "/y.ai.md"
    conn.close()


def test_list_tasks_by_status(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task(tid="t1", sha="h1"))  # PENDING
    t2 = make_task(tid="t2", sha="h2")
    t2.status = "COMPLETED"
    repo.create_task(t2)
    pending = repo.list_tasks_by_status("PENDING")
    assert len(pending) == 1
    assert pending[0].id == "t1"
    completed = repo.list_tasks_by_status("COMPLETED")
    assert len(completed) == 1
    assert completed[0].id == "t2"
    conn.close()


def test_list_all_tasks_orders_desc_by_created(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    t1 = make_task(tid="t1", sha="h1")
    t1.created_at = 1000
    repo.create_task(t1)  # 早期
    t2 = make_task(tid="t2", sha="h2")
    t2.created_at = 2000
    repo.create_task(t2)  # 较晚
    all_tasks = repo.list_all_tasks()
    # DESC 排序：最新的在前
    assert all_tasks[0].id == "t2"
    assert all_tasks[1].id == "t1"
    conn.close()


def test_delete_task_cascades(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x.raw.md",
            sha256="a",
            char_count=1,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    repo.create_artifact(
        AIArtifact(
            id="a1",
            section_id="s1",
            ai_md_path="/y.ai.md",
            ai_md="",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            retry_count=0,
            model_name="stub",
            created_at=int(time.time()),
        )
    )
    repo.delete_task("t1")
    assert repo.get_task("t1") is None
    assert repo.get_section("s1") is None  # CASCADE
    assert repo.get_artifact_by_section("s1") is None  # CASCADE
    conn.close()


def test_update_task_status_with_error_msg(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.update_task_status("t1", "FAILED", error_msg="boom")
    t = repo.get_task("t1")
    assert t.status == "FAILED"
    assert t.error_msg == "boom"
    conn.close()


def test_get_task_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    assert repo.get_task("nope") is None
    conn.close()


def test_get_section_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    assert repo.get_section("nope") is None
    conn.close()


def test_get_artifact_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_section(
        Section(
            id="s1",
            task_id="t1",
            seq=0,
            raw_md_path="/x.raw.md",
            sha256="a",
            char_count=1,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
    )
    assert repo.get_artifact_by_section("s1") is None  # 无 artifact
    assert repo.get_artifact_by_section("absent") is None
    conn.close()


def test_find_task_by_sha256_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    assert repo.find_completed_task_by_file_sha256("absent") is None
    conn.close()


def test_find_artifact_by_sha256_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    assert repo.find_completed_artifact_by_section_sha256("absent") is None
    conn.close()


def test_storage_repository_waits_for_workbench_transaction_and_commits_after_rollback(tmp_path):
    db_path = tmp_path / "shared.db"
    seed = init_db(str(db_path))
    apply_workbench_schema(seed)
    seed.close()
    conn = sqlite3.connect(db_path, check_same_thread=False, factory=PausingConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    workbench = WorkbenchRepository(conn)
    storage = Repository(conn)
    assert workbench._connection_lock is storage._connection_lock
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    topic = workbench.create_topic(course.id, 0, "竞争优势", "")
    original = workbench.replace_topic_note_blocks(topic.id, {"old": "旧内容"})
    conn.execute(
        """
        CREATE TRIGGER fail_workbench_note_insert
        BEFORE INSERT ON wb_topic_note_blocks
        WHEN NEW.kind = 'thread-a'
        BEGIN
          SELECT RAISE(ABORT, 'workbench rollback');
        END
        """
    )
    conn.commit()
    conn.note_deleted = threading.Event()
    conn.resume_topic_write = threading.Event()
    conn.pause_note_delete = True
    storage_started = threading.Event()
    storage_finished = threading.Event()
    workbench_errors = []
    storage_errors = []

    def rollback_workbench_write():
        try:
            workbench.replace_topic_note_blocks(topic.id, {"thread-a": "A"})
        except sqlite3.IntegrityError as exc:
            workbench_errors.append(str(exc))

    def write_storage_task():
        storage_started.set()
        try:
            storage.create_task(make_task(tid="thread-b", sha="thread-b"))
        except BaseException as exc:
            storage_errors.append(exc)
        finally:
            storage_finished.set()

    thread_a = threading.Thread(target=rollback_workbench_write)
    thread_b = threading.Thread(target=write_storage_task)
    thread_a.start()
    assert conn.note_deleted.wait(timeout=2)
    thread_b.start()
    assert storage_started.wait(timeout=2)
    storage_was_blocked = not storage_finished.wait(timeout=0.1)

    conn.resume_topic_write.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert storage_was_blocked
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert workbench_errors == ["workbench rollback"]
    assert storage_errors == []
    assert workbench.list_topic_note_blocks(topic.id) == original
    assert storage.get_task("thread-b") is not None
    conn.close()


def test_storage_duplicate_failure_restores_transaction_before_workbench_write(tmp_path):
    db_path = tmp_path / "shared.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    storage = Repository(conn)
    workbench = WorkbenchRepository(conn)
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    storage.create_task(make_task())
    observer = sqlite3.connect(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        storage.create_task(make_task())

    assert conn.in_transaction is False
    topic = workbench.create_topic(course.id, 0, "竞争优势", "")
    visible = observer.execute("SELECT title FROM wb_topics WHERE id = ?", (topic.id,)).fetchone()
    assert visible[0] == "竞争优势"
    observer.close()
    conn.close()


def test_storage_write_does_not_commit_outer_transaction(tmp_path):
    db_path = tmp_path / "shared.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    storage = Repository(conn)
    workbench = WorkbenchRepository(conn)
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    observer = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    storage.create_task(make_task(tid="outer-task", sha="outer-task"))

    assert conn.in_transaction is True
    conn.rollback()
    assert observer.execute(
        "SELECT COUNT(*) FROM tasks WHERE id = 'outer-task'"
    ).fetchone()[0] == 0
    assert observer.execute(
        "SELECT description FROM wb_courses WHERE id = ?",
        (course.id,),
    ).fetchone()[0] == ""
    observer.close()
    conn.close()


def test_storage_write_failure_preserves_outer_transaction(tmp_path):
    conn = init_db(str(tmp_path / "shared.db"))
    apply_workbench_schema(conn)
    storage = Repository(conn)
    workbench = WorkbenchRepository(conn)
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    storage.create_task(make_task())
    conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        storage.create_task(make_task())

    assert conn.in_transaction is True
    assert workbench.get_course(course.id).description == "outer pending"
    conn.rollback()
    conn.close()


@pytest.mark.parametrize(
    "operation",
    [
        "update_task_status",
        "delete_task",
        "create_section",
        "update_section_ai_status",
        "create_artifact",
        "increment_retry",
        "create_batch",
        "update_batch_status",
        "increment_batch_completed",
        "finish_batch",
        "set_task_batch_id",
    ],
)
def test_storage_write_execute_failures_restore_transaction_state(tmp_path, operation):
    conn = init_db(str(tmp_path / "storage.db"))
    apply_serve_schema(conn)
    storage = Repository(conn)
    storage.create_task(make_task())
    section = Section(
        id="s1",
        task_id="t1",
        seq=0,
        raw_md_path="/x.raw.md",
        sha256="section",
        char_count=1,
        ai_status="PENDING",
        created_at=int(time.time()),
    )
    storage.create_section(section)
    artifact = AIArtifact(
        id="a1",
        section_id="s1",
        ai_md_path="/x.ai.md",
        ai_md="",
        created_at=int(time.time()),
    )
    storage.create_artifact(artifact)
    batch = {
        "id": "b1",
        "status": "PENDING",
        "concurrency": 1,
        "policy": "serial",
        "priority": 0,
        "total_tasks": 1,
        "completed_tasks": 0,
        "created_at": int(time.time()),
        "finished_at": None,
    }
    storage.create_batch(batch)
    conn.executescript(
        """
        CREATE TRIGGER fail_task_update
        BEFORE UPDATE ON tasks WHEN NEW.status = 'FAIL_TX'
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        CREATE TRIGGER fail_task_delete
        BEFORE DELETE ON tasks
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        CREATE TRIGGER fail_section_update
        BEFORE UPDATE ON sections WHEN NEW.ai_status = 'FAIL_TX'
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        CREATE TRIGGER fail_artifact_retry
        BEFORE UPDATE ON ai_artifacts WHEN NEW.retry_count > OLD.retry_count
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        CREATE TRIGGER fail_batch_update
        BEFORE UPDATE ON batches
        WHEN NEW.status = 'FAIL_TX' OR NEW.completed_tasks > OLD.completed_tasks
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        CREATE TRIGGER fail_task_batch
        BEFORE UPDATE OF batch_id ON tasks WHEN NEW.batch_id = 'fail-batch'
        BEGIN SELECT RAISE(ABORT, 'forced storage failure'); END;
        """
    )
    conn.commit()
    operations = {
        "update_task_status": lambda: storage.update_task_status("t1", "FAIL_TX"),
        "delete_task": lambda: storage.delete_task("t1"),
        "create_section": lambda: storage.create_section(section),
        "update_section_ai_status": lambda: storage.update_section_ai_status("s1", "FAIL_TX"),
        "create_artifact": lambda: storage.create_artifact(artifact),
        "increment_retry": lambda: storage.increment_retry("a1"),
        "create_batch": lambda: storage.create_batch(batch),
        "update_batch_status": lambda: storage.update_batch_status("b1", "FAIL_TX"),
        "increment_batch_completed": lambda: storage.increment_batch_completed("b1"),
        "finish_batch": lambda: storage.finish_batch("b1", "FAIL_TX"),
        "set_task_batch_id": lambda: storage.set_task_batch_id("t1", "fail-batch"),
    }

    with pytest.raises(sqlite3.IntegrityError, match="forced storage failure|UNIQUE constraint"):
        operations[operation]()

    assert conn.in_transaction is False
    conn.close()


def test_deferred_foreign_key_commit_failure_rolls_back_before_next_write(tmp_path):
    db_path = tmp_path / "storage.db"
    conn = init_db(str(db_path))
    storage = Repository(conn)
    invalid = Section(
        id="invalid-section",
        task_id="missing-task",
        seq=0,
        raw_md_path="/invalid.md",
        sha256="invalid",
        char_count=1,
        ai_status="PENDING",
        created_at=int(time.time()),
    )
    conn.execute("PRAGMA defer_foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        storage.create_section(invalid)

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM sections WHERE id = 'invalid-section'"
    ).fetchone()[0] == 0
    storage.create_task(make_task())
    valid = Section(
        id="valid-section",
        task_id="t1",
        seq=0,
        raw_md_path="/valid.md",
        sha256="valid",
        char_count=1,
        ai_status="PENDING",
        created_at=int(time.time()),
    )
    storage.create_section(valid)
    observer = sqlite3.connect(db_path)
    assert observer.execute(
        "SELECT task_id FROM sections WHERE id = 'valid-section'"
    ).fetchone()[0] == "t1"
    observer.close()
    conn.close()


def test_savepoint_release_failure_rolls_back_only_repository_write(tmp_path):
    db_path = tmp_path / "storage.db"
    seed = init_db(str(db_path))
    apply_workbench_schema(seed)
    seed.close()
    conn = sqlite3.connect(db_path, factory=ReleaseFailingConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    storage = Repository(conn)
    workbench = WorkbenchRepository(conn)
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    conn.execute(
        "UPDATE wb_courses SET description = 'outer pending' WHERE id = ?",
        (course.id,),
    )
    conn.fail_next_release = True

    with pytest.raises(sqlite3.OperationalError, match="forced savepoint release failure"):
        storage.create_task(make_task(tid="release-failed", sha="release-failed"))

    assert conn.in_transaction is True
    assert workbench.get_course(course.id).description == "outer pending"
    assert storage.get_task("release-failed") is None
    conn.rollback()
    conn.close()


def test_begin_failures_do_not_call_rollback(tmp_path):
    db_path = tmp_path / "storage.db"
    seed = init_db(str(db_path))
    apply_workbench_schema(seed)
    seed.close()
    conn = sqlite3.connect(db_path, factory=BeginFailingConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    storage = Repository(conn)
    workbench = WorkbenchRepository(conn)
    course = workbench.create_course("战略管理", "", str(tmp_path / "out"))
    topic = workbench.create_topic(course.id, 0, "竞争优势", "")

    conn.fail_begin_sql = "BEGIN"
    with pytest.raises(sqlite3.OperationalError, match="forced BEGIN failure"):
        storage.create_task(make_task(tid="begin-failed", sha="begin-failed"))
    assert conn.rollback_calls == 0
    assert conn.in_transaction is False

    conn.fail_begin_sql = "BEGIN IMMEDIATE"
    with pytest.raises(sqlite3.OperationalError, match="forced BEGIN IMMEDIATE failure"):
        workbench.replace_topic_note_blocks(topic.id, {"summary": "摘要"})
    assert conn.rollback_calls == 0
    assert conn.in_transaction is False
    conn.close()


def test_commit_error_remains_primary_when_rollback_cleanup_fails(tmp_path):
    db_path = tmp_path / "storage.db"
    seed = init_db(str(db_path))
    seed.close()
    conn = sqlite3.connect(db_path, factory=RollbackFailingConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    storage = Repository(conn)
    invalid = Section(
        id="invalid-section",
        task_id="missing-task",
        seq=0,
        raw_md_path="/invalid.md",
        sha256="invalid",
        char_count=1,
        ai_status="PENDING",
        created_at=int(time.time()),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed") as caught:
        storage.create_section(invalid)

    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert "forced rollback cleanup failure" in str(caught.value.__cause__)
    assert any("transaction cleanup failed" in note for note in caught.value.__notes__)
    conn.close()
