import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest

from parsing_core.serving import serve as serve_module
from parsing_core.serving.serve import (
    build_app,
    recover_interrupted_work,
    session_token_from_fd,
)
from parsing_core.storage.schema import init_db
from parsing_core.workbench import markdown_sync as markdown_sync_module
from parsing_core.workbench import pipeline as pipeline_module
from parsing_core.workbench import topic_markdown_sync as topic_markdown_sync_module
from parsing_core.workbench.executors import StubIntensiveReadingExecutor
from parsing_core.workbench.pipeline import ROUNDS, IntensiveReadingPipeline
from parsing_core.workbench.repository import WorkbenchRepository
from parsing_core.workbench.schema import apply_workbench_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"


def _token_pipe(token: str) -> int:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    try:
        os.write(write_fd, token.encode("ascii"))
    finally:
        os.close(write_fd)
    return read_fd


def _cleanup_process(process: subprocess.Popen[str]) -> None:
    errors: list[BaseException] = []
    try:
        if process.poll() is None:
            process.kill()
    except BaseException as exc:
        errors.append(exc)
    try:
        process.wait(timeout=5)
    except BaseException as exc:
        errors.append(exc)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


@contextmanager
def _production_server_process(
    port: int,
    env: dict[str, str],
) -> Iterator[subprocess.Popen[str]]:
    token_fd = _token_pipe(TEST_SESSION_TOKEN)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "parsing_core.serving.serve",
                "--port",
                str(port),
                "--session-token-fd",
                str(token_fd),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=(token_fd,),
        )
    finally:
        os.close(token_fd)
    body_failed = True
    try:
        yield process
        body_failed = False
    finally:
        try:
            _cleanup_process(process)
        except BaseException:
            if not body_failed:
                raise


def test_session_token_is_read_once_and_channel_is_closed():
    read_fd = _token_pipe(TEST_SESSION_TOKEN)

    assert session_token_from_fd(read_fd) == TEST_SESSION_TOKEN
    with pytest.raises(OSError):
        os.fstat(read_fd)


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
        session_token=TEST_SESSION_TOKEN,
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
    ).fetchone() == ("FAILED", "chapter generation interrupted")
    assert conn.execute("SELECT COUNT(*) FROM wb_chapter_generation_leases").fetchone()[0] == 0


