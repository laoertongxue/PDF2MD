import sqlite3

from parsing_core.serving.serve import recover_interrupted_work
from parsing_core.storage.schema import init_db


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
