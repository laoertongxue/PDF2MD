import asyncio
import concurrent.futures
import fcntl
import hashlib
import multiprocessing
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Section, Task
from parsing_core.orchestrator import Orchestrator
from parsing_core.parser.chunker import split_sections
from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.api.routes_tasks import create_task
from parsing_core.serving.models.api import TaskCreateRequest, WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer
from parsing_core.serving.scheduler import Scheduler
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


class _ProcessBlockingParser:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release

    def parse(self, path):
        markdown = Path(path).read_text(encoding="utf-8")
        self.entered.wait(timeout=10)
        if not self.release.wait(timeout=10):
            raise RuntimeError("initial parse release timed out")
        return markdown


def _run_initial_parse_process(
    db_path,
    base_dir,
    source_path,
    task_id,
    entered,
    release,
    result_queue,
):
    conn = init_db(db_path)
    try:
        apply_serve_schema(conn)
        orch = Orchestrator(
            repo=Repository(conn),
            fs=FsLayout(base_dir=base_dir),
            llm=StubLLMClient(),
            db_path=db_path,
        )
        orch.parser = _ProcessBlockingParser(entered, release)
        try:
            result = orch.parse_file(source_path, force=True, task_id=task_id)
        except BaseException as error:
            result_queue.put(("parse", "error", repr(error)))
        else:
            result_queue.put(("parse", "ok", result))
    finally:
        conn.close()


def _run_delete_process(
    db_path,
    base_dir,
    task_id,
    started,
    done,
    result_queue,
):
    conn = init_db(db_path)
    try:
        apply_serve_schema(conn)
        orch = Orchestrator(
            repo=Repository(conn),
            fs=FsLayout(base_dir=base_dir),
            llm=StubLLMClient(),
            db_path=db_path,
        )
        scheduler = Scheduler(lambda: orch)

        async def delete_and_shutdown():
            try:
                return await scheduler.delete_task(task_id)
            finally:
                await scheduler.shutdown()

        started.set()
        try:
            result = asyncio.run(delete_and_shutdown())
        except BaseException as error:
            result_queue.put(("delete", "error", repr(error)))
        else:
            result_queue.put(("delete", "ok", result))
    finally:
        done.set()
        conn.close()


def _run_barrier_delete_process(
    db_path,
    base_dir,
    task_id,
    start_barrier,
    result_queue,
):
    conn = init_db(db_path)
    try:
        apply_serve_schema(conn)
        orch = Orchestrator(
            repo=Repository(conn),
            fs=FsLayout(base_dir=base_dir),
            llm=StubLLMClient(),
            db_path=db_path,
        )
        scheduler = Scheduler(lambda: orch)

        async def delete_and_shutdown():
            try:
                return await scheduler.delete_task(task_id)
            finally:
                await scheduler.shutdown()

        start_barrier.wait(timeout=10)
        try:
            result = asyncio.run(delete_and_shutdown())
            lock_inode = (Path(base_dir) / ".locks" / f"{task_id}.lock").stat().st_ino
        except BaseException as error:
            result_queue.put(("error", repr(error), None))
        else:
            result_queue.put(("ok", result, lock_inode))
    finally:
        conn.close()


class RecordingBatchRepo:
    def __init__(self):
        self.batches = {}
        self.set_progress_calls = []
        self.tasks = {}

    def create_batch(self, batch):
        self.batches[batch["id"]] = dict(batch)

    def create_batch_with_tasks(self, batch, tasks):
        self.create_batch(batch)
        for task in tasks:
            self.tasks[task.id] = task

    def create_task(self, task):
        self.tasks[task.id] = task

    def update_task_status(self, task_id, status, error_msg=None):
        task = self.tasks[task_id]
        task.status = status
        task.error_msg = error_msg
        task.updated_at = int(time.time())

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def delete_task(self, task_id):
        self.tasks.pop(task_id, None)

    def list_all_tasks(self):
        return list(self.tasks.values())

    def get_batch(self, batch_id):
        batch = self.batches.get(batch_id)
        return dict(batch) if batch is not None else None

    def increment_batch_completed(self, batch_id):
        self.batches[batch_id]["completed_tasks"] += 1

    def finish_batch(self, batch_id, status):
        self.batches[batch_id]["status"] = status
        self.batches[batch_id]["finished_at"] = 1

    def set_batch_progress(self, batch_id, completed, status=None):
        self.set_progress_calls.append((batch_id, completed, status))
        batch = self.batches[batch_id]
        batch["completed_tasks"] = max(batch["completed_tasks"], completed)
        if status is not None:
            priorities = {"RUNNING": 0, "COMPLETED": 1, "FAILED": 2, "CANCELLED": 3}
            if priorities[status] >= priorities.get(batch["status"], 0):
                batch["status"] = status
            batch["finished_at"] = 1


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make_recording_scheduler(*, parse_file=None, purge_task=None, repo=None, **scheduler_kwargs):
    repo = repo or RecordingBatchRepo()

    class RecordingOrchestrator:
        def __init__(self):
            self.repo = repo
            self.on_progress = None

        def parse_file(self, file_path, force, task_id, batch_id):
            if parse_file is not None:
                return parse_file(file_path, force, task_id, batch_id)
            return None

        def purge(self, task_id):
            if repo.get_task(task_id) is None:
                return {"task_id": task_id, "purged": False}
            if purge_task is not None:
                purge_task(task_id)
            repo.delete_task(task_id)
            return {"task_id": task_id, "purged": True}

        def try_purge(self, task_id):
            return self.purge(task_id)

    return Scheduler(RecordingOrchestrator, **scheduler_kwargs), repo


