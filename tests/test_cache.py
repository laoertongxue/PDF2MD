import time

from parsing_core.models.dataclasses import Task
from parsing_core.storage.cache import CacheService
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


def seed(conn):
    t = Task(
        id="t1",
        file_path="/a",
        snapshot_path="/a",
        file_sha256="FILE1",
        status="COMPLETED",
        model_tier="stub",
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (
            t.id,
            t.file_path,
            t.snapshot_path,
            t.file_sha256,
            t.status,
            t.model_tier,
            t.created_at,
            t.updated_at,
            t.error_msg,
        ),
    )
    conn.execute(
        "INSERT INTO sections VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "t1", 0, "/raw.md", "SEC1", 100, "COMPLETED", int(time.time())),
    )
    conn.execute(
        "INSERT INTO ai_artifacts VALUES (?,?,?,?,?,?,?,?,?)",
        ("a1", "s1", "/cached.ai.md", 1, 1, 0.0, 0, "stub", int(time.time())),
    )
    conn.commit()


def test_file_cache_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(Repository(conn))
    t = cache.find_completed_task_by_file_sha256("FILE1")
    assert t is not None and t.id == "t1"
    conn.close()


def test_file_cache_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(Repository(conn))
    assert cache.find_completed_task_by_file_sha256("missing") is None
    conn.close()


def test_section_artifact_hit(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    seed(conn)
    cache = CacheService(Repository(conn))
    a = cache.find_completed_artifact_by_section_sha256("SEC1")
    assert a is not None and a.ai_md_path == "/cached.ai.md"
    conn.close()


def test_section_artifact_miss(tmp_path):
    conn = init_db(str(tmp_path / "x.db"))
    cache = CacheService(Repository(conn))
    assert cache.find_completed_artifact_by_section_sha256("nope") is None
    conn.close()
