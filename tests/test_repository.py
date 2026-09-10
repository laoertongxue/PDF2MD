import sqlite3
import threading
import time
from pathlib import Path

import pytest

import parsing_core.storage.repository as repository_module
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


def test_create_sections_rolls_back_entire_batch_on_mid_insert_failure(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    sections = [
        Section(
            id=f"s{seq}",
            task_id="t1",
            seq=seq,
            raw_md_path=f"/x/{seq}.raw.md",
            sha256=f"sha-{seq}",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
        for seq in range(3)
    ]
    original_insert = repo._insert_section
    insert_count = 0

    def fail_second_insert(section):
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("forced section batch failure")
        original_insert(section)

    monkeypatch.setattr(repo, "_insert_section", fail_second_insert)
    with pytest.raises(RuntimeError, match="forced section batch failure"):
        repo.create_sections(sections)

    assert repo.list_sections("t1") == []
    assert conn.in_transaction is False
    conn.close()


def test_create_sections_records_complete_contiguous_checkpoint(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    sections = [
        Section(
            id=f"s{seq}",
            task_id="t1",
            seq=seq,
            raw_md_path=f"/x/{seq}.raw.md",
            sha256=f"sha-{seq}",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
        for seq in range(3)
    ]

    repo.create_sections(sections)

    recovery = repo.get_task_recovery("t1")
    assert recovery is not None
    assert recovery["sectioning_complete"] is True
    assert recovery["expected_sections"] == 3
    assert recovery["resume_owner"] is None
    assert recovery["resume_generation"] == 0
    conn.close()


@pytest.mark.parametrize("invalid_batch", ["empty", "duplicate_seq", "seq_gap", "mixed_task"])
def test_create_sections_rejects_invalid_batch_without_partial_checkpoint(
    tmp_path,
    invalid_batch,
):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    repo.create_task(make_task(tid="t2", sha="h2"))
    task_ids = ["t1", "t1"]
    seqs = [0, 1]
    if invalid_batch == "empty":
        task_ids = []
        seqs = []
    elif invalid_batch == "duplicate_seq":
        seqs = [0, 0]
    elif invalid_batch == "seq_gap":
        seqs = [0, 2]
    elif invalid_batch == "mixed_task":
        task_ids = ["t1", "t2"]
    sections = [
        Section(
            id=f"invalid-{index}",
            task_id=task_id,
            seq=seq,
            raw_md_path=f"/x/{index}.raw.md",
            sha256=f"sha-{index}",
            char_count=10,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
        for index, (task_id, seq) in enumerate(zip(task_ids, seqs, strict=True))
    ]

    with pytest.raises(ValueError):
        repo.create_sections(sections)

    assert repo.list_sections("t1") == []
    assert repo.list_sections("t2") == []
    for task_id in ("t1", "t2"):
        recovery = repo.get_task_recovery(task_id)
        assert recovery is not None
        assert recovery["sectioning_complete"] is False
        assert recovery["expected_sections"] == 0
    conn.close()


def test_resume_claim_is_exclusive_across_connections_and_generation_fenced(tmp_path):
    db_path = tmp_path / "x.db"
    first_conn = init_db(str(db_path))
    first = Repository(first_conn)
    first.create_task(make_task())
    second_conn = init_db(str(db_path))
    second = Repository(second_conn)

    first_generation = first.claim_task_resume("t1", "owner-one")
    competing_generation = second.claim_task_resume("t1", "owner-two")

    assert first_generation == 1
    assert competing_generation is None
    with pytest.raises(RuntimeError, match="resume claim"):
        second.update_task_status_fenced(
            "t1",
            "FAILED",
            "owner-two",
            1,
            error_msg="must not win",
        )
    assert second.get_task("t1").status == "PENDING"
    assert second.release_task_resume("t1", "owner-two", 1) is False
    assert first.release_task_resume("t1", "owner-one", first_generation) is True
    second_generation = second.claim_task_resume("t1", "owner-two")
    assert second_generation == 2
    with pytest.raises(RuntimeError, match="resume claim"):
        first.update_task_status_fenced(
            "t1",
            "FAILED",
            "owner-one",
            first_generation,
            error_msg="stale owner",
        )
    assert first.get_task("t1").status == "PENDING"
    assert second.release_task_resume("t1", "owner-two", second_generation) is True
    first_conn.close()
    second_conn.close()


def test_stale_resume_generation_cannot_commit_sections_or_artifact(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    original = Section(
        id="original-section",
        task_id="t1",
        seq=0,
        raw_md_path="/x/0.raw.md",
        sha256="original",
        char_count=8,
        ai_status="PENDING",
        created_at=int(time.time()),
    )
    repo.create_sections([original])
    stale_generation = repo.claim_task_resume("t1", "stale-owner")
    assert stale_generation == 1
    assert repo.release_task_resume("t1", "stale-owner", stale_generation) is True
    current_generation = repo.claim_task_resume("t1", "current-owner")
    assert current_generation == 2
    replacement = Section(
        id="replacement-section",
        task_id="t1",
        seq=0,
        raw_md_path="/x/replacement.raw.md",
        sha256="replacement",
        char_count=11,
        ai_status="PENDING",
        created_at=int(time.time()),
    )
    artifact = AIArtifact(
        id="stale-artifact",
        section_id=original.id,
        ai_md_path="/x/0.ai.md",
        ai_md="",
        created_at=int(time.time()),
    )

    with pytest.raises(RuntimeError, match="resume claim"):
        repo.replace_sections_with_checkpoint(
            "t1",
            [replacement],
            "stale-owner",
            stale_generation,
        )
    with pytest.raises(RuntimeError, match="resume claim"):
        repo.complete_section_with_artifact_fenced(
            artifact,
            "t1",
            "stale-owner",
            stale_generation,
        )

    assert repo.list_sections("t1") == [original]
    assert repo.get_artifact_by_section(original.id) is None
    assert repo.get_section(original.id).ai_status == "PENDING"
    assert repo.release_task_resume("t1", "current-owner", current_generation) is True
    conn.close()


def test_recovery_write_methods_are_registered_as_atomic():
    assert {
        "claim_task_resume",
        "release_task_resume",
        "update_task_status_fenced",
        "replace_sections_with_checkpoint",
        "complete_section_with_artifact_fenced",
    } <= set(repository_module._WRITE_METHODS)


def test_fenced_section_rebuild_is_atomic_and_preserves_claim_on_failure(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    repo.create_task(make_task())
    old_section = Section(
        id="old-section",
        task_id="t1",
        seq=0,
        raw_md_path="/x/old.raw.md",
        sha256="old",
        char_count=3,
        ai_status="COMPLETED",
        created_at=int(time.time()),
    )
    repo.create_section(old_section)
    repo.create_artifact(
        AIArtifact(
            id="old-artifact",
            section_id=old_section.id,
            ai_md_path="/x/old.ai.md",
            ai_md="",
            created_at=int(time.time()),
        )
    )
    generation = repo.claim_task_resume("t1", "owner-one")
    assert generation == 1
    replacements = [
        Section(
            id=f"new-{seq}",
            task_id="t1",
            seq=seq,
            raw_md_path=f"/x/{seq}.raw.md",
            sha256=f"new-{seq}",
            char_count=5,
            ai_status="PENDING",
            created_at=int(time.time()),
        )
        for seq in range(2)
    ]
    original_insert = repo._insert_section
    insert_count = 0

    def fail_second_insert(section):
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("forced fenced rebuild failure")
        original_insert(section)

    monkeypatch.setattr(repo, "_insert_section", fail_second_insert)
    with pytest.raises(RuntimeError, match="forced fenced rebuild failure"):
        repo.replace_sections_with_checkpoint(
            "t1",
            replacements,
            "owner-one",
            generation,
        )

    assert repo.list_sections("t1") == [old_section]
    assert repo.get_artifact_by_section(old_section.id) is not None
    recovery = repo.get_task_recovery("t1")
    assert recovery is not None
    assert recovery["resume_owner"] == "owner-one"
    assert recovery["resume_generation"] == generation
    assert recovery["sectioning_complete"] is False
    assert recovery["expected_sections"] == 0
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


def test_complete_section_with_artifact_atomically_persists_both_states(tmp_path):
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
    artifact = AIArtifact(
        id="a1",
        section_id="s1",
        ai_md_path="/x/0.ai.md",
        ai_md="",
        model_name="stub",
        created_at=int(time.time()),
    )

    repo.complete_section_with_artifact(artifact)

    assert repo.get_artifact_by_section("s1") == artifact
    assert repo.get_section("s1").ai_status == "COMPLETED"
    repo.update_section_ai_status("s1", "PENDING")
    repo.complete_section_with_artifact(artifact, artifact_already_persisted=True)
    assert repo.get_artifact_by_section("s1") == artifact
    assert repo.get_section("s1").ai_status == "COMPLETED"
    conn.close()


def test_complete_section_with_artifact_rolls_back_replacement_if_status_update_fails(tmp_path):
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
    old_artifact = AIArtifact(
        id="old-artifact",
        section_id="s1",
        ai_md_path="/x/old.ai.md",
        ai_md="",
        created_at=int(time.time()),
    )
    repo.create_artifact(old_artifact)
    conn.executescript(
        """
        CREATE TRIGGER fail_atomic_section_completion
        BEFORE UPDATE ON sections WHEN NEW.ai_status = 'COMPLETED'
        BEGIN SELECT RAISE(ABORT, 'forced section completion failure'); END;
        """
    )
    conn.commit()
    replacement = AIArtifact(
        id="replacement-artifact",
        section_id="s1",
        ai_md_path="/x/new.ai.md",
        ai_md="",
        created_at=int(time.time()),
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced section completion failure"):
        repo.complete_section_with_artifact(replacement)

    assert repo.get_artifact_by_section("s1") == old_artifact
    assert repo.get_section("s1").ai_status == "PENDING"
    assert conn.in_transaction is False
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


def test_materialize_cached_task_rolls_back_waiting_promotion_and_children(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    now = int(time.time())
    batch = {
        "id": "b1",
        "status": "RUNNING",
        "concurrency": 1,
        "policy": "serial",
        "priority": 0,
        "total_tasks": 1,
        "completed_tasks": 0,
        "created_at": now,
        "finished_at": None,
    }
    repo.create_batch(batch)
    waiting = make_task(tid="accepted", sha="")
    waiting.file_path = "/course.md"
    waiting.snapshot_path = ""
    waiting.status = "WAITING"
    waiting.batch_id = "b1"
    repo.create_task(waiting)
    completed = make_task(tid="accepted", sha="course-sha")
    completed.file_path = "/course.md"
    completed.snapshot_path = ""
    completed.status = "COMPLETED"
    completed.batch_id = "b1"
    sections = [
        Section(
            id="s1",
            task_id="accepted",
            seq=0,
            raw_md_path="/accepted/0.raw.md",
            sha256="section-1",
            char_count=10,
            ai_status="COMPLETED",
            created_at=now,
        ),
        Section(
            id="s2",
            task_id="accepted",
            seq=1,
            raw_md_path="/accepted/1.raw.md",
            sha256="section-2",
            char_count=10,
            ai_status="COMPLETED",
            created_at=now,
        ),
    ]
    artifacts = [
        AIArtifact(
            id="duplicate-artifact",
            section_id="s1",
            ai_md_path="/accepted/0.ai.md",
            created_at=now,
        ),
        AIArtifact(
            id="duplicate-artifact",
            section_id="s2",
            ai_md_path="/accepted/1.ai.md",
            created_at=now,
        ),
    ]

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        repo.materialize_cached_task(completed, sections, artifacts)

    restored = repo.get_task("accepted")
    assert restored is not None
    assert restored.status == "WAITING"
    assert restored.file_sha256 == ""
    assert repo.list_sections("accepted") == []
    conn.close()


def test_materialize_cached_task_rejects_empty_completed_task(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    now = int(time.time())
    repo.create_batch(
        {
            "id": "b1",
            "status": "RUNNING",
            "concurrency": 1,
            "policy": "serial",
            "priority": 0,
            "total_tasks": 1,
            "completed_tasks": 0,
            "created_at": now,
            "finished_at": None,
        }
    )
    waiting = make_task(tid="accepted", sha="")
    waiting.file_path = "/course.md"
    waiting.snapshot_path = ""
    waiting.status = "WAITING"
    waiting.batch_id = "b1"
    repo.create_task(waiting)
    completed = make_task(tid="accepted", sha="course-sha")
    completed.file_path = "/course.md"
    completed.snapshot_path = ""
    completed.status = "COMPLETED"
    completed.batch_id = "b1"

    with pytest.raises(ValueError, match="at least one section"):
        repo.materialize_cached_task(completed, [], [])

    restored = repo.get_task("accepted")
    assert restored is not None
    assert restored.status == "WAITING"
    assert repo.list_sections("accepted") == []
    conn.close()


def test_materialize_cached_task_does_not_revive_deleted_target(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    now = int(time.time())
    repo.create_batch(
        {
            "id": "b1",
            "status": "RUNNING",
            "concurrency": 1,
            "policy": "serial",
            "priority": 0,
            "total_tasks": 1,
            "completed_tasks": 0,
            "created_at": now,
            "finished_at": None,
        }
    )
    waiting = make_task(tid="accepted", sha="")
    waiting.file_path = "/course.md"
    waiting.snapshot_path = ""
    waiting.status = "WAITING"
    waiting.batch_id = "b1"
    repo.create_task(waiting)
    repo.delete_task("accepted")

    completed = make_task(tid="accepted", sha="course-sha")
    completed.file_path = "/course.md"
    completed.snapshot_path = ""
    completed.status = "COMPLETED"
    completed.batch_id = "b1"
    section = Section(
        id="section-1",
        task_id="accepted",
        seq=0,
        raw_md_path="/accepted/0.raw.md",
        sha256="section-sha",
        char_count=10,
        ai_status="COMPLETED",
        created_at=now,
    )
    artifact = AIArtifact(
        id="artifact-1",
        section_id=section.id,
        ai_md_path="/accepted/0.ai.md",
        created_at=now,
    )

    with pytest.raises(RuntimeError, match="no longer eligible"):
        repo.materialize_cached_task(completed, [section], [artifact])

    assert repo.get_task("accepted") is None
    assert repo.get_section(section.id) is None
    assert repo.get_artifact_by_section(section.id) is None
    conn.close()


def test_promote_preregistered_task_is_update_only_and_never_inserts_missing_target(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    promoted = make_task(tid="deleted-before-promotion", sha="course-sha")
    promoted.file_path = "/course.md"
    promoted.status = "PARSING"
    promoted.batch_id = "batch-1"

    with pytest.raises(repository_module.TaskPromotionConflict, match="no longer eligible"):
        repo.promote_preregistered_task(promoted)

    assert repo.get_task(promoted.id) is None
    conn.close()


def test_promote_preregistered_task_updates_matching_waiting_row(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    now = int(time.time())
    repo.create_batch(
        {
            "id": "batch-1",
            "status": "RUNNING",
            "concurrency": 1,
            "policy": "serial",
            "priority": 0,
            "total_tasks": 1,
            "completed_tasks": 0,
            "created_at": now,
            "finished_at": None,
        }
    )
    waiting = make_task(tid="accepted", sha="")
    waiting.file_path = "/course.md"
    waiting.snapshot_path = ""
    waiting.status = "WAITING"
    waiting.batch_id = "batch-1"
    repo.create_task(waiting)
    promoted = make_task(tid="accepted", sha="course-sha")
    promoted.file_path = waiting.file_path
    promoted.snapshot_path = "/tmp/course.snapshot"
    promoted.status = "PARSING"
    promoted.batch_id = waiting.batch_id

    repo.promote_preregistered_task(promoted)

    stored = repo.get_task("accepted")
    assert stored is not None
    assert stored.status == "PARSING"
    assert stored.file_sha256 == "course-sha"
    assert stored.snapshot_path == "/tmp/course.snapshot"
    conn.close()


def test_normal_task_promotion_conflict_is_distinct_from_cache_materialization_conflict():
    assert (
        repository_module.TaskPromotionConflict
        is not repository_module.CachedTaskMaterializationConflict
    )


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


def test_list_waiting_tasks_page_uses_stable_id_keyset_and_limit(tmp_path: Path) -> None:
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    now = int(time.time())
    for index, status in enumerate(
        ["WAITING", "PENDING", "COMPLETED", "WAITING", "FAILED", "PENDING"]
    ):
        task = make_task(tid=f"task-{index}", sha=f"sha-{index}")
        task.status = status
        task.created_at = now + index
        task.updated_at = now + index
        repo.create_task(task)

    first = repo.list_waiting_tasks_page(after_id=None, limit=2)
    second = repo.list_waiting_tasks_page(after_id=first[-1].id, limit=2)
    final = repo.list_waiting_tasks_page(after_id=second[-1].id, limit=2)

    assert [task.id for task in first] == ["task-0", "task-1"]
    assert [task.id for task in second] == ["task-3", "task-5"]
    assert final == []
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
    assert observer.execute("SELECT COUNT(*) FROM tasks WHERE id = 'outer-task'").fetchone()[0] == 0
    assert (
        observer.execute(
            "SELECT description FROM wb_courses WHERE id = ?",
            (course.id,),
        ).fetchone()[0]
        == ""
    )
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
        "create_sections",
        "update_section_ai_status",
        "create_artifact",
        "complete_section_with_artifact",
        "increment_retry",
        "create_batch",
        "create_batch_with_tasks",
        "materialize_cached_task",
        "update_batch_status",
        "increment_batch_completed",
        "finish_batch",
        "set_batch_progress",
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
    section.ai_status = "COMPLETED"
    materialized_task = make_task(tid="t1", sha="materialized")
    materialized_task.status = "COMPLETED"
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
        BEFORE UPDATE ON sections
        WHEN NEW.ai_status = 'FAIL_TX'
          OR (NEW.ai_status = 'COMPLETED' AND OLD.ai_status = 'PENDING')
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
        "create_sections": lambda: storage.create_sections([section]),
        "update_section_ai_status": lambda: storage.update_section_ai_status("s1", "FAIL_TX"),
        "create_artifact": lambda: storage.create_artifact(artifact),
        "complete_section_with_artifact": lambda: storage.complete_section_with_artifact(
            artifact,
            artifact_already_persisted=True,
        ),
        "increment_retry": lambda: storage.increment_retry("a1"),
        "create_batch": lambda: storage.create_batch(batch),
        "create_batch_with_tasks": lambda: storage.create_batch_with_tasks(batch, [make_task()]),
        "materialize_cached_task": lambda: storage.materialize_cached_task(
            materialized_task,
            [section],
            [artifact],
        ),
        "update_batch_status": lambda: storage.update_batch_status("b1", "FAIL_TX"),
        "increment_batch_completed": lambda: storage.increment_batch_completed("b1"),
        "finish_batch": lambda: storage.finish_batch("b1", "FAIL_TX"),
        "set_batch_progress": lambda: storage.set_batch_progress(
            "b1", completed=1, status="FAIL_TX"
        ),
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
    assert (
        conn.execute("SELECT COUNT(*) FROM sections WHERE id = 'invalid-section'").fetchone()[0]
        == 0
    )
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
    assert (
        observer.execute("SELECT task_id FROM sections WHERE id = 'valid-section'").fetchone()[0]
        == "t1"
    )
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

    conn.fail_begin_sql = "BEGIN IMMEDIATE"
    with pytest.raises(sqlite3.OperationalError, match="forced BEGIN IMMEDIATE failure"):
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