async def wait_until(predicate, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def assert_batch_memory_purged(scheduler, batch_id):
    batch_maps = (
        scheduler._batches,
        scheduler._buffers,
        scheduler._subscribers,
        scheduler._subscriber_queues,
        scheduler._subscriber_tasks,
        scheduler._seq_counters,
        scheduler._batch_tasks,
        scheduler._thread_task_ids,
    )
    assert all(batch_id not in mapping for mapping in batch_maps)
    assert batch_id not in scheduler._cancelled


def make_orch_factory(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db_path = tmp_path / "serve.db"

    def factory():
        sub_dir = base / f"task_{time.time_ns()}"
        sub_dir.mkdir()
        fs = FsLayout(base_dir=str(sub_dir))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    return factory


def test_scheduler_startup_cleans_interrupted_materialization_outputs(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    tasks_dir = Path(fs.tasks_dir())
    source_input = tmp_path / "course.pdf"
    source_input.write_bytes(b"user-input")
    now = int(time.time())
    statuses = {
        "completed-task": "COMPLETED",
        "pending-task": "PENDING",
        "pending-with-sections": "PENDING",
        "failed-task": "FAILED",
        "parsing-task": "PARSING",
        "running-task": "RUNNING",
        "interrupted-task": "INTERRUPTED",
    }
    snapshots = {}
    for task_id, status in statuses.items():
        snapshot = tmp_path / f"{task_id}.snapshot"
        snapshot.write_bytes(b"snapshot")
        snapshots[task_id] = snapshot
        repo.create_task(
            Task(
                id=task_id,
                file_path=str(source_input),
                snapshot_path=str(snapshot),
                file_sha256=task_id,
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
        task_dir = tasks_dir / task_id
        task_dir.mkdir()
        (task_dir / "merged.md").write_text(status, encoding="utf-8")
    pending_raw = tasks_dir / "pending-with-sections" / "0.raw.md"
    pending_raw.write_text("recoverable raw", encoding="utf-8")
    repo.create_section(
        Section(
            id="pending-section",
            task_id="pending-with-sections",
            seq=0,
            raw_md_path=str(pending_raw),
            sha256="pending-sha",
            char_count=15,
            ai_status="PENDING",
            created_at=now,
        )
    )
    stale_staging = [
        tasks_dir / ".pending-task.first.tmp",
        tasks_dir / ".completed-task.second.tmp",
    ]
    for path in stale_staging:
        path.mkdir()
        (path / "partial.md").write_text("partial", encoding="utf-8")

    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: orch)

    assert (tasks_dir / "completed-task" / "merged.md").is_file()
    assert not (tasks_dir / "pending-task").exists()
    for preserved_task_id in (
        "failed-task",
        "pending-with-sections",
        "parsing-task",
        "running-task",
        "interrupted-task",
    ):
        assert (tasks_dir / preserved_task_id / "merged.md").is_file()
    assert all(not path.exists() for path in stale_staging)
    assert source_input.read_bytes() == b"user-input"
    assert all(path.read_bytes() == b"snapshot" for path in snapshots.values())
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_scheduler_startup_cleanup_is_bounded_and_avoids_candidate_n_plus_one() -> None:
    page_size = 128
    task_count = 1_025

    class BoundedLayout:
        def __init__(self) -> None:
            self.max_batch = 0
            self.removed_staging: list[str] = []
            self.removed_tasks: list[str] = []

        def iter_task_entries(self, *, batch_size: int):
            assert batch_size == page_size
            names = [f".task-{index:04d}.nonce.tmp" for index in range(task_count)]
            for offset in range(0, len(names), batch_size):
                batch = [
                    type(
                        "Entry",
                        (),
                        {"name": name, "identity": (1, offset + position + 1)},
                    )()
                    for position, name in enumerate(names[offset : offset + batch_size])
                ]
                self.max_batch = max(self.max_batch, len(batch))
                yield batch

        def remove_staging_tree(
            self,
            name: str,
            *,
            expected_identity: tuple[int, int],
        ) -> bool:
            self.removed_staging.append(name)
            return True

        def remove_task_tree(
            self,
            task_id: str,
            *,
            expected_identity: tuple[int, int] | None = None,
        ) -> bool:
            self.removed_tasks.append(task_id)
            return True

        def task_identity(self, task_id: str) -> tuple[int, int] | None:
            return (1, int(task_id.rsplit("-", 1)[1]) + 1)

    class BoundedRepo:
        def __init__(self) -> None:
            self.materialized_calls = 0
            self.page_calls = 0
            self.get_task_calls = 0

        def list_task_ids_with_materialized_children(self) -> set[str]:
            self.materialized_calls += 1
            return {f"task-{index:04d}" for index in range(0, task_count, 2)}

        def list_waiting_tasks_page(self, *, after_id: str | None, limit: int) -> list[Task]:
            self.page_calls += 1
            assert limit == page_size
            start = 0 if after_id is None else int(after_id.rsplit("-", 1)[1]) + 1
            stop = min(start + limit, task_count)
            return [
                Task(
                    id=f"task-{index:04d}",
                    file_path=f"/tmp/task-{index:04d}.pdf",
                    snapshot_path="",
                    file_sha256=str(index),
                    status="WAITING",
                    created_at=index,
                    updated_at=index,
                )
                for index in range(start, stop)
            ]

        def get_task(self, _task_id: str) -> Task | None:
            self.get_task_calls += 1
            raise AssertionError("startup cleanup must not issue candidate N+1 queries")

    layout = BoundedLayout()
    repo = BoundedRepo()

    class CleanupOrchestrator:
        def __init__(self) -> None:
            self.fs = layout
            self.repo = repo
            self.on_progress = None

        def _try_open_resume_lock(self, _task_id: str, *, blocking: bool = False) -> int:
            assert blocking is False
            return 123

        def _close_resume_lock(self, _lock_fd: int) -> None:
            return None

    scheduler = Scheduler(CleanupOrchestrator)

    assert layout.max_batch <= page_size
    assert len(layout.removed_staging) == task_count
    assert len(layout.removed_tasks) == task_count // 2
    assert repo.materialized_calls == 1
    assert repo.page_calls <= (task_count + page_size - 1) // page_size + 1
    assert repo.get_task_calls == 0
    asyncio.run(scheduler.shutdown())


def test_scheduler_startup_preserves_failed_task_outputs_for_real_resume(tmp_path):
    class FailingLLM(StubLLMClient):
        def interpret(self, section, raw_md):
            raise RuntimeError("intentional LLM interruption")

    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    source = tmp_path / "course.md"
    source.write_text("# 第一章\n\n恢复所需正文。\n", encoding="utf-8")
    failing = Orchestrator(repo=repo, fs=fs, llm=FailingLLM(), db_path=str(db_path))

    with pytest.raises(RuntimeError, match="intentional LLM interruption"):
        failing.parse_file(str(source), force=True)

    failed_task = repo.list_tasks_by_status("FAILED")[0]
    sections = repo.list_sections(failed_task.id)
    assert sections
    raw_paths = [Path(section.raw_md_path) for section in sections]
    assert all(path.is_file() for path in raw_paths)
    resumed = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    scheduler = Scheduler(lambda: resumed)
    result = resumed.resume(failed_task.id)

    assert result["status"] == "COMPLETED"
    assert all(path.is_file() for path in raw_paths)
    assert Path(fs.merged_path(failed_task.id)).is_file()
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_scheduler_restart_resume_reparses_snapshot_after_parser_failure(tmp_path, monkeypatch):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    source = tmp_path / "course.md"
    source.write_text("## 第一章\n\n只能从保留副本恢复的正文。\n", encoding="utf-8")
    failing = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    def fail_before_sections(_snapshot_path):
        raise RuntimeError("parser interrupted before sections")

    monkeypatch.setattr(failing.parser, "parse", fail_before_sections)
    with pytest.raises(RuntimeError, match="parser interrupted before sections"):
        failing.parse_file(str(source), force=True)

    failed_task = repo.list_tasks_by_status("FAILED")[0]
    assert repo.list_sections(failed_task.id) == []
    assert Path(failed_task.snapshot_path).is_file()
    source.unlink()

    resumed = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: resumed)
    result = resumed.resume(failed_task.id)

    sections = repo.list_sections(failed_task.id)
    assert result["status"] == "COMPLETED"
    assert result["sections"] == len(sections) > 0
    assert all(section.ai_status == "COMPLETED" for section in sections)
    merged = Path(result["merged_md_path"])
    assert "只能从保留副本恢复的正文" in merged.read_text(encoding="utf-8")
    assert not Path(failed_task.snapshot_path).exists()
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_scheduler_restart_repairs_legacy_completed_task_with_zero_sections(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    snapshot_path = tmp_path / "legacy.snapshot.md"
    snapshot_path.write_text("## 第一章\n\n旧任务恢复正文。\n", encoding="utf-8")
    now = int(time.time())
    repo.create_task(
        Task(
            id="legacy-empty-completed",
            file_path=str(tmp_path / "missing-original.md"),
            snapshot_path=str(snapshot_path),
            file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            status="COMPLETED",
            created_at=now,
            updated_at=now,
        )
    )

    resumed = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: resumed)
    result = resumed.resume("legacy-empty-completed")

    sections = repo.list_sections("legacy-empty-completed")
    assert result["status"] == "COMPLETED"
    assert sections
    assert "旧任务恢复正文" in Path(result["merged_md_path"]).read_text(encoding="utf-8")
    assert not snapshot_path.exists()
    asyncio.run(scheduler.shutdown())
    conn.close()


@pytest.mark.parametrize(
    "checkpoint_state",
    ["missing", "partial", "seq_gap", "count_mismatch"],
)
def test_scheduler_restart_rebuilds_untrusted_section_checkpoint(
    tmp_path,
    checkpoint_state,
):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    markdown = (
        "## 第一章\n\nCHECKPOINT_ALPHA\n\n"
        "## 第二章\n\nCHECKPOINT_BETA\n\n"
        "## 第三章\n\nCHECKPOINT_GAMMA\n"
    )
    snapshot_path = tmp_path / "checkpoint.snapshot.md"
    snapshot_path.write_text(markdown, encoding="utf-8")
    now = int(time.time())
    task = Task(
        id=f"checkpoint-{checkpoint_state}",
        file_path=str(tmp_path / "missing-original.md"),
        snapshot_path=str(snapshot_path),
        file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        status="FAILED",
        created_at=now,
        updated_at=now,
    )
    repo.create_task(task)
    chunks = split_sections(markdown)
    selected = {
        "missing": [0, 1, 2],
        "partial": [0],
        "seq_gap": [0, 2],
        "count_mismatch": [0, 1],
    }[checkpoint_state]
    old_sections = []
    for seq in selected:
        raw_path = Path(fs.section_raw_path(task.id, seq))
        raw_path.write_text(chunks[seq].raw, encoding="utf-8")
        section = Section(
            id=f"old-{checkpoint_state}-{seq}",
            task_id=task.id,
            seq=seq,
            raw_md_path=str(raw_path),
            sha256=chunks[seq].sha256,
            char_count=chunks[seq].char_count,
            ai_status="PENDING",
            created_at=now,
        )
        repo.create_section(section)
        old_sections.append(section)
    if checkpoint_state != "missing":
        expected_sections = {
            "partial": 3,
            "seq_gap": 2,
            "count_mismatch": 3,
        }[checkpoint_state]
        conn.execute(
            "UPDATE task_recovery SET sectioning_complete = 1, expected_sections = ? "
            "WHERE task_id = ?",
            (expected_sections, task.id),
        )
        conn.commit()

    resumed = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: resumed)
    result = resumed.resume(task.id)

    rebuilt = repo.list_sections(task.id)
    merged = Path(result["merged_md_path"]).read_text(encoding="utf-8")
    assert result["status"] == "COMPLETED"
    assert [section.seq for section in rebuilt] == [0, 1, 2]
    assert {section.id for section in rebuilt}.isdisjoint({section.id for section in old_sections})
    assert "CHECKPOINT_ALPHA" in merged
    assert "CHECKPOINT_BETA" in merged
    assert "CHECKPOINT_GAMMA" in merged
    recovery = repo.get_task_recovery(task.id)
    assert recovery is not None
    assert recovery["sectioning_complete"] is True
    assert recovery["expected_sections"] == 3
    assert not snapshot_path.exists()
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_two_connections_resume_once_with_claim_and_fencing(tmp_path):
    class BlockingParser:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.entered = threading.Barrier(2)
            self.release = threading.Event()

        def parse(self, path):
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.entered.wait(timeout=2)
                assert self.release.wait(timeout=2)
            return Path(path).read_text(encoding="utf-8")

    class CountingLLM(StubLLMClient):
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def interpret(self, section, raw_md):
            with self.lock:
                self.calls += 1
            return super().interpret(section, raw_md)

    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    first_conn = init_db(str(db_path))
    apply_serve_schema(first_conn)
    first_repo = Repository(first_conn)
    fs = FsLayout(base_dir=str(base))
    snapshot_path = tmp_path / "concurrent.snapshot.md"
    snapshot_path.write_text("## 第一章\n\nCONCURRENT_RECOVERY\n", encoding="utf-8")
    now = int(time.time())
    task = Task(
        id="concurrent-resume",
        file_path=str(tmp_path / "missing-original.md"),
        snapshot_path=str(snapshot_path),
        file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        status="FAILED",
        created_at=now,
        updated_at=now,
    )
    first_repo.create_task(task)
    parser = BlockingParser()
    llm = CountingLLM()
    first_orch = Orchestrator(
        repo=first_repo,
        fs=fs,
        llm=llm,
        db_path=str(db_path),
    )
    first_orch.parser = parser
    first_results = []
    first_errors = []

    def run_first_resume():
        try:
            first_results.append(first_orch.resume(task.id))
        except BaseException as error:
            first_errors.append(error)

    worker = threading.Thread(target=run_first_resume)
    worker.start()
    parser.entered.wait(timeout=2)
    second_conn = init_db(str(db_path))
    apply_serve_schema(second_conn)
    second_repo = Repository(second_conn)
    second_orch = Orchestrator(
        repo=second_repo,
        fs=fs,
        llm=llm,
        db_path=str(db_path),
    )
    second_orch.parser = parser
    try:
        second_result = second_orch.resume(task.id)
    finally:
        parser.release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert first_errors == []
    assert first_results and first_results[0]["status"] == "COMPLETED"
    assert second_result["status"] == "BUSY"
    assert parser.calls == 1
    assert llm.calls == 1
    final_task = second_repo.get_task(task.id)
    assert final_task is not None and final_task.status == "COMPLETED"
    recovery = second_repo.get_task_recovery(task.id)
    assert recovery is not None
    assert recovery["resume_owner"] is None
    assert recovery["resume_generation"] == 1
    first_conn.close()
    second_conn.close()


def test_new_orchestrator_clears_unlocked_stale_resume_claim(tmp_path):
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_task(
        Task(
            id="stale-resume",
            file_path="/missing/input.md",
            snapshot_path="/missing/snapshot.md",
            file_sha256="sha",
            status="FAILED",
            created_at=1,
            updated_at=1,
        )
    )
    assert repo.claim_task_resume("stale-resume", "previous-process:owner") == 1

    Orchestrator(
        repo=repo,
        fs=FsLayout(base_dir=str(tmp_path / "data")),
        llm=StubLLMClient(),
        db_path=str(db_path),
    )

    recovery = repo.get_task_recovery("stale-resume")
    assert recovery is not None
    assert recovery["resume_owner"] is None
    assert recovery["resume_generation"] == 1
    conn.close()


def test_scheduler_startup_does_not_delete_pending_task_with_active_resume_claim(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    first_conn = init_db(str(db_path))
    apply_serve_schema(first_conn)
    first_repo = Repository(first_conn)
    fs = FsLayout(base_dir=str(base))
    snapshot_path = tmp_path / "active-claim.snapshot.md"
    snapshot_path.write_text("## 第一章\n\nACTIVE_CLAIM_BODY\n", encoding="utf-8")
    task = Task(
        id="active-pending-resume",
        file_path=str(tmp_path / "missing-original.md"),
        snapshot_path=str(snapshot_path),
        file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        status="PENDING",
        created_at=1,
        updated_at=1,
    )
    first_repo.create_task(task)
    first_orch = Orchestrator(
        repo=first_repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    checkpoint_entered = threading.Barrier(2)
    release_checkpoint = threading.Event()
    first_results = []
    first_errors = []

    def block_checkpoint(_task_id, _sections):
        checkpoint_entered.wait(timeout=2)
        assert release_checkpoint.wait(timeout=2)
        return False

    def run_resume():
        try:
            first_results.append(first_orch.resume(task.id))
        except BaseException as error:
            first_errors.append(error)

    first_orch._has_trusted_section_checkpoint = block_checkpoint
    task_dir = Path(fs.task_dir(task.id))
    marker = task_dir / "resume-marker.md"
    marker.write_text("active", encoding="utf-8")
    worker = threading.Thread(target=run_resume)
    worker.start()
    checkpoint_entered.wait(timeout=2)
    lock_path = Path(fs.base_dir) / ".locks" / f"{task.id}.lock"
    assert lock_path.is_file()
    lock_inode = lock_path.stat().st_ino

    second_conn = init_db(str(db_path))
    apply_serve_schema(second_conn)
    second_orch = Orchestrator(
        repo=Repository(second_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = Scheduler(lambda: second_orch)
    try:
        assert task_dir.is_dir()
        assert marker.is_file()
        assert lock_path.is_file()
        assert lock_path.stat().st_ino == lock_inode
    finally:
        release_checkpoint.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert first_errors == []
    assert first_results and first_results[0]["status"] == "COMPLETED"
    asyncio.run(scheduler.shutdown())
    first_conn.close()
    second_conn.close()


def test_scheduler_startup_skips_waiting_output_held_by_initial_parse_lock(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    first_conn = init_db(str(db_path))
    apply_serve_schema(first_conn)
    first_repo = Repository(first_conn)
    fs = FsLayout(base_dir=str(base))
    source = tmp_path / "startup-active.md"
    source.write_text("## 第一章\n\nSTARTUP_ACTIVE_PARSE\n", encoding="utf-8")
    task = Task(
        id="startup-active-initial-parse",
        file_path=str(source),
        snapshot_path="",
        file_sha256="",
        status="WAITING",
        created_at=1,
        updated_at=1,
    )
    first_repo.create_task(task)
    first_orch = Orchestrator(
        repo=first_repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    promotion_entered = threading.Barrier(2)
    release_promotion = threading.Event()
    parse_results = []
    parse_errors = []
    real_promote = first_repo.promote_preregistered_task
    task_dir = Path(fs.task_dir(task.id))
    marker = task_dir / "active-initial-parse.marker"

    def blocking_promote(promoted_task):
        marker.write_text("active", encoding="utf-8")
        promotion_entered.wait(timeout=3)
        assert release_promotion.wait(timeout=3)
        real_promote(promoted_task)

    first_repo.promote_preregistered_task = blocking_promote

    def run_parse():
        try:
            parse_results.append(first_orch.parse_file(str(source), force=True, task_id=task.id))
        except BaseException as error:
            parse_errors.append(error)

    parse_thread = threading.Thread(target=run_parse)
    parse_thread.start()
    promotion_entered.wait(timeout=3)
    second_conn = init_db(str(db_path))
    apply_serve_schema(second_conn)
    second_orch = Orchestrator(
        repo=Repository(second_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = None
    try:
        scheduler = Scheduler(lambda: second_orch)
        marker_preserved_while_parse_active = marker.is_file()
    finally:
        release_promotion.set()
    parse_thread.join(timeout=5)

    assert marker_preserved_while_parse_active is True
    assert not parse_thread.is_alive()
    assert parse_errors == []
    assert parse_results and parse_results[0]["status"] == "COMPLETED"
    if scheduler is not None:
        asyncio.run(scheduler.shutdown())
    first_conn.close()
    second_conn.close()


def test_resume_db_error_after_task_lock_releases_flock(tmp_path, monkeypatch):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    snapshot_path = tmp_path / "db-error.snapshot.md"
    snapshot_path.write_text("## 第一章\n\nDB_ERROR_LOCK_RELEASE\n", encoding="utf-8")
    task = Task(
        id="resume-db-error-lock-release",
        file_path=str(tmp_path / "missing.md"),
        snapshot_path=str(snapshot_path),
        file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        status="FAILED",
        created_at=1,
        updated_at=1,
    )
    repo.create_task(task)
    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    real_get_task = repo.get_task
    get_calls = 0

    def fail_after_lock(task_id):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 2:
            raise RuntimeError("database failed after task lock")
        return real_get_task(task_id)

    monkeypatch.setattr(repo, "get_task", fail_after_lock)
    with pytest.raises(RuntimeError, match="database failed after task lock"):
        orch.resume(task.id)
    monkeypatch.setattr(repo, "get_task", real_get_task)

    lock_path = base / ".locks" / f"{task.id}.lock"
    probe_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    acquired = False
    try:
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            acquired = True
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd)

    assert acquired is True
    conn.close()


def test_scheduler_restart_resume_reparses_all_sections_after_atomic_section_failure(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    source = tmp_path / "course.md"
    markers = ("RECOVERY_ALPHA", "RECOVERY_BETA", "RECOVERY_GAMMA")
    source.write_text(
        "".join(f"## 第 {index} 章\n\n{marker}\n\n" for index, marker in enumerate(markers, 1)),
        encoding="utf-8",
    )
    failing = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    original_insert = repo._insert_section
    insert_count = 0

    def fail_second_section(section):
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("section batch interrupted")
        original_insert(section)

    monkeypatch.setattr(repo, "_insert_section", fail_second_section)
    with pytest.raises(RuntimeError, match="section batch interrupted"):
        failing.parse_file(str(source), force=True)

    failed_task = repo.list_tasks_by_status("FAILED")[0]
    assert repo.list_sections(failed_task.id) == []
    assert Path(failed_task.snapshot_path).is_file()
    source.unlink()

    resumed = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: resumed)
    result = resumed.resume(failed_task.id)

    sections = repo.list_sections(failed_task.id)
    assert result["status"] == "COMPLETED"
    assert [section.seq for section in sections] == [0, 1, 2]
    raw_text = "".join(
        Path(section.raw_md_path).read_text(encoding="utf-8") for section in sections
    )
    merged_text = Path(result["merged_md_path"]).read_text(encoding="utf-8")
    for marker in markers:
        assert raw_text.count(marker) == 1
        assert merged_text.count(marker) == 1
    assert not Path(failed_task.snapshot_path).exists()
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_submit_batch_returns_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path), max_global_concurrency=4)

    async def go():
        result = await sch.submit_batch(
            files=[str(Path("tests/fixtures/sample.md").resolve())], concurrency=2, priority=0
        )
        await asyncio.wait_for(sch._batches[result.batch_id].done.wait(), timeout=5)
        return result

    result = asyncio.run(go())
    assert result.batch_id
    assert len(result.task_ids) == 1
    assert result.accepted == 1
    assert result.rejected == 0


def test_create_task_route_parses_into_fs_layout_tasks_hierarchy(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    orch = Orchestrator(
        repo=repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = Scheduler(lambda: orch)
    source = Path("tests/fixtures/sample.md").resolve()

    async def run_route() -> object:
        response = await create_task(TaskCreateRequest(file_path=str(source)), scheduler)
        await asyncio.wait_for(
            scheduler._batches[response.batch_id].done.wait(),
            timeout=5,
        )
        await scheduler.shutdown()
        return response

    response = asyncio.run(run_route())
    task_id = response.task_ids[0]
    task = repo.get_task(task_id)

    assert task is not None and task.status == "COMPLETED"
    assert (base / "tasks" / task_id / "merged.md").is_file()
    assert not (base / task_id).exists()
    conn.close()


def test_submit_batch_preregisters_every_accepted_task_before_returning():
    async def go():
        scheduler, repo = make_recording_scheduler()
        response = await scheduler.submit_batch(
            files=["first.pdf", "second.pdf"],
            concurrency=1,
        )

        assert set(repo.tasks) == set(response.task_ids)
        assert [repo.tasks[task_id].file_path for task_id in response.task_ids] == [
            "first.pdf",
            "second.pdf",
        ]
        assert {repo.tasks[task_id].status for task_id in response.task_ids} == {"WAITING"}
        assert {repo.tasks[task_id].batch_id for task_id in response.task_ids} == {
            response.batch_id
        }

        await scheduler.cancel_batch(response.batch_id)
        await asyncio.wait_for(scheduler._batches[response.batch_id].done.wait(), timeout=1)

    asyncio.run(go())


def test_cache_hit_and_pre_snapshot_failure_update_the_accepted_task_id():
    async def cached_case():
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: {
                "task_id": "different-cache-task",
                "cached": True,
                "status": "COMPLETED",
            }
        )
        response = await scheduler.submit_batch(files=["cached.pdf"], concurrency=1)
        await asyncio.wait_for(scheduler._batches[response.batch_id].done.wait(), timeout=1)
        assert set(repo.tasks) == {response.task_ids[0]}
        assert repo.tasks[response.task_ids[0]].status == "COMPLETED"

    async def failed_case():
        def fail_before_task_creation(*_args):
            raise FileNotFoundError("snapshot failed")

        scheduler, repo = make_recording_scheduler(parse_file=fail_before_task_creation)
        response = await scheduler.submit_batch(files=["missing.pdf"], concurrency=1)
        await asyncio.wait_for(scheduler._batches[response.batch_id].done.wait(), timeout=1)
        task = repo.tasks[response.task_ids[0]]
        assert task.status == "FAILED"
        assert task.error_msg == "snapshot failed"

    asyncio.run(cached_case())
    asyncio.run(failed_case())


def test_emit_increments_seq(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)

    async def go():
        await sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="BATCH_STATE", payload={}, ts=0))
        await sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="BATCH_STATE", payload={}, ts=0))

    asyncio.run(go())
    events = list(sch._buffers["b1"])
    assert [e.seq for e in events] == [0, 1]


def test_emit_to_subscriber(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.sent_event = asyncio.Event()

        async def send_text(self, text):
            self.sent.append(text)
            self.sent_event.set()

    ws = FakeWS()

    async def go():
        sch.add_subscriber("b1", ws)
        sender, _ = sch.start_subscriber("b1", ws, [])
        await sch._emit(
            "b1",
            WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={"status": "PARSING"}, ts=0),
        )
        await asyncio.wait_for(ws.sent_event.wait(), timeout=1)
        removed_sender = sch.remove_subscriber("b1", ws)
        if removed_sender is not None:
            await asyncio.gather(removed_sender, return_exceptions=True)

    asyncio.run(go())
    assert len(ws.sent) == 1
    assert "TASK_STATE" in ws.sent[0]


def test_emit_drops_dead_subscriber(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)

    class DeadWS:
        async def send_text(self, text):
            raise RuntimeError("connection closed")

    ws = DeadWS()

    async def go():
        sch.add_subscriber("b1", ws)
        sender, _ = sch.start_subscriber("b1", ws, [])
        await sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={}, ts=0))
        await asyncio.wait_for(sender, timeout=1)

    asyncio.run(go())
    assert ws not in sch._subscribers["b1"]


def test_emit_drops_slow_subscriber_when_bounded_queue_is_full(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path), subscriber_queue_max=1)
    first_event = WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={}, ts=0)
    sch._seq_counters["b1"] = 1
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)
    sch._buffers["b1"].append(first_event)

    class SlowWS:
        def __init__(self):
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()

        async def send_text(self, _text):
            self.send_started.set()
            await self.release_send.wait()

    ws = SlowWS()

    async def go():
        sch.add_subscriber("b1", ws)
        sender, _ = sch.start_subscriber("b1", ws, [first_event])
        try:
            await asyncio.wait_for(ws.send_started.wait(), timeout=1)
            await sch._emit(
                "b1", WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={}, ts=0)
            )
            await sch._emit(
                "b1", WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={}, ts=0)
            )
        finally:
            ws.release_send.set()
        await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(go())

    assert ws not in sch._subscribers["b1"]
    assert [event.seq for event in sch._buffers["b1"]] == [0, 1, 2]