@pytest.mark.parametrize("sync_failure", [False, True])
def test_startup_recovers_sync_pending_without_model_or_user_post(tmp_path, sync_failure):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    output_root = tmp_path / "course-output"
    if sync_failure:
        real_root = tmp_path / "real-output"
        real_root.mkdir()
        output_root.symlink_to(real_root, target_is_directory=True)
    course = repo.create_course("战略管理", "", str(output_root))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    source_path = tmp_path / "chapter.md"
    source_path.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(
        course.id,
        source.id,
        0,
        "第一章",
        str(source_path),
    )
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    start = repo.start_chapter_generation(chapter.id)
    for round_key in ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    run_count = conn.execute(
        "SELECT COUNT(*) FROM wb_chapter_generation_runs WHERE chapter_id = ?",
        (chapter.id,),
    ).fetchone()[0]
    conn.close()

    recover_interrupted_work(
        db_path,
        tmp_path / "tmp",
        resume_chapter_sync=True,
    )

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM wb_chapters WHERE id = ?",
        (chapter.id,),
    ).fetchone()[0]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wb_chapter_generation_runs WHERE chapter_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == run_count
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wb_chapter_generation_leases WHERE chapter_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == 0
    )
    if not sync_failure:
        assert status == "COMPLETED"
        assert (output_root / "教材" / "教材" / "01-第一章" / "intensive-note.md").exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wb_chapter_generation_publications WHERE chapter_id = ?",
                (chapter.id,),
            ).fetchone()[0]
            == 0
        )
    else:
        assert status == "SYNC_PENDING"
        error = conn.execute(
            "SELECT error FROM wb_chapter_generation_publications WHERE chapter_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        assert error
        assert str(tmp_path) not in error


def test_startup_recovery_uses_keyset_batches_for_new_pending_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    course = repo.create_course("战略管理", "", str(tmp_path / "output"))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    chapter_ids = []
    for seq in range(2):
        pipeline = IntensiveReadingPipeline(
            repo,
            StubIntensiveReadingExecutor(),
            tmp_path / f"runs-{seq}",
        )
        source_path = tmp_path / f"chapter-{seq}.md"
        source_path.write_text(f"## 第{seq + 1}章\n正文", encoding="utf-8")
        chapter = repo.create_chapter(
            course.id,
            source.id,
            seq,
            f"第{seq + 1}章",
            str(source_path),
        )
        repo.update_chapter_status(chapter.id, "CONFIRMED")
        start = repo.start_chapter_generation(chapter.id)
        for round_key in ROUNDS[:-1]:
            pipeline._run_candidate(chapter.id, start.owner_id, round_key)
        pipeline._run_review_and_stage(chapter.id, start.owner_id)
        chapter_ids.append(chapter.id)

    first_id, inserted_id = sorted(chapter_ids)
    inserted_row = conn.execute(
        "SELECT chapter_id, publication_id, owner_id, review_run_id, input_fingerprint, "
        "output_fingerprint, status, error, updated_at "
        "FROM wb_chapter_generation_publications WHERE chapter_id = ?",
        (inserted_id,),
    ).fetchone()
    conn.execute(
        "DELETE FROM wb_chapter_generation_publications WHERE chapter_id = ?",
        (inserted_id,),
    )
    conn.commit()
    conn.close()
    recovered = []
    inserted = False

    def recover_pending(repo, chapter_id):
        nonlocal inserted
        recovered.append(chapter_id)
        if chapter_id == first_id and not inserted:
            inserted = True
            repo.conn.execute(
                "INSERT INTO wb_chapter_generation_publications "
                "(chapter_id, publication_id, owner_id, review_run_id, input_fingerprint, "
                "output_fingerprint, status, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(inserted_row),
            )
            repo.conn.commit()
        return True

    monkeypatch.setattr(pipeline_module, "recover_pending_chapter_markdown_sync", recover_pending)
    monkeypatch.setattr(serve_module, "RECOVERY_BATCH_SIZE", 1, raising=False)

    serve_module._recover_pending_chapter_publications(db_path)

    assert recovered == [first_id, inserted_id]


def test_startup_rolls_forward_committed_publication_journal(tmp_path, monkeypatch):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    output_root = tmp_path / "course-output"
    course = repo.create_course("战略管理", "", str(output_root))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    source_path = tmp_path / "chapter.md"
    source_path.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(
        course.id,
        source.id,
        0,
        "第一章",
        str(source_path),
    )
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    real_recover = markdown_sync_module._recover_atomic_bundle_locked
    cleanup_failures = 2

    def leave_committed_journal(dir_fd, *, transaction_committed=None):
        nonlocal cleanup_failures
        receipt_exists = conn.execute(
            "SELECT COUNT(*) FROM wb_chapter_publication_receipts WHERE chapter_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        if transaction_committed is not None and receipt_exists and cleanup_failures:
            cleanup_failures -= 1
            raise OSError("simulated exit before journal cleanup")
        return real_recover(
            dir_fd,
            transaction_committed=transaction_committed,
        )

    monkeypatch.setattr(
        markdown_sync_module,
        "_recover_atomic_bundle_locked",
        leave_committed_journal,
    )
    IntensiveReadingPipeline(
        repo,
        StubIntensiveReadingExecutor(),
        tmp_path / "runs",
    ).run_all(chapter.id)
    journal = (
        output_root / "教材" / "教材" / "01-第一章" / "runs" / markdown_sync_module.JOURNAL_NAME
    )
    assert repo.get_chapter(chapter.id).status == "COMPLETED"
    assert journal.exists()
    monkeypatch.setattr(
        markdown_sync_module,
        "_recover_atomic_bundle_locked",
        real_recover,
    )
    conn.close()

    recover_interrupted_work(
        db_path,
        tmp_path / "tmp",
        resume_chapter_sync=True,
    )

    assert not journal.exists()
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute(
            "SELECT status FROM wb_chapters WHERE id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == "COMPLETED"
    )


def test_startup_rolls_forward_committed_topic_publication_journal(tmp_path, monkeypatch):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    output_root = tmp_path / "course-output"
    course = repo.create_course("战略管理", "", str(output_root))
    topic = repo.create_topic(course.id, 0, "竞争优势", "")
    repo.update_topic(topic.id, confirmed=True)
    repo.set_topic_markdown_sync_state(topic.id, "PENDING")
    real_recover = markdown_sync_module._recover_atomic_bundle_locked
    cleanup_failures = 2

    def leave_committed_journal(dir_fd, *, transaction_committed=None):
        nonlocal cleanup_failures
        receipt_exists = conn.execute(
            "SELECT COUNT(*) FROM wb_markdown_publication_receipts "
            "WHERE entity_type = 'topic' AND entity_id = ?",
            (topic.id,),
        ).fetchone()[0]
        if transaction_committed is not None and receipt_exists and cleanup_failures:
            cleanup_failures -= 1
            raise OSError("simulated exit before topic journal cleanup")
        return real_recover(
            dir_fd,
            transaction_committed=transaction_committed,
        )

    monkeypatch.setattr(
        markdown_sync_module,
        "_recover_atomic_bundle_locked",
        leave_committed_journal,
    )
    topic_markdown_sync_module.sync_topic_map_markdown(repo, topic.id)
    journal = output_root / "课程主题" / "01-竞争优势" / markdown_sync_module.JOURNAL_NAME
    assert journal.exists()
    monkeypatch.setattr(
        markdown_sync_module,
        "_recover_atomic_bundle_locked",
        real_recover,
    )
    conn.close()

    recover_interrupted_work(
        db_path,
        tmp_path / "tmp",
        resume_chapter_sync=True,
    )

    assert not journal.exists()


def test_startup_rolls_back_uncommitted_journal_before_invalidating_changed_input(tmp_path):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    output_root = tmp_path / "course-output"
    course = repo.create_course("战略管理", "", str(output_root))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    source_path = tmp_path / "chapter.md"
    source_path.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(
        course.id,
        source.id,
        0,
        "第一章",
        str(source_path),
    )
    repo.update_chapter_status(chapter.id, "CONFIRMED")
    pipeline = IntensiveReadingPipeline(repo, StubIntensiveReadingExecutor(), tmp_path / "runs")
    start = repo.start_chapter_generation(chapter.id)
    for round_key in ROUNDS[:-1]:
        pipeline._run_candidate(chapter.id, start.owner_id, round_key)
    pipeline._run_review_and_stage(chapter.id, start.owner_id)
    pending = repo.pending_chapter_markdown_sync(chapter.id)
    assert pending is not None
    chapter_dir = output_root / "教材" / "教材" / "01-第一章"
    chapter_dir.mkdir(parents=True)
    invalid_note = chapter_dir / "intensive-note.md"
    invalid_note.write_text("invalid old owner publication", encoding="utf-8")
    journal = chapter_dir / markdown_sync_module.JOURNAL_NAME
    journal.write_text(
        json.dumps(
            {
                "phase": "PREPARED",
                "transaction_id": pending["publication_id"],
                "entries": [
                    {
                        "target": "intensive-note.md",
                        "temp": ".pdf2md-deadbeef.bundle-tmp",
                        "backup": None,
                        "existed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_path.write_text("## 第一章\n输入已经改变。", encoding="utf-8")
    conn.close()

    recover_interrupted_work(
        db_path,
        tmp_path / "tmp",
        resume_chapter_sync=True,
    )

    assert not invalid_note.exists()
    assert not journal.exists()
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wb_markdown_publications "
            "WHERE entity_type = 'chapter' AND entity_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == 0
    )
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute(
            "SELECT status FROM wb_chapters WHERE id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == "FAILED"
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wb_chapter_generation_publications WHERE chapter_id = ?",
            (chapter.id,),
        ).fetchone()[0]
        == 0
    )


def test_startup_rolls_back_uncommitted_ordinary_publication_journal(tmp_path):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_workbench_schema(conn)
    repo = WorkbenchRepository(conn)
    output_root = tmp_path / "course-output"
    course = repo.create_course("战略管理", "", str(output_root))
    source = repo.create_source(course.id, "main", "/tmp/book.md", "教材")
    source_path = tmp_path / "chapter.md"
    source_path.write_text("## 第一章\n战略是选择。", encoding="utf-8")
    chapter = repo.create_chapter(
        course.id,
        source.id,
        0,
        "第一章",
        str(source_path),
    )
    claim = repo.claim_markdown_publication(
        "chapter",
        chapter.id,
        repo.markdown_publication_state_fingerprint("chapter", chapter.id),
        "content-fingerprint",
    )
    chapter_dir = output_root / "教材" / "教材" / "01-第一章"
    chapter_dir.mkdir(parents=True)
    invalid_note = chapter_dir / "intensive-note.md"
    invalid_note.write_text("uncommitted ordinary publication", encoding="utf-8")
    journal = chapter_dir / markdown_sync_module.JOURNAL_NAME
    journal.write_text(
        json.dumps(
            {
                "phase": "PREPARED",
                "transaction_id": claim.publication_id,
                "entries": [
                    {
                        "target": "intensive-note.md",
                        "temp": ".pdf2md-deadbeef.bundle-tmp",
                        "backup": None,
                        "existed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn.close()

    recover_interrupted_work(
        db_path,
        tmp_path / "tmp",
        resume_chapter_sync=True,
    )

    assert not invalid_note.exists()
    assert not journal.exists()


def test_production_server_starts_and_runs_shutdown_hook_on_sigterm(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path)
    env.pop("PDF2MD_SESSION_TOKEN", None)
    serve_base = tmp_path / "parsing-core-serve"
    with _production_server_process(port, env) as process:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"server exited during startup:\n{process.stdout.read()}")
            try:
                health_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN},
                )
                with urllib.request.urlopen(health_request, timeout=0.2) as response:
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


def test_authenticated_shutdown_exits_production_server_normally(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path)
    env.pop("PDF2MD_SESSION_TOKEN", None)
    with _production_server_process(port, env) as process:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"server exited during startup:\n{process.stdout.read()}")
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN},
                )
                with urllib.request.urlopen(request, timeout=0.2) as response:
                    assert response.status == 200
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("server did not become healthy")

        shutdown = urllib.request.Request(
            f"http://127.0.0.1:{port}/shutdown",
            data=b"",
            headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN},
            method="POST",
        )
        with urllib.request.urlopen(shutdown, timeout=1) as response:
            assert response.status == 200
            assert response.read() == b'{"status":"shutting_down"}'

        assert process.wait(timeout=5) == 0
