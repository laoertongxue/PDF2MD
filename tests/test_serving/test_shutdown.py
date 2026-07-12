import sqlite3

from parsing_core.serving.serve import recover_interrupted_work
from parsing_core.storage.schema import init_db
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema


def test_shutdown_marks_running_tasks_recoverable_and_cleans_temp_dir(tmp_path):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("running", "/a.pdf", "/snap", "sha", "RUNNING", "stub", 1, 1, None),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("done", "/b.pdf", "/snap", "sha2", "COMPLETED", "stub", 1, 1, None),
    )
    conn.commit()
    conn.close()
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    (temp_dir / "partial.bin").write_bytes(b"partial")

    recover_interrupted_work(db_path, temp_dir)

    conn = sqlite3.connect(db_path)
    running = conn.execute("SELECT status, error_msg FROM tasks WHERE id = 'running'").fetchone()
    done = conn.execute("SELECT status FROM tasks WHERE id = 'done'").fetchone()
    assert running == ("INTERRUPTED", "recoverable: interrupted by service shutdown")
    assert done == ("COMPLETED",)
    assert not temp_dir.exists()


def test_restart_marks_chapter_generation_interrupted_and_releases_owner(tmp_path):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    chapter = repo.create_chapter(course.id, source.id, 0, "第一章", "/tmp/chapter.md")
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    start = repo.start_chapter_generation(chapter.id)
    run = repo.create_chapter_generation_run(chapter.id, start.owner_id, "structure")
    conn.close()

    recover_interrupted_work(db_path, tmp_path / "tmp")

    conn = sqlite3.connect(db_path)
    assert (
        conn.execute("SELECT status FROM wb_chapters WHERE id = ?", (chapter.id,)).fetchone()[0]
        == "FAILED"
    )
    assert conn.execute(
        "SELECT status, error FROM wb_chapter_generation_runs WHERE id = ?", (run.id,)
    ).fetchone() == ("FAILED", "interrupted")
    assert conn.execute("SELECT COUNT(*) FROM wb_chapter_generation_leases").fetchone()[0] == 0