def test_cancel_unknown_batch_does_not_leak_cancelled_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    result = asyncio.run(sch.cancel_batch("b1"))
    assert result["cancelled"] is False
    assert "b1" not in sch._cancelled


def test_cancel_completed_batch_is_idempotent_without_changing_terminal_state():
    async def go():
        scheduler, repo = make_recording_scheduler()
        response = await scheduler.submit_batch(files=[], concurrency=1)

        result = await scheduler.cancel_batch(response.batch_id)

        assert result == {"batch_id": response.batch_id, "cancelled": True}
        assert repo.get_batch(response.batch_id)["status"] == "COMPLETED"
        assert response.batch_id not in scheduler._cancelled
        done_events = [
            event
            for event in scheduler.replay_events(response.batch_id, -1)
            if event.event == "BATCH_DONE"
        ]
        assert [event.payload["status"] for event in done_events] == ["COMPLETED"]

    asyncio.run(go())


def test_submit_batch_awaits_all_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path), max_global_concurrency=4)

    async def go():
        result = await sch.submit_batch(
            files=[str(Path("tests/fixtures/sample.md").resolve())] * 3, concurrency=3, priority=0
        )
        ctx = sch._batches.get(result.batch_id)
        await asyncio.wait_for(ctx.done.wait(), timeout=5)
        return ctx.completed

    completed = asyncio.run(go())
    assert completed >= 3


