import time

from parsing_core.models.dataclasses import AIArtifact, Section, Task
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db


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
