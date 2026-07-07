import time

from parsing_core.models.dataclasses import Task
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


def make_batch(bid="b1", status="PENDING"):
    return {
        "id": bid,
        "status": status,
        "concurrency": 4,
        "policy": "parallel",
        "priority": 0,
        "total_tasks": 2,
        "completed_tasks": 0,
        "created_at": int(time.time()),
        "finished_at": None,
    }


def test_create_and_get_batch(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1"))
    b = repo.get_batch("b1")
    assert b is not None
    assert b["status"] == "PENDING"
    assert b["total_tasks"] == 2
    conn.close()


def test_list_batches_by_status(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1", "PENDING"))
    repo.create_batch(make_batch("b2", "RUNNING"))
    pending = repo.list_batches_by_status("PENDING")
    assert len(pending) == 1
    assert pending[0]["id"] == "b1"
    conn.close()


def test_list_all_batches(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1"))
    repo.create_batch(make_batch("b2"))
    assert len(repo.list_all_batches()) == 2
    conn.close()


def test_update_batch_status(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1", "PENDING"))
    repo.update_batch_status("b1", "RUNNING")
    assert repo.get_batch("b1")["status"] == "RUNNING"
    conn.close()


def test_increment_batch_completed(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1", "RUNNING"))
    repo.increment_batch_completed("b1")
    repo.increment_batch_completed("b1")
    assert repo.get_batch("b1")["completed_tasks"] == 2
    conn.close()


def test_finish_batch(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1", "RUNNING"))
    repo.finish_batch("b1", status="COMPLETED")
    b = repo.get_batch("b1")
    assert b["status"] == "COMPLETED"
    assert b["finished_at"] is not None
    conn.close()


def test_set_task_batch_id(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_batch(make_batch("b1"))
    t = Task(
        id="t1",
        file_path="/a",
        snapshot_path="/s",
        file_sha256="h",
        status="PENDING",
        model_tier="stub",
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    repo.create_task(t)
    repo.set_task_batch_id("t1", "b1")
    fetched = repo.get_task("t1")
    assert fetched is not None
    assert fetched.batch_id == "b1"
    conn.close()


def test_get_batch_missing_returns_none(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    assert repo.get_batch("nope") is None
    conn.close()