def test_cancel_batch_completes_waiting_and_running_tasks_exactly_once():
    async def go():
        loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        release_first = threading.Event()
        parse_calls = []
        parse_calls_lock = threading.Lock()

        def controlled_parse(_file_path, _force, task_id, _batch_id):
            with parse_calls_lock:
                parse_calls.append(task_id)
            loop.call_soon_threadsafe(first_started.set)
            if not release_first.wait(timeout=5):
                raise RuntimeError("test did not release running parse")

        clock = FakeClock()
        scheduler, repo = make_recording_scheduler(
            parse_file=controlled_parse,
            max_global_concurrency=3,
            clock=clock,
            buffer_ttl_sec=10,
        )
        response = await scheduler.submit_batch(
            files=["first.pdf", "second.pdf", "third.pdf"],
            concurrency=1,
        )
        batch_id = response.batch_id
        ctx = scheduler._batches[batch_id]
        await asyncio.wait_for(first_started.wait(), timeout=1)
        handles = dict(getattr(scheduler, "_batch_tasks", {}).get(batch_id, {}))

        try:
            cancelled = await scheduler.cancel_batch(batch_id)
            assert cancelled == {"batch_id": batch_id, "cancelled": True}
            assert ctx.completed == 2
            assert repo.get_batch(batch_id)["completed_tasks"] == 2
            assert repo.get_batch(batch_id)["status"] == "RUNNING"
            clock.advance(10)
            assert not scheduler.is_batch_gone(batch_id)

            running_task_id = parse_calls[0]
            assert len(handles) == 3
            assert handles[running_task_id].cancelling() == 0
            waiting_handles = [
                handle for task_id, handle in handles.items() if task_id != running_task_id
            ]
            assert all(handle.cancelling() > 0 for handle in waiting_handles)
        finally:
            release_first.set()

        await asyncio.wait_for(ctx.done.wait(), timeout=1)
        await asyncio.gather(*handles.values(), return_exceptions=True)

        batch = repo.get_batch(batch_id)
        assert batch["status"] == "CANCELLED"
        assert batch["completed_tasks"] == batch["total_tasks"] == 3
        assert ctx.completed == ctx.total == 3

        events = scheduler.replay_events(batch_id, since=-1)
        task_events = [event for event in events if event.event == "TASK_STATE"]
        done_events = [event for event in events if event.event == "BATCH_DONE"]
        assert len(task_events) == 3
        assert {event.task_id for event in task_events} == set(response.task_ids)
        assert {event.payload["status"] for event in task_events} == {"CANCELLED"}
        assert [event.payload["status"] for event in done_events] == ["CANCELLED"]
        assert [event.seq for event in events] == list(range(len(events)))
        assert parse_calls == [running_task_id]
        assert scheduler._batch_tasks.get(batch_id, {}) == {}
        assert scheduler._thread_task_ids.get(batch_id, set()) == set()

        clock.advance(10)
        assert scheduler.is_batch_gone(batch_id)
        assert_batch_memory_purged(scheduler, batch_id)

    asyncio.run(go())


def test_cancelled_task_checks_state_before_global_semaphore():
    async def go():
        parse_calls = []
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: parse_calls.append(True)
        )
        response = await scheduler.submit_batch(files=["waiting.pdf"], concurrency=1)
        batch_id = response.batch_id
        ctx = scheduler._batches[batch_id]
        scheduler._cancelled.add(batch_id)
        handles = tuple(scheduler._batch_tasks[batch_id].values())

        await asyncio.gather(*handles, return_exceptions=True)

        assert parse_calls == []
        assert ctx.completed == 1
        assert repo.get_batch(batch_id)["status"] == "CANCELLED"
        task_events = [
            event for event in scheduler.replay_events(batch_id, -1) if event.event == "TASK_STATE"
        ]
        assert [event.payload["status"] for event in task_events] == ["CANCELLED"]

    asyncio.run(go())


def test_cancelled_task_checks_state_after_batch_semaphore():
    async def go():
        parse_calls = []
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: parse_calls.append(True)
        )
        response = await scheduler.submit_batch(files=["waiting.pdf"], concurrency=1)
        batch_id = response.batch_id
        ctx = scheduler._batches[batch_id]

        class CancelOnEnter:
            async def __aenter__(self):
                scheduler._cancelled.add(batch_id)

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        ctx.sem = CancelOnEnter()
        handles = tuple(scheduler._batch_tasks[batch_id].values())

        await asyncio.gather(*handles, return_exceptions=True)

        assert parse_calls == []
        assert ctx.completed == 1
        assert repo.get_batch(batch_id)["status"] == "CANCELLED"
        task_events = [
            event for event in scheduler.replay_events(batch_id, -1) if event.event == "TASK_STATE"
        ]
        assert [event.payload["status"] for event in task_events] == ["CANCELLED"]

    asyncio.run(go())


def test_empty_batch_finishes_immediately():
    async def go():
        scheduler, repo = make_recording_scheduler()
        response = await scheduler.submit_batch(files=[], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        assert ctx.done.is_set()
        assert ctx.completed == ctx.total == 0
        batch = repo.get_batch(response.batch_id)
        assert batch["status"] == "COMPLETED"
        assert batch["completed_tasks"] == batch["total_tasks"] == 0
        events = scheduler.replay_events(response.batch_id, since=-1)
        assert [event.event for event in events] == ["BATCH_STATE", "BATCH_DONE"]
        assert events[-1].payload["status"] == "COMPLETED"

    asyncio.run(go())


def test_terminal_batch_replays_until_ttl_then_purges_all_memory():
    async def go():
        clock = FakeClock()
        scheduler, repo = make_recording_scheduler(
            clock=clock,
            buffer_ttl_sec=10,
            max_history_batches=100,
        )
        response = await scheduler.submit_batch(files=[], concurrency=1)
        batch_id = response.batch_id

        clock.advance(9)
        assert not scheduler.is_batch_gone(batch_id)
        assert [event.event for event in scheduler.replay_events(batch_id, -1)] == [
            "BATCH_STATE",
            "BATCH_DONE",
        ]

        clock.advance(1)
        assert scheduler.replay_events(batch_id, -1) == []
        assert scheduler.is_batch_gone(batch_id)
        assert_batch_memory_purged(scheduler, batch_id)
        assert repo.get_batch(batch_id)["status"] == "COMPLETED"

    asyncio.run(go())


def test_history_limit_purges_oldest_terminal_batches():
    async def go():
        clock = FakeClock()
        scheduler, _repo = make_recording_scheduler(
            clock=clock,
            buffer_ttl_sec=1000,
            max_history_batches=2,
        )
        batch_ids = []
        for _ in range(3):
            response = await scheduler.submit_batch(files=[], concurrency=1)
            batch_ids.append(response.batch_id)
            clock.advance(1)

        assert scheduler.is_batch_gone(batch_ids[0])
        assert not scheduler.is_batch_gone(batch_ids[1])
        assert not scheduler.is_batch_gone(batch_ids[2])
        assert set(scheduler._batches) == set(batch_ids[1:])
        assert_batch_memory_purged(scheduler, batch_ids[0])

    asyncio.run(go())


def test_purge_preserves_subscriber_then_cancels_stale_sender_safely():
    async def go():
        clock = FakeClock()
        scheduler, _repo = make_recording_scheduler(
            clock=clock,
            buffer_ttl_sec=1,
            max_history_batches=100,
        )
        response = await scheduler.submit_batch(files=[], concurrency=1)
        batch_id = response.batch_id

        class BlockingWS:
            def __init__(self):
                self.send_started = asyncio.Event()

            async def send_text(self, _text):
                self.send_started.set()
                await asyncio.Event().wait()

        ws = BlockingWS()
        replay = scheduler.replay_events(batch_id, since=-1)
        scheduler.add_subscriber(batch_id, ws)
        sender, _ = scheduler.start_subscriber(batch_id, ws, replay)
        await asyncio.wait_for(ws.send_started.wait(), timeout=1)

        clock.advance(1)
        assert not scheduler.is_batch_gone(batch_id)
        assert batch_id in scheduler._buffers

        removed_sender = scheduler.remove_subscriber(batch_id, ws)
        if removed_sender is not None:
            await asyncio.gather(removed_sender, return_exceptions=True)
        assert scheduler.is_batch_gone(batch_id)
        await asyncio.gather(sender, return_exceptions=True)

        assert sender.cancelled()
        assert scheduler._cleanup_tasks == set()
        assert_batch_memory_purged(scheduler, batch_id)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and task.get_name().startswith("ws-sender-")
        ]

    asyncio.run(go())


def test_delete_queued_task_cancels_worker_before_start_and_purges_outputs(tmp_path):
    async def go():
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        executor_started = threading.Event()
        release_executor = threading.Event()
        parse_calls = []
        output_root = tmp_path / "outputs"
        output_root.mkdir()

        def occupy_executor():
            executor_started.set()
            assert release_executor.wait(timeout=5)

        def purge_task(task_id):
            shutil.rmtree(output_root / task_id)

        blocker = loop.run_in_executor(None, occupy_executor)
        await wait_until(executor_started.is_set)
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: parse_calls.append(True),
            purge_task=purge_task,
            max_global_concurrency=1,
        )
        response = await scheduler.submit_batch(files=["queued.pdf"], concurrency=1)
        batch_id = response.batch_id
        task_id = response.task_ids[0]
        task_output = output_root / task_id
        task_output.mkdir()
        (task_output / "partial.md").write_text("partial", encoding="utf-8")
        ctx = scheduler._batches[batch_id]
        await wait_until(lambda: ctx.task_phases.get(task_id) == "QUEUED")

        try:
            result = await asyncio.wait_for(scheduler.delete_task(task_id), timeout=1)
        finally:
            release_executor.set()
            await blocker

        assert result == {"task_id": task_id, "purged": True}
        assert parse_calls == []
        assert repo.get_task(task_id) is None
        assert not task_output.exists()

    asyncio.run(go())


