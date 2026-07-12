import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager

import pytest

from parsing_core.serving.serve import build_app, recover_interrupted_work
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


@pytest.mark.asyncio
async def test_shutdown_hook_composes_with_existing_lifespan():
    events = []

    @asynccontextmanager
    async def existing_lifespan(_app):
        events.append("existing-start")
        try:
            yield
        finally:
            events.append("existing-stop")

    app = build_app(
        orch_factory=lambda: object(),
        lifespan=existing_lifespan,
        shutdown_hook=lambda: events.append("shutdown-hook"),
    )

    async with app.router.lifespan_context(app):
        events.append("running")

    assert events == ["existing-start", "running", "existing-stop", "shutdown-hook"]


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


def test_production_server_starts_and_runs_shutdown_hook_on_sigterm(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "parsing_core.serving.serve", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    serve_base = tmp_path / "parsing-core-serve"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"server exited during startup:\n{process.stdout.read()}")
            try:
                health_url = f"http://127.0.0.1:{port}/health"
                with urllib.request.urlopen(health_url, timeout=0.2) as response:
                    assert response.status == 200
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("server did not become healthy")

        db_path = serve_base / "serve.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("live", "/a.pdf", "/snap", "sha-live", "RUNNING", "stub", 1, 1, None, None),
        )
        conn.commit()
        conn.close()
        temp_dir = serve_base / "tmp"
        temp_dir.mkdir(exist_ok=True)
        (temp_dir / "partial.bin").write_bytes(b"partial")

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) in {0, -signal.SIGTERM}

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT status, error_msg FROM tasks WHERE id = 'live'").fetchone() == (
            "INTERRUPTED",
            "recoverable: interrupted by service shutdown",
        )
        assert not temp_dir.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