def test_delete_active_task_waits_for_worker_then_purges_late_db_and_files(tmp_path):
    async def go():
        loop = asyncio.get_running_loop()
        parse_started = asyncio.Event()
        release_parse = threading.Event()
        parse_finished = threading.Event()
        purge_started = threading.Event()
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        repo = RecordingBatchRepo()

        def active_parse(_file_path, _force, task_id, _batch_id):
            task_output = output_root / task_id
            task_output.mkdir()
            (task_output / "0.raw.md").write_text("raw", encoding="utf-8")
            loop.call_soon_threadsafe(parse_started.set)
            assert release_parse.wait(timeout=5)
            (task_output / "merged.md").write_text("late output", encoding="utf-8")
            repo.update_task_status(task_id, "COMPLETED")
            parse_finished.set()

        def purge_task(task_id):
            purge_started.set()
            shutil.rmtree(output_root / task_id)

        scheduler, _ = make_recording_scheduler(
            parse_file=active_parse,
            purge_task=purge_task,
            repo=repo,
        )
        response = await scheduler.submit_batch(files=["running.pdf"], concurrency=1)
        batch_id = response.batch_id
        task_id = response.task_ids[0]
        await asyncio.wait_for(parse_started.wait(), timeout=1)

        deletion = asyncio.create_task(scheduler.delete_task(task_id))
        ctx = scheduler._batches[batch_id]
        await wait_until(lambda: task_id in ctx.externally_cancelled_task_ids)
        assert not deletion.done()
        assert not purge_started.is_set()
        release_parse.set()
        result = await asyncio.wait_for(deletion, timeout=1)

        assert result == {"task_id": task_id, "purged": True}
        assert parse_finished.is_set()
        assert purge_started.is_set()
        assert repo.get_task(task_id) is None
        assert not (output_root / task_id).exists()

    asyncio.run(go())


def test_delete_active_real_task_leaves_no_sqlite_row_or_output_directory(tmp_path, monkeypatch):
    parse_started = threading.Event()
    release_parse = threading.Event()
    parser_finished = threading.Event()
    real_parse = MarkItDownAdapter.parse

    def blocking_parse(parser, file_path):
        parse_started.set()
        assert release_parse.wait(timeout=5)
        try:
            return real_parse(parser, file_path)
        finally:
            parser_finished.set()

    monkeypatch.setattr(MarkItDownAdapter, "parse", blocking_parse)

    async def go():
        base = tmp_path / "data"
        db_path = tmp_path / "serve.db"
        connections = []

        def factory():
            conn = init_db(str(db_path))
            apply_serve_schema(conn)
            connections.append(conn)
            return Orchestrator(
                repo=Repository(conn),
                fs=FsLayout(base_dir=str(base)),
                llm=StubLLMClient(),
                db_path=str(db_path),
            )

        source = tmp_path / "course.md"
        source.write_text("# 第一章\n\n真实删除竞态。\n", encoding="utf-8")
        scheduler = Scheduler(factory)
        response = await scheduler.submit_batch(files=[str(source)], concurrency=1)
        task_id = response.task_ids[0]
        batch_id = response.batch_id
        await wait_until(parse_started.is_set)

        deletion = asyncio.create_task(scheduler.delete_task(task_id))
        ctx = scheduler._batches[batch_id]
        await wait_until(lambda: task_id in ctx.externally_cancelled_task_ids)
        assert not deletion.done()
        release_parse.set()
        result = await asyncio.wait_for(deletion, timeout=2)

        assert result == {"task_id": task_id, "purged": True}
        assert parser_finished.is_set()
        assert scheduler._query_orch.repo.get_task(task_id) is None
        assert not (base / "tasks" / task_id).exists()
        await scheduler.shutdown()
        for conn in connections:
            conn.close()

    asyncio.run(go())


def test_delete_waits_for_independent_resume_lock_and_cannot_be_revived(tmp_path):
    class BlockingParser:
        def __init__(self):
            self.entered = threading.Barrier(2)
            self.release = threading.Event()

        def parse(self, path):
            self.entered.wait(timeout=2)
            assert self.release.wait(timeout=3)
            return Path(path).read_text(encoding="utf-8")

    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    resume_conn = init_db(str(db_path))
    apply_serve_schema(resume_conn)
    resume_repo = Repository(resume_conn)
    fs = FsLayout(base_dir=str(base))
    snapshot_path = tmp_path / "delete-resume.snapshot.md"
    snapshot_path.write_text("## 第一章\n\nDELETE_RESUME_BODY\n", encoding="utf-8")
    task = Task(
        id="independent-delete-resume",
        file_path=str(tmp_path / "missing-original.md"),
        snapshot_path=str(snapshot_path),
        file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        status="FAILED",
        created_at=1,
        updated_at=1,
    )
    resume_repo.create_task(task)
    parser = BlockingParser()
    resume_orch = Orchestrator(
        repo=resume_repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    resume_orch.parser = parser
    resume_results = []
    resume_errors = []
    delete_results = []
    delete_errors = []

    def run_resume():
        try:
            resume_results.append(resume_orch.resume(task.id))
        except BaseException as error:
            resume_errors.append(error)

    resume_thread = threading.Thread(target=run_resume)
    resume_thread.start()
    parser.entered.wait(timeout=2)
    stable_lock_path = base / ".locks" / f"{task.id}.lock"
    assert stable_lock_path.is_file()
    assert not (base / "tasks" / task.id / ".resume.lock").exists()
    lock_inode = stable_lock_path.stat().st_ino

    delete_conn = init_db(str(db_path))
    apply_serve_schema(delete_conn)
    delete_orch = Orchestrator(
        repo=Repository(delete_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = Scheduler(lambda: delete_orch)
    purge_entered = threading.Barrier(2)
    delete_done = threading.Event()
    real_try_purge = delete_orch.try_purge
    observed_first_purge = False

    def observed_try_purge(task_id):
        nonlocal observed_first_purge
        if not observed_first_purge:
            observed_first_purge = True
            purge_entered.wait(timeout=2)
        return real_try_purge(task_id)

    delete_orch.try_purge = observed_try_purge

    def run_delete():
        try:
            delete_results.append(asyncio.run(scheduler.delete_task(task.id)))
        except BaseException as error:
            delete_errors.append(error)
        finally:
            delete_done.set()

    delete_thread = threading.Thread(target=run_delete)
    delete_thread.start()
    purge_entered.wait(timeout=2)
    delete_finished_while_resume_active = delete_done.wait(timeout=0.5)
    parser.release.set()
    resume_thread.join(timeout=3)
    delete_thread.join(timeout=3)

    assert not resume_thread.is_alive()
    assert not delete_thread.is_alive()
    assert delete_finished_while_resume_active is False
    assert resume_errors == []
    assert resume_results and resume_results[0]["status"] == "COMPLETED"
    assert delete_errors == []
    assert delete_results == [{"task_id": task.id, "purged": True}]
    assert delete_orch.repo.get_task(task.id) is None
    assert not (base / "tasks" / task.id).exists()
    assert not os.path.lexists(snapshot_path)
    assert stable_lock_path.is_file()
    assert stable_lock_path.stat().st_ino == lock_inode
    asyncio.run(scheduler.shutdown())
    resume_conn.close()
    delete_conn.close()


def test_spawned_initial_parse_cannot_rebuild_outputs_after_delete_returns(tmp_path):
    context = multiprocessing.get_context("spawn")
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    source = tmp_path / "spawned-initial.md"
    source.write_text("## 第一章\n\nSPAWNED_INITIAL_PARSE\n", encoding="utf-8")
    task_id = "spawned-initial-delete"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_task(
        Task(
            id=task_id,
            file_path=str(source),
            snapshot_path="",
            file_sha256="",
            status="WAITING",
            created_at=1,
            updated_at=1,
        )
    )
    parse_entered = context.Barrier(2)
    release_parse = context.Event()
    delete_started = context.Event()
    delete_done = context.Event()
    result_queue = context.Queue()
    parse_process = context.Process(
        target=_run_initial_parse_process,
        args=(
            str(db_path),
            str(base),
            str(source),
            task_id,
            parse_entered,
            release_parse,
            result_queue,
        ),
    )
    delete_process = context.Process(
        target=_run_delete_process,
        args=(
            str(db_path),
            str(base),
            task_id,
            delete_started,
            delete_done,
            result_queue,
        ),
    )

    parse_process.start()
    try:
        parse_entered.wait(timeout=10)
        active_task = repo.get_task(task_id)
        assert active_task is not None
        snapshot_path = Path(active_task.snapshot_path)
        assert snapshot_path.is_file()
        delete_process.start()
        assert delete_started.wait(timeout=10)
        delete_finished_while_parse_active = delete_done.wait(timeout=0.75)
    finally:
        release_parse.set()
        parse_process.join(timeout=15)
        if delete_process.pid is not None:
            delete_process.join(timeout=15)
        if parse_process.is_alive():
            parse_process.terminate()
            parse_process.join(timeout=5)
        if delete_process.is_alive():
            delete_process.terminate()
            delete_process.join(timeout=5)

    process_results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
    result_queue.close()
    result_queue.join_thread()
    by_kind = {result[0]: result for result in process_results}
    assert delete_finished_while_parse_active is False
    assert parse_process.exitcode == 0
    assert delete_process.exitcode == 0
    assert by_kind["parse"][1] == "ok"
    assert by_kind["parse"][2]["status"] == "COMPLETED"
    assert by_kind["delete"] == (
        "delete",
        "ok",
        {"task_id": task_id, "purged": True},
    )
    assert repo.get_task(task_id) is None
    assert not os.path.lexists(base / "tasks" / task_id)
    assert not os.path.lexists(snapshot_path)
    conn.close()


def test_cache_materialization_holds_task_lock_until_publication_finishes(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    source = tmp_path / "cache-materialization.md"
    source.write_text("## 第一章\n\nCACHE_MATERIALIZATION_LOCK\n", encoding="utf-8")
    first_conn = init_db(str(db_path))
    apply_serve_schema(first_conn)
    first_repo = Repository(first_conn)
    fs = FsLayout(base_dir=str(base))
    first_orch = Orchestrator(
        repo=first_repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    source_result = first_orch.parse_file(str(source), force=True)
    assert source_result["status"] == "COMPLETED"
    target_id = "cache-materialization-delete"
    first_repo.create_task(
        Task(
            id=target_id,
            file_path=str(source),
            snapshot_path="",
            file_sha256="",
            status="WAITING",
            created_at=1,
            updated_at=1,
        )
    )
    publication_entered = threading.Barrier(2)
    release_publication = threading.Event()
    real_publish = first_orch._publish_staging_directory
    materialization_results = []
    materialization_errors = []

    def blocking_publish(staging_dir, accepted_task_id):
        real_publish(staging_dir, accepted_task_id)
        publication_entered.wait(timeout=3)
        assert release_publication.wait(timeout=3)

    first_orch._publish_staging_directory = blocking_publish

    def run_materialization():
        try:
            materialization_results.append(first_orch.parse_file(str(source), task_id=target_id))
        except BaseException as error:
            materialization_errors.append(error)

    materialization_thread = threading.Thread(target=run_materialization)
    materialization_thread.start()
    publication_entered.wait(timeout=3)
    second_conn = init_db(str(db_path))
    apply_serve_schema(second_conn)
    second_orch = Orchestrator(
        repo=Repository(second_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = Scheduler(lambda: second_orch)
    delete_done = threading.Event()
    delete_results = []
    delete_errors = []

    def run_delete():
        try:
            delete_results.append(asyncio.run(scheduler.delete_task(target_id)))
        except BaseException as error:
            delete_errors.append(error)
        finally:
            delete_done.set()

    delete_thread = threading.Thread(target=run_delete)
    delete_thread.start()
    delete_finished_while_publication_active = delete_done.wait(timeout=0.5)
    release_publication.set()
    materialization_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert delete_finished_while_publication_active is False
    assert not materialization_thread.is_alive()
    assert not delete_thread.is_alive()
    assert materialization_errors == []
    assert materialization_results and materialization_results[0]["cached"] is True
    assert delete_errors == []
    assert delete_results == [{"task_id": target_id, "purged": True}]
    assert second_orch.repo.get_task(target_id) is None
    assert not os.path.lexists(base / "tasks" / target_id)
    asyncio.run(scheduler.shutdown())
    first_conn.close()
    second_conn.close()


def test_scheduler_startup_skips_active_cache_staging_held_by_task_lock(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    source = tmp_path / "active-cache-staging.md"
    source.write_text("## 第一章\n\nACTIVE_CACHE_STAGING_LOCK\n", encoding="utf-8")
    first_conn = init_db(str(db_path))
    apply_serve_schema(first_conn)
    first_repo = Repository(first_conn)
    fs = FsLayout(base_dir=str(base))
    first_orch = Orchestrator(
        repo=first_repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    source_result = first_orch.parse_file(str(source), force=True)
    assert source_result["status"] == "COMPLETED"
    target_id = "active-cache-staging-lock"
    first_repo.create_task(
        Task(
            id=target_id,
            file_path=str(source),
            snapshot_path="",
            file_sha256="",
            status="WAITING",
            created_at=1,
            updated_at=1,
        )
    )
    publication_entered = threading.Event()
    release_publication = threading.Event()
    staging_paths = []
    real_publish = first_orch._publish_staging_directory
    materialization_results = []
    materialization_errors = []

    def paused_publish(staging_dir, accepted_task_id):
        staging_paths.append(staging_dir)
        publication_entered.set()
        assert release_publication.wait(timeout=3)
        real_publish(staging_dir, accepted_task_id)

    first_orch._publish_staging_directory = paused_publish

    def run_materialization():
        try:
            materialization_results.append(first_orch.parse_file(str(source), task_id=target_id))
        except BaseException as error:
            materialization_errors.append(error)

    materialization_thread = threading.Thread(target=run_materialization)
    materialization_thread.start()
    assert publication_entered.wait(timeout=3)
    second_conn = init_db(str(db_path))
    apply_serve_schema(second_conn)
    second_orch = Orchestrator(
        repo=Repository(second_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    scheduler = None
    try:
        scheduler = Scheduler(lambda: second_orch)
        assert staging_paths and staging_paths[0].is_dir()
        assert (base / ".locks" / f"{target_id}.lock").is_file()
    finally:
        release_publication.set()
    materialization_thread.join(timeout=5)

    assert not materialization_thread.is_alive()
    assert materialization_errors == []
    assert materialization_results and materialization_results[0]["cached"] is True
    if scheduler is not None:
        asyncio.run(scheduler.shutdown())
    first_conn.close()
    second_conn.close()


def test_scheduler_startup_rechecks_staging_identity_after_task_lock(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    fs = FsLayout(base_dir=str(base))
    staging = Path(fs.tasks_dir()) / ".staging-identity.first.tmp"
    staging.mkdir()
    (staging / "original.md").write_text("original", encoding="utf-8")
    displaced = Path(fs.tasks_dir()) / ".staging-identity.displaced"
    replacement_marker = staging / "replacement.md"
    orch = Orchestrator(
        repo=Repository(conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    real_try_cleanup_task_lock = Scheduler._try_cleanup_task_lock
    replaced = False

    def replace_staging_after_lock(scheduler, task_id):
        nonlocal replaced
        lock_fd = real_try_cleanup_task_lock(scheduler, task_id)
        if lock_fd is not None and task_id == "staging-identity" and not replaced:
            replaced = True
            staging.rename(displaced)
            staging.mkdir()
            replacement_marker.write_text("replacement", encoding="utf-8")
        return lock_fd

    monkeypatch.setattr(
        Scheduler,
        "_try_cleanup_task_lock",
        replace_staging_after_lock,
    )
    scheduler = Scheduler(lambda: orch)

    assert replaced is True
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    assert (displaced / "original.md").read_text(encoding="utf-8") == "original"
    assert (base / ".locks" / "staging-identity.lock").is_file()
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_scheduler_startup_does_not_delete_waiting_replacement_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    task_id = "waiting-identity"
    repo.create_task(
        Task(
            id=task_id,
            file_path="/tmp/waiting.pdf",
            snapshot_path="",
            file_sha256="waiting",
            status="WAITING",
            created_at=1,
            updated_at=1,
        )
    )
    task = Path(fs.task_dir(task_id))
    (task / "original.md").write_text("original", encoding="utf-8")
    displaced = task.with_name(f"{task_id}-displaced")
    replacement_marker = task / "replacement.md"
    orch = Orchestrator(
        repo=repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(db_path),
    )
    real_try_cleanup_task_lock = Scheduler._try_cleanup_task_lock
    swapped = False

    def replace_waiting_after_lock(scheduler: Scheduler, candidate: str) -> int | None:
        nonlocal swapped
        lock_fd = real_try_cleanup_task_lock(scheduler, candidate)
        if lock_fd is not None and candidate == task_id and not swapped:
            task.rename(displaced)
            task.mkdir(mode=0o700)
            replacement_marker.write_text("replacement", encoding="utf-8")
            swapped = True
        return lock_fd

    monkeypatch.setattr(Scheduler, "_try_cleanup_task_lock", replace_waiting_after_lock)

    scheduler = Scheduler(lambda: orch)

    assert swapped
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    assert (displaced / "original.md").read_text(encoding="utf-8") == "original"
    assert repo.get_task(task_id) is not None
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_spawned_concurrent_deletes_share_first_lock_inode(tmp_path):
    context = multiprocessing.get_context("spawn")
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    snapshot_path = tmp_path / "double-delete.snapshot.md"
    snapshot_path.write_text("double delete", encoding="utf-8")
    task_id = "spawned-double-delete"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    repo.create_task(
        Task(
            id=task_id,
            file_path=str(tmp_path / "missing.md"),
            snapshot_path=str(snapshot_path),
            file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            status="FAILED",
            created_at=1,
            updated_at=1,
        )
    )
    task_dir = base / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "partial.md").write_text("partial", encoding="utf-8")
    start_barrier = context.Barrier(3)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_run_barrier_delete_process,
            args=(str(db_path), str(base), task_id, start_barrier, result_queue),
        )
        for _ in range(2)
    ]
    assert not (base / ".locks").exists()
    for process in processes:
        process.start()
    try:
        start_barrier.wait(timeout=10)
    finally:
        for process in processes:
            process.join(timeout=15)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
    result_queue.close()
    result_queue.join_thread()
    assert all(process.exitcode == 0 for process in processes)
    assert all(result[0] == "ok" for result in results), results
    assert sorted(result[1]["purged"] for result in results) == [False, True]
    assert len({result[2] for result in results}) == 1
    lock_path = base / ".locks" / f"{task_id}.lock"
    assert lock_path.is_file()
    assert lock_path.stat().st_ino == results[0][2]
    assert repo.get_task(task_id) is None
    assert not os.path.lexists(task_dir)
    assert not os.path.lexists(snapshot_path)
    conn.close()


def test_two_busy_deletes_do_not_starve_unlocked_third_delete(tmp_path):
    base = tmp_path / "data"
    db_path = tmp_path / "serve.db"
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    fs = FsLayout(base_dir=str(base))
    task_ids = ["busy-delete-one", "busy-delete-two", "free-delete-three"]
    for task_id in task_ids:
        snapshot_path = tmp_path / f"{task_id}.snapshot.md"
        snapshot_path.write_text(task_id, encoding="utf-8")
        repo.create_task(
            Task(
                id=task_id,
                file_path=str(tmp_path / f"{task_id}.md"),
                snapshot_path=str(snapshot_path),
                file_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                status="FAILED",
                created_at=1,
                updated_at=1,
            )
        )
        task_dir = Path(fs.task_dir(task_id))
        (task_dir / "partial.md").write_text(task_id, encoding="utf-8")

    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))
    scheduler = Scheduler(lambda: orch)
    held_lock_fds = [orch._try_open_resume_lock(task_id) for task_id in task_ids[:2]]
    assert all(lock_fd is not None for lock_fd in held_lock_fds)
    attempts_entered = threading.Barrier(3)
    attempt_guard = threading.Lock()
    observed_task_ids = set()
    real_open_lock = orch._try_open_resume_lock

    def observed_open_lock(task_id, *, blocking=False):
        should_wait = False
        if task_id in task_ids[:2]:
            with attempt_guard:
                if task_id not in observed_task_ids:
                    observed_task_ids.add(task_id)
                    should_wait = True
        if should_wait:
            attempts_entered.wait(timeout=5)
        return real_open_lock(task_id, blocking=blocking)

    orch._try_open_resume_lock = observed_open_lock
    delete_results = []
    delete_errors = []

    def run_blocked_delete(task_id):
        try:
            delete_results.append(asyncio.run(scheduler.delete_task(task_id)))
        except BaseException as error:
            delete_errors.append(error)

    delete_threads = [
        threading.Thread(target=run_blocked_delete, args=(task_id,)) for task_id in task_ids[:2]
    ]
    for thread in delete_threads:
        thread.start()
    attempts_entered.wait(timeout=5)

    async def delete_third():
        return await asyncio.wait_for(scheduler.delete_task(task_ids[2]), timeout=0.75)

    try:
        try:
            third_result = asyncio.run(delete_third())
        except TimeoutError:
            third_result = None
    finally:
        for lock_fd in held_lock_fds:
            assert lock_fd is not None
            orch._close_resume_lock(lock_fd)
    for thread in delete_threads:
        thread.join(timeout=5)

    assert third_result == {"task_id": task_ids[2], "purged": True}
    assert all(not thread.is_alive() for thread in delete_threads)
    assert delete_errors == []
    assert sorted(result["task_id"] for result in delete_results) == sorted(task_ids[:2])
    assert all(result["purged"] is True for result in delete_results)
    asyncio.run(scheduler.shutdown())
    conn.close()


def test_cancel_queued_to_thread_before_worker_start_never_calls_parse_file():
    async def go():
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        executor_started = threading.Event()
        release_executor = threading.Event()
        parse_calls = []

        def occupy_executor():
            executor_started.set()
            release_executor.wait(timeout=5)

        blocker = loop.run_in_executor(None, occupy_executor)
        await wait_until(executor_started.is_set)
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: parse_calls.append(True),
            max_global_concurrency=1,
        )
        response = await scheduler.submit_batch(files=["queued.pdf"], concurrency=1)
        batch_id = response.batch_id
        task_id = response.task_ids[0]
        ctx = scheduler._batches[batch_id]

        def worker_is_queued():
            phases = getattr(ctx, "task_phases", {})
            return phases.get(task_id) == "QUEUED" or task_id in scheduler._thread_task_ids.get(
                batch_id, set()
            )

        await wait_until(worker_is_queued)
        await scheduler.cancel_batch(batch_id)
        release_executor.set()
        await blocker
        await asyncio.wait_for(ctx.done.wait(), timeout=1)
        await asyncio.gather(*scheduler._batch_tasks.get(batch_id, {}).values())

        assert parse_calls == []
        assert repo.get_batch(batch_id)["status"] == "CANCELLED"
        assert ctx.completed == 1

    asyncio.run(go())


def test_immediate_batch_cancel_completes_tasks_that_never_entered_coroutine():
    async def go():
        parse_calls = []
        scheduler, repo = make_recording_scheduler(
            parse_file=lambda *_args: parse_calls.append(True)
        )
        response = await scheduler.submit_batch(
            files=["never-started-1.pdf", "never-started-2.pdf"],
            concurrency=1,
        )
        ctx = scheduler._batches[response.batch_id]

        await scheduler.cancel_batch(response.batch_id)
        await asyncio.wait_for(ctx.done.wait(), timeout=1)

        assert parse_calls == []
        assert ctx.completed == ctx.total == 2
        assert repo.get_batch(response.batch_id)["status"] == "CANCELLED"

    asyncio.run(go())


def test_direct_prestart_task_cancel_is_completed_by_done_callback():
    async def go():
        scheduler, repo = make_recording_scheduler()
        response = await scheduler.submit_batch(files=["never-started.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        handle = scheduler._batch_tasks[response.batch_id][response.task_ids[0]]

        handle.cancel()
        await asyncio.wait_for(ctx.done.wait(), timeout=1)

        assert ctx.completed == 1
        assert repo.get_batch(response.batch_id)["status"] == "CANCELLED"

    asyncio.run(go())


def test_outer_task_cancel_waits_for_real_thread_before_completion_and_finalize():
    async def go():
        loop = asyncio.get_running_loop()
        parse_started = asyncio.Event()
        release_parse = threading.Event()
        parse_finished = threading.Event()

        def blocking_parse(*_args):
            loop.call_soon_threadsafe(parse_started.set)
            release_parse.wait(timeout=5)
            parse_finished.set()

        scheduler, repo = make_recording_scheduler(parse_file=blocking_parse)
        response = await scheduler.submit_batch(files=["running.pdf"], concurrency=1)
        batch_id = response.batch_id
        ctx = scheduler._batches[batch_id]
        handle = scheduler._batch_tasks[batch_id][response.task_ids[0]]
        await asyncio.wait_for(parse_started.wait(), timeout=1)

        handle.cancel()
        await asyncio.sleep(0.05)
        try:
            assert not handle.done()
            assert not ctx.done.is_set()
            assert ctx.completed == 0
            assert repo.get_batch(batch_id)["status"] == "RUNNING"
        finally:
            release_parse.set()

        await asyncio.gather(handle, return_exceptions=True)
        await asyncio.wait_for(ctx.done.wait(), timeout=1)
        assert parse_finished.is_set()
        assert ctx.completed == 1
        assert repo.get_batch(batch_id)["status"] == "CANCELLED"

    asyncio.run(go())


def test_scheduler_shutdown_waits_for_real_thread_before_terminal_state():
    async def go():
        loop = asyncio.get_running_loop()
        parse_started = asyncio.Event()
        release_parse = threading.Event()
        thread_finished = threading.Event()

        def blocking_parse(*_args):
            loop.call_soon_threadsafe(parse_started.set)
            release_parse.wait(timeout=5)
            thread_finished.set()

        scheduler, repo = make_recording_scheduler(parse_file=blocking_parse)
        response = await scheduler.submit_batch(files=["running.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        await asyncio.wait_for(parse_started.wait(), timeout=1)

        shutdown = asyncio.create_task(scheduler.shutdown())
        await asyncio.sleep(0.05)
        try:
            assert not shutdown.done()
            assert not thread_finished.is_set()
            assert not ctx.done.is_set()
            assert repo.get_batch(response.batch_id)["status"] == "RUNNING"
            with pytest.raises(RuntimeError, match="shutting down"):
                await scheduler.submit_batch(files=["rejected.pdf"], concurrency=1)
        finally:
            release_parse.set()

        await asyncio.wait_for(shutdown, timeout=1)
        assert thread_finished.is_set()
        assert ctx.done.is_set()
        assert repo.get_batch(response.batch_id)["status"] == "CANCELLED"

    asyncio.run(go())


def test_batch_semaphore_is_acquired_before_global_semaphore():
    async def go():
        loop = asyncio.get_running_loop()
        first_batch_started = asyncio.Event()
        second_batch_started = asyncio.Event()
        release_first_batch = threading.Event()

        def controlled_parse(file_path, *_args):
            if file_path.startswith("a-"):
                loop.call_soon_threadsafe(first_batch_started.set)
                release_first_batch.wait(timeout=5)
            else:
                loop.call_soon_threadsafe(second_batch_started.set)

        scheduler, _repo = make_recording_scheduler(
            parse_file=controlled_parse,
            max_global_concurrency=2,
        )
        first = await scheduler.submit_batch(
            files=["a-1.pdf", "a-2.pdf", "a-3.pdf"],
            concurrency=1,
        )
        await asyncio.wait_for(first_batch_started.wait(), timeout=1)
        second = await scheduler.submit_batch(files=["b-1.pdf"], concurrency=1)

        second_started_without_releasing_first = True
        try:
            await asyncio.wait_for(second_batch_started.wait(), timeout=0.25)
        except TimeoutError:
            second_started_without_releasing_first = False
        finally:
            release_first_batch.set()

        await asyncio.wait_for(scheduler._batches[first.batch_id].done.wait(), timeout=2)
        await asyncio.wait_for(scheduler._batches[second.batch_id].done.wait(), timeout=2)
        assert second_started_without_releasing_first

    asyncio.run(go())


def test_equal_priority_batches_dispatch_round_robin():
    async def go():
        started: list[str] = []
        releases = threading.Semaphore(0)

        def blocking_parse(file_path, *_args):
            started.append(file_path)
            if not releases.acquire(timeout=5):
                raise RuntimeError("test did not release parse")

        scheduler, _repo = make_recording_scheduler(
            parse_file=blocking_parse,
            max_global_concurrency=1,
        )
        first = await scheduler.submit_batch(
            files=["a-1.pdf", "a-2.pdf"], concurrency=1, priority=0
        )
        await wait_until(lambda: len(started) == 1)
        second = await scheduler.submit_batch(files=["b-1.pdf"], concurrency=1, priority=0)

        releases.release()
        await wait_until(lambda: len(started) >= 2)
        releases.release()
        await wait_until(lambda: len(started) >= 3)
        releases.release()
        await asyncio.wait_for(scheduler._batches[first.batch_id].done.wait(), timeout=1)
        await asyncio.wait_for(scheduler._batches[second.batch_id].done.wait(), timeout=1)

        assert started == ["a-1.pdf", "b-1.pdf", "a-2.pdf"]

    asyncio.run(go())


def test_priority_preempts_undispatched_work_but_low_priority_cannot_starve():
    async def go():
        started: list[str] = []
        releases = threading.Semaphore(0)

        def blocking_parse(file_path, *_args):
            started.append(file_path)
            if not releases.acquire(timeout=5):
                raise RuntimeError("test did not release parse")

        scheduler, _repo = make_recording_scheduler(
            parse_file=blocking_parse,
            max_global_concurrency=1,
        )
        low = await scheduler.submit_batch(
            files=["low-1.pdf", "low-2.pdf"], concurrency=1, priority=0
        )
        await wait_until(lambda: len(started) == 1)
        high = await scheduler.submit_batch(
            files=[f"high-{index}.pdf" for index in range(1, 6)],
            concurrency=1,
            priority=10,
        )

        for count in range(2, 8):
            releases.release()
            await wait_until(lambda count=count: len(started) >= count)
        releases.release()
        await asyncio.wait_for(scheduler._batches[low.batch_id].done.wait(), timeout=1)
        await asyncio.wait_for(scheduler._batches[high.batch_id].done.wait(), timeout=1)

        assert started[0] == "low-1.pdf"
        assert started[1:4] == ["high-1.pdf", "high-2.pdf", "high-3.pdf"]
        assert started[4] == "low-2.pdf"
        assert started[5:] == ["high-4.pdf", "high-5.pdf"]

    asyncio.run(go())


def test_priority_fairness_rotates_across_every_lower_priority_level():
    async def go():
        started: list[str] = []
        releases = threading.Semaphore(0)

        def blocking_parse(file_path, *_args):
            started.append(file_path)
            if not releases.acquire(timeout=5):
                raise RuntimeError("test did not release parse")

        scheduler, _repo = make_recording_scheduler(
            parse_file=blocking_parse,
            max_global_concurrency=1,
        )
        gate = await scheduler.submit_batch(files=["gate.pdf"], concurrency=1, priority=-1)
        await wait_until(lambda: started == ["gate.pdf"])
        high = await scheduler.submit_batch(
            files=[f"high-{index}.pdf" for index in range(1, 10)],
            concurrency=1,
            priority=10,
        )
        medium = await scheduler.submit_batch(
            files=[f"medium-{index}.pdf" for index in range(1, 10)],
            concurrency=1,
            priority=5,
        )
        low = await scheduler.submit_batch(files=["low.pdf"], concurrency=1, priority=0)

        for count in range(2, 21):
            releases.release()
            await wait_until(lambda count=count: len(started) >= count)
        releases.release()
        for response in (gate, high, medium, low):
            await asyncio.wait_for(
                scheduler._batches[response.batch_id].done.wait(),
                timeout=1,
            )

        assert started.index("low.pdf") <= 8

    asyncio.run(go())


def test_parse_failure_emits_failed_task_and_failed_batch():
    async def go():
        def fail_parse(*_args):
            raise RuntimeError("parse failed")

        scheduler, repo = make_recording_scheduler(parse_file=fail_parse)
        response = await scheduler.submit_batch(files=["broken.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        await asyncio.wait_for(ctx.done.wait(), timeout=1)

        events = scheduler.replay_events(response.batch_id, -1)
        task_states = [event.payload["status"] for event in events if event.event == "TASK_STATE"]
        done_states = [event.payload["status"] for event in events if event.event == "BATCH_DONE"]
        assert task_states == ["FAILED"]
        assert done_states == ["FAILED"]
        assert repo.get_batch(response.batch_id)["status"] == "FAILED"

    asyncio.run(go())


def test_cancelled_batch_status_has_priority_over_failed_task():
    async def go():
        loop = asyncio.get_running_loop()
        blocking_started = asyncio.Event()
        release_blocking = threading.Event()

        def parse(file_path, *_args):
            if file_path == "failed.pdf":
                raise RuntimeError("failed")
            loop.call_soon_threadsafe(blocking_started.set)
            release_blocking.wait(timeout=5)

        scheduler, repo = make_recording_scheduler(
            parse_file=parse,
            max_global_concurrency=2,
        )
        response = await scheduler.submit_batch(
            files=["failed.pdf", "blocking.pdf"],
            concurrency=2,
        )
        ctx = scheduler._batches[response.batch_id]
        await asyncio.wait_for(blocking_started.wait(), timeout=1)
        await wait_until(lambda: ctx.completed == 1)
        await scheduler.cancel_batch(response.batch_id)
        release_blocking.set()
        await asyncio.wait_for(ctx.done.wait(), timeout=1)

        assert repo.get_batch(response.batch_id)["status"] == "CANCELLED"
        done = [
            event.payload["status"]
            for event in scheduler.replay_events(response.batch_id, -1)
            if event.event == "BATCH_DONE"
        ]
        assert done == ["CANCELLED"]

    asyncio.run(go())


def test_absolute_progress_retry_after_execute_does_not_double_count():
    class RaiseOnceAfterExecuteRepo(RecordingBatchRepo):
        def __init__(self):
            super().__init__()
            self.raise_after_execute = True

        def set_batch_progress(self, batch_id, completed, status=None):
            super().set_batch_progress(batch_id, completed, status)
            if self.raise_after_execute:
                self.raise_after_execute = False
                raise OSError("connection interrupted after execute")

    async def go():
        repo = RaiseOnceAfterExecuteRepo()
        scheduler, _repo = make_recording_scheduler(repo=repo)
        response = await scheduler.submit_batch(files=["one.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        await asyncio.wait_for(ctx.done.wait(), timeout=1)

        batch = repo.get_batch(response.batch_id)
        assert batch["completed_tasks"] == 1
        assert batch["status"] == "COMPLETED"
        assert repo.set_progress_calls[:2] == [
            (response.batch_id, 1, None),
            (response.batch_id, 1, None),
        ]

    asyncio.run(go())


def test_persistent_progress_failure_never_finalizes_or_purges_and_can_be_retried():
    class FailingRepo(RecordingBatchRepo):
        def __init__(self):
            super().__init__()
            self.fail = True

        def set_batch_progress(self, batch_id, completed, status=None):
            if self.fail:
                raise OSError("disk unavailable")
            super().set_batch_progress(batch_id, completed, status)

        def increment_batch_completed(self, batch_id):
            if self.fail:
                raise OSError("disk unavailable")
            super().increment_batch_completed(batch_id)

    async def go():
        wall_clock = FakeClock(1_000)
        monotonic_clock = FakeClock(100)
        repo = FailingRepo()
        scheduler, _repo = make_recording_scheduler(
            repo=repo,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
            buffer_ttl_sec=1,
        )
        response = await scheduler.submit_batch(files=["one.pdf"], concurrency=1)
        batch_id = response.batch_id
        ctx = scheduler._batches[batch_id]
        handles = tuple(scheduler._batch_tasks[batch_id].values())
        await asyncio.gather(*handles, return_exceptions=True)

        assert ctx.completed == 0
        assert not ctx.finalized
        assert not ctx.done.is_set()
        assert repo.get_batch(batch_id)["status"] == "RUNNING"
        errors = [
            event
            for event in scheduler.replay_events(batch_id, -1)
            if event.event == "ERROR" and event.payload.get("code") == "BATCH_PERSISTENCE_FAILED"
        ]
        assert len(errors) == 1
        monotonic_clock.advance(10)
        assert not scheduler.is_batch_gone(batch_id)

        repo.fail = False
        await scheduler.retry_batch_persistence(batch_id)
        await asyncio.wait_for(ctx.done.wait(), timeout=1)
        assert repo.get_batch(batch_id)["completed_tasks"] == 1
        assert repo.get_batch(batch_id)["status"] == "COMPLETED"

    asyncio.run(go())


def test_persistence_retry_recovers_automatically_without_internal_call():
    class RecoveringRepo(RecordingBatchRepo):
        def __init__(self):
            super().__init__()
            self.failures_remaining = 3

        def set_batch_progress(self, batch_id, completed, status=None):
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise OSError("temporary disk failure")
            super().set_batch_progress(batch_id, completed, status)

    async def go():
        repo = RecoveringRepo()
        scheduler, _repo = make_recording_scheduler(repo=repo)
        response = await scheduler.submit_batch(files=["one.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]

        await asyncio.wait_for(ctx.done.wait(), timeout=2)

        batch = repo.get_batch(response.batch_id)
        assert batch["completed_tasks"] == 1
        assert batch["status"] == "COMPLETED"
        assert repo.failures_remaining == 0
        assert scheduler._persistence_retry_tasks == {}

    asyncio.run(go())


def test_shutdown_stops_persistence_retry_without_faking_disk_recovery():
    class PermanentlyFailingRepo(RecordingBatchRepo):
        def set_batch_progress(self, batch_id, completed, status=None):
            raise OSError("disk unavailable")

    async def go():
        repo = PermanentlyFailingRepo()
        scheduler, _repo = make_recording_scheduler(repo=repo)
        response = await scheduler.submit_batch(files=["one.pdf"], concurrency=1)
        ctx = scheduler._batches[response.batch_id]
        await asyncio.gather(*tuple(scheduler._batch_tasks[response.batch_id].values()))
        await wait_until(lambda: response.batch_id in scheduler._persistence_retry_tasks)

        await asyncio.wait_for(scheduler.shutdown(), timeout=1)

        assert scheduler._persistence_retry_tasks == {}
        assert not ctx.finalized
        assert not ctx.done.is_set()
        assert repo.get_batch(response.batch_id)["status"] == "RUNNING"

    asyncio.run(go())


def test_active_batch_capacity_is_bounded():
    from parsing_core.serving.scheduler import SchedulerCapacityError

    async def go():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def blocking_parse(*_args):
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=5)

        scheduler, _repo = make_recording_scheduler(
            parse_file=blocking_parse,
            max_active_batches=1,
        )
        first = await scheduler.submit_batch(files=["first.pdf"], concurrency=1)
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            try:
                await scheduler.submit_batch(files=["second.pdf"], concurrency=1)
            except SchedulerCapacityError as error:
                assert error.code == "ACTIVE_BATCH_LIMIT"
            else:
                raise AssertionError("second active batch was accepted")
        finally:
            release.set()
        await asyncio.wait_for(scheduler._batches[first.batch_id].done.wait(), timeout=1)

    asyncio.run(go())


def test_subscriber_capacity_is_bounded_and_real_disconnect_releases_slot():
    from parsing_core.serving.scheduler import SchedulerCapacityError

    class IdleWS:
        async def send_text(self, _text):
            await asyncio.Event().wait()

    async def go():
        scheduler, _repo = make_recording_scheduler(max_subscribers_per_batch=1)
        response = await scheduler.submit_batch(files=[], concurrency=1)
        first = IdleWS()
        second = IdleWS()
        scheduler.add_subscriber(response.batch_id, first)
        try:
            scheduler.add_subscriber(response.batch_id, second)
        except SchedulerCapacityError as error:
            assert error.code == "SUBSCRIBER_LIMIT"
        else:
            raise AssertionError("second subscriber was accepted")

        scheduler.remove_subscriber(response.batch_id, first)
        scheduler.add_subscriber(response.batch_id, second)
        assert second in scheduler._subscribers[response.batch_id]

    asyncio.run(go())


def test_subscribed_terminal_batches_cannot_grow_tracked_state_without_bound():
    from parsing_core.serving.scheduler import SchedulerCapacityError

    class IdleWS:
        async def send_text(self, _text):
            await asyncio.Event().wait()

    async def go():
        loop = asyncio.get_running_loop()
        parse_started = asyncio.Event()
        release_parse = threading.Event()

        def blocking_parse(*_args):
            loop.call_soon_threadsafe(parse_started.set)
            release_parse.wait(timeout=5)

        scheduler, _repo = make_recording_scheduler(
            parse_file=blocking_parse,
            max_active_batches=1,
            max_history_batches=1,
        )
        first = await scheduler.submit_batch(files=[], concurrency=1)
        scheduler.add_subscriber(first.batch_id, IdleWS())

        second = await scheduler.submit_batch(files=["blocking.pdf"], concurrency=1)
        await asyncio.wait_for(parse_started.wait(), timeout=1)
        scheduler.add_subscriber(second.batch_id, IdleWS())
        release_parse.set()
        await asyncio.wait_for(scheduler._batches[second.batch_id].done.wait(), timeout=1)

        with pytest.raises(SchedulerCapacityError) as captured:
            await scheduler.submit_batch(files=["third.pdf"], concurrency=1)
        assert captured.value.code == "TRACKED_BATCH_LIMIT"
        assert len(scheduler._batches) == 2

    asyncio.run(go())


def test_thread_progress_relay_has_fixed_pending_limit_under_event_storm():
    async def go():
        storm_sent = threading.Event()
        release_parse = threading.Event()
        gate = asyncio.Event()
        blocked_progress_calls = 0
        repo = RecordingBatchRepo()

        class StormOrchestrator:
            def __init__(self):
                self.repo = repo
                self.on_progress = None

            def parse_file(self, _file_path, _force, task_id, batch_id):
                assert self.on_progress is not None
                for index in range(1_000):
                    self.on_progress(task_id, "PROGRESS", {"index": index})
                storm_sent.set()
                release_parse.wait(timeout=5)

        scheduler = Scheduler(StormOrchestrator, progress_pending_max=8)
        original_emit = scheduler._emit

        async def blocked_emit(batch_id, event):
            nonlocal blocked_progress_calls
            if event.event == "PROGRESS":
                blocked_progress_calls += 1
                await gate.wait()
            await original_emit(batch_id, event)

        scheduler._emit = blocked_emit
        response = await scheduler.submit_batch(files=["storm.pdf"], concurrency=1)
        await wait_until(storm_sent.is_set)
        await asyncio.sleep(0.05)

        assert len(scheduler._progress_futures) <= 8
        assert blocked_progress_calls <= 8

        gate.set()
        release_parse.set()
        await asyncio.wait_for(scheduler._batches[response.batch_id].done.wait(), timeout=1)
        await wait_until(lambda: not scheduler._progress_futures)

    asyncio.run(go())


def test_progress_relay_does_not_deadlock_when_future_is_already_done(monkeypatch):
    scheduler, _repo = make_recording_scheduler(progress_pending_max=1)
    finished = threading.Event()

    def immediate_future(coroutine, _loop):
        coroutine.close()
        future = concurrent.futures.Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", immediate_future)

    def relay():
        loop = asyncio.new_event_loop()
        try:
            scheduler._relay_progress(
                loop,
                "missing-batch",
                "task-1",
                "PROGRESS",
                {},
            )
            finished.set()
        finally:
            loop.close()

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    thread.join(timeout=0.25)

    assert finished.is_set()
    assert scheduler._progress_futures == {}


def test_batch_done_rejects_late_progress_without_refreshing_monotonic_ttl():
    async def go():
        wall_clock = FakeClock(1_000)
        monotonic_clock = FakeClock(100)
        scheduler, _repo = make_recording_scheduler(
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
            buffer_ttl_sec=10,
        )
        response = await scheduler.submit_batch(files=[], concurrency=1)
        batch_id = response.batch_id
        events_before = list(scheduler._buffers[batch_id])
        assert all(event.ts == 1_000 for event in events_before)

        wall_clock.advance(-500)
        monotonic_clock.advance(9)
        await scheduler._emit(
            batch_id,
            WSEvent(
                seq=0,
                batch_id=batch_id,
                event="PROGRESS",
                payload={"late": True},
                ts=int(wall_clock()),
            ),
        )
        assert list(scheduler._buffers[batch_id]) == events_before

        monotonic_clock.advance(1)
        assert scheduler.is_batch_gone(batch_id)

    asyncio.run(go())
