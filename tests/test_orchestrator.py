import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

import parsing_core.orchestrator as orchestrator_module
import parsing_core.storage.fs_layout as fs_layout_module
import parsing_core.storage.repository as repository_module
from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Task
from parsing_core.orchestrator import Orchestrator
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema
from parsing_core.utils.hashing import text_sha256


class CountingOrchestrator(Orchestrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache_load_counts = {}

    def _load_cached_task(self, task):
        self.cache_load_counts[task.id] = self.cache_load_counts.get(task.id, 0) + 1
        return super()._load_cached_task(task)


def make_orchestrator(tmp_path):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    repo = Repository(conn)
    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(tmp_path / "x.db"))
    return orch, repo, fs, conn


def make_counting_orchestrator(tmp_path):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    orch = CountingOrchestrator(
        repo=repo,
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(tmp_path / "x.db"),
    )
    return orch, repo, fs, conn


def make_serving_orchestrator(tmp_path):
    os.environ["XDG_DATA_HOME"] = str(tmp_path)
    fs = FsLayout(base_dir=str(tmp_path / "data"))
    conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(conn)
    repo = Repository(conn)
    orch = Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(tmp_path / "x.db"))
    return orch, repo, fs, conn


def preregister_waiting_task(repo, task_id, file_path, batch_id="batch-1"):
    now = int(time.time())
    repo.create_batch(
        {
            "id": batch_id,
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
    repo.create_task(
        Task(
            id=task_id,
            file_path=file_path,
            snapshot_path="",
            file_sha256="",
            status="WAITING",
            created_at=now,
            updated_at=now,
            batch_id=batch_id,
        )
    )


def rewrite_cached_raw(orch, repo, task_id, file_path, raw):
    section = repo.list_sections(task_id)[0]
    Path(section.raw_md_path).write_text(raw, encoding="utf-8")
    repo.conn.execute(
        "UPDATE sections SET sha256 = ?, char_count = ? WHERE id = ?",
        (text_sha256(raw), len(raw), section.id),
    )
    repo.conn.commit()
    Path(orch.fs.merged_path(task_id)).write_text(
        orch._merge(task_id, file_path),
        encoding="utf-8",
    )


def rewrite_cached_ai(orch, repo, task_id, file_path, interpreted):
    section = repo.list_sections(task_id)[0]
    artifact = repo.get_artifact_by_section(section.id)
    assert artifact is not None
    Path(artifact.ai_md_path).write_text(interpreted, encoding="utf-8")
    Path(orch.fs.merged_path(task_id)).write_text(
        orch._merge(task_id, file_path),
        encoding="utf-8",
    )


def count_parser_calls(orch, monkeypatch):
    calls = []
    real_parse = orch.parser.parse

    def counting_parse(path):
        calls.append(path)
        return real_parse(path)

    monkeypatch.setattr(orch.parser, "parse", counting_parse)
    return calls


def test_parse_file_creates_merged_md(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert result["status"] == "COMPLETED"
    merged = Path(result["merged_md_path"])
    assert merged.exists()
    assert merged.parent == Path(fs.base_dir) / "tasks" / result["task_id"]
    assert not (Path(fs.base_dir) / result["task_id"]).exists()
    text = merged.read_text()
    assert "▸ AI 解读" in text
    assert "```mermaid" in text


def test_parse_file_returns_task_id(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    assert "task_id" in result
    assert len(result["task_id"]) == 36  # uuid


def test_parse_file_records_sections_count(tmp_path):
    orch, *_ = make_orchestrator(tmp_path)
    md = "## A\n\nfoo\n\n## B\n\nbar\n"
    f = tmp_path / "in.md"
    f.write_text(md)
    result = orch.parse_file(str(f))
    assert result["sections"] >= 2


def test_parse_file_rejects_empty_split_instead_of_completing_empty_task(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "empty.md"
    source.write_text("source exists", encoding="utf-8")
    monkeypatch.setattr(orch.parser, "parse", lambda _path: "\n\t\n")

    with pytest.raises(RuntimeError, match="no sections"):
        orch.parse_file(str(source), force=True)

    failed = repo.list_tasks_by_status("FAILED")[0]
    assert repo.list_sections(failed.id) == []
    assert not Path(fs.merged_path(failed.id)).exists()
    assert Path(failed.snapshot_path).is_file()
    conn.close()


def test_parse_file_fails_closed_when_task_name_is_replaced_during_image_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "parse-identity.md"
    source.write_text("# Identity\n\nbody", encoding="utf-8")
    displaced: Path | None = None
    replacement_marker: Path | None = None

    def replace_task_during_extract(raw_md: str, images_dir: str):
        nonlocal displaced, replacement_marker
        images = Path(images_dir)
        task = images.parent
        displaced = task.with_name(f"{task.name}-displaced")
        task.rename(displaced)
        task.mkdir(mode=0o700)
        (task / "images").mkdir(mode=0o700)
        replacement_marker = task / "replacement.md"
        replacement_marker.write_text("replacement", encoding="utf-8")
        return raw_md, []

    monkeypatch.setattr(orchestrator_module, "extract_images", replace_task_during_extract)

    with pytest.raises(RuntimeError, match="identity changed"):
        orch.parse_file(str(source), force=True)

    tasks = repo.list_all_tasks()
    assert len(tasks) == 1
    assert tasks[0].status == "FAILED"
    assert repo.list_sections(tasks[0].id) == []
    assert displaced is not None and displaced.is_dir()
    assert replacement_marker is not None
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    conn.close()


def test_task_text_write_fsyncs_file_before_replace_and_parent_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, _repo, fs, conn = make_orchestrator(tmp_path)
    task_id = "durable-text"
    fs.task_dir(task_id)
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append("fsync-file" if stat.S_ISREG(mode) else "fsync-dir")
        real_fsync(fd)

    def record_replace(*args: object, **kwargs: object) -> None:
        events.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module.os, "fsync", record_fsync)
    monkeypatch.setattr(orchestrator_module.os, "replace", record_replace)

    output = orch._write_task_text(task_id, "0.raw.md", "durable")

    assert Path(output).read_text(encoding="utf-8") == "durable"
    assert events[:3] == ["fsync-file", "replace", "fsync-dir"]
    conn.close()


def test_completed_parse_has_durable_identity_bound_publication_receipt(tmp_path: Path) -> None:
    orch, _repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "receipt.md"
    source.write_text("# Receipt\n\nbody", encoding="utf-8")

    result = orch.parse_file(str(source), force=True)

    task_id = str(result["task_id"])
    receipt = Path(fs.task_path(task_id)) / ".publication-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    task_stat = receipt.parent.stat(follow_symlinks=False)
    assert payload["version"] == 1
    assert payload["task_id"] == task_id
    assert payload["kind"] == "parse"
    assert payload["directory_identity"] == [task_stat.st_dev, task_stat.st_ino]
    conn.close()


def test_file_cache_hit_second_parse(tmp_path):
    orch, _repo, fs, _conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    r1 = orch.parse_file(str(sample))
    r2 = orch.parse_file(str(sample))
    assert r2["cached"] is True
    assert r1["task_id"] == r2["task_id"]
    assert Path(r2["merged_md_path"]).parent == Path(fs.base_dir) / "tasks" / r2["task_id"]
    assert not (Path(fs.base_dir) / r2["task_id"]).exists()


def test_purge_removes_only_task_from_tasks_hierarchy(tmp_path):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample), force=True)
    task_id = result["task_id"]
    task_dir = Path(fs.base_dir) / "tasks" / task_id
    legacy_dir = Path(fs.base_dir) / task_id

    assert task_dir.is_dir()
    assert not legacy_dir.exists()
    assert orch.purge(task_id) == {"task_id": task_id, "purged": True}
    assert not task_dir.exists()
    assert not legacy_dir.exists()
    assert repo.get_task(task_id) is None
    conn.close()


def test_purge_does_not_delete_replacement_or_database_row_on_inode_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source = tmp_path / "purge-race.md"
    source.write_text("# Purge race\n\nbody", encoding="utf-8")
    result = orch.parse_file(str(source), force=True)
    task_id = str(result["task_id"])
    task = Path(fs.task_path(task_id))
    displaced = task.with_name(f"{task_id}-displaced")
    replacement_marker = task / "replacement.md"
    real_rename = fs_layout_module.os.rename
    swapped = False

    def swap_at_quarantine(
        source_name: str,
        destination_name: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if source_name == task_id and "quarantine" in destination_name and not swapped:
            real_rename(
                source_name,
                displaced.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            os.mkdir(task_id, mode=0o700, dir_fd=src_dir_fd)
            replacement_task_fd = os.open(
                task_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=src_dir_fd,
            )
            try:
                marker_fd = os.open(
                    replacement_marker.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_task_fd,
                )
                try:
                    os.write(marker_fd, b"replacement")
                finally:
                    os.close(marker_fd)
            finally:
                os.close(replacement_task_fd)
            swapped = True
        real_rename(
            source_name,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(fs_layout_module.os, "rename", swap_at_quarantine)

    with pytest.raises(RuntimeError, match="identity changed"):
        orch.purge(task_id)

    assert swapped
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    assert displaced.is_dir()
    assert repo.get_task(task_id) is not None
    conn.close()


def test_direct_file_cache_hit_rechecks_after_concurrent_purge(tmp_path, monkeypatch):
    purge_orch, purge_repo, fs, purge_conn = make_serving_orchestrator(tmp_path)
    source = tmp_path / "direct-cache-purge.md"
    source.write_text("## 第一章\n\nDIRECT_CACHE_PURGE_RACE\n", encoding="utf-8")
    source_result = purge_orch.parse_file(str(source), force=True)
    source_id = source_result["task_id"]
    stale_merged_path = Path(source_result["merged_md_path"])

    reader_conn = init_db(str(tmp_path / "x.db"))
    apply_serve_schema(reader_conn)
    reader_orch = Orchestrator(
        repo=Repository(reader_conn),
        fs=fs,
        llm=StubLLMClient(),
        db_path=str(tmp_path / "x.db"),
    )
    first_validation_finished = threading.Event()
    allow_reader_to_continue = threading.Event()
    real_validate = reader_orch._validated_cached_task
    validation_calls = 0
    reader_results = []
    reader_errors = []

    def pause_after_first_validation(task, validation_session):
        nonlocal validation_calls
        cached = real_validate(task, validation_session)
        validation_calls += 1
        if validation_calls == 1:
            first_validation_finished.set()
            assert allow_reader_to_continue.wait(timeout=3)
        return cached

    monkeypatch.setattr(reader_orch, "_validated_cached_task", pause_after_first_validation)

    def run_reader():
        try:
            reader_results.append(reader_orch.parse_file(str(source)))
        except BaseException as error:
            reader_errors.append(error)

    reader = threading.Thread(target=run_reader)
    reader.start()
    try:
        assert first_validation_finished.wait(timeout=3)
        assert purge_orch.purge(source_id) == {"task_id": source_id, "purged": True}
        assert purge_repo.get_task(source_id) is None
        assert not stale_merged_path.exists()
    finally:
        allow_reader_to_continue.set()
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert reader_errors == []
    assert len(reader_results) == 1
    result = reader_results[0]
    assert result["cached"] is False
    assert result["status"] == "COMPLETED"
    assert result["task_id"] != source_id
    assert Path(result["merged_md_path"]).is_file()
    purge_conn.close()
    reader_conn.close()


def test_explicit_direct_cache_hit_does_not_reacquire_held_task_lock(tmp_path, monkeypatch):
    orch, _repo, _fs, conn = make_serving_orchestrator(tmp_path)
    source = tmp_path / "same-cache-task.md"
    source.write_text("## 第一章\n\nSAME_CACHE_TASK_LOCK\n", encoding="utf-8")
    source_result = orch.parse_file(str(source), force=True)
    source_id = source_result["task_id"]
    real_open_lock = orch._try_open_resume_lock
    task_lock_opens = 0

    def reject_duplicate_lock(task_id, *, blocking=False):
        nonlocal task_lock_opens
        if task_id == source_id:
            task_lock_opens += 1
            if task_lock_opens > 1:
                raise AssertionError("direct cache hit reacquired its held task lock")
        return real_open_lock(task_id, blocking=blocking)

    monkeypatch.setattr(orch, "_try_open_resume_lock", reject_duplicate_lock)

    result = orch.parse_file(str(source), task_id=source_id)

    assert result["cached"] is True
    assert result["task_id"] == source_id
    assert task_lock_opens == 1
    conn.close()


def test_explicit_task_id_materializes_file_cache_without_rerunning_pipeline(tmp_path, monkeypatch):
    workspace = tmp_path / "Application Support"
    workspace.mkdir()
    orch, repo, fs, conn = make_serving_orchestrator(workspace)
    source_file = workspace / "course.md"
    source_file.write_text(
        "# 第一章\n\n![模型\r\n图](data:image/png;base64,aW1hZ2UtYnl0ZXM=)\n\n管理决策正文。\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_sections = repo.list_sections(source_id)
    source_artifacts = [repo.get_artifact_by_section(section.id) for section in source_sections]
    source_files = {
        path: path.read_bytes()
        for path in Path(fs.task_dir(source_id)).rglob("*")
        if path.is_file()
    }

    accepted_id = "accepted-cache-task"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)
    monkeypatch.setattr(
        orch.parser,
        "parse",
        lambda _path: (_ for _ in ()).throw(AssertionError("parser reran on cache hit")),
    )
    monkeypatch.setattr(
        orch.llm,
        "interpret",
        lambda *_args: (_ for _ in ()).throw(AssertionError("LLM reran on cache hit")),
    )

    result = orch.parse_file(
        str(source_file),
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result == {
        "task_id": accepted_id,
        "merged_md_path": fs.merged_path(accepted_id),
        "sections": len(source_sections),
        "cached": True,
        "status": "COMPLETED",
    }
    accepted_task = repo.get_task(accepted_id)
    assert accepted_task is not None
    assert accepted_task.status == "COMPLETED"
    assert accepted_task.batch_id == "batch-1"
    assert snapshots and not Path(snapshots[-1]).exists()

    accepted_sections = repo.list_sections(accepted_id)
    assert len(accepted_sections) == len(source_sections)
    accepted_dir = Path(fs.task_dir(accepted_id))
    source_dir = Path(fs.task_dir(source_id))
    for source_section, accepted_section, source_artifact in zip(
        source_sections, accepted_sections, source_artifacts, strict=True
    ):
        assert accepted_section.id != source_section.id
        assert Path(accepted_section.raw_md_path).parent == accepted_dir
        assert Path(accepted_section.raw_md_path).is_file()
        accepted_raw = Path(accepted_section.raw_md_path).read_text(encoding="utf-8")
        assert str(source_dir) not in accepted_raw
        assert quote(str(accepted_dir), safe="/") in accepted_raw
        assert "![模型 图](<" in accepted_raw

        accepted_artifact = repo.get_artifact_by_section(accepted_section.id)
        assert source_artifact is not None
        assert accepted_artifact is not None
        assert accepted_artifact.id != source_artifact.id
        assert Path(accepted_artifact.ai_md_path).parent == accepted_dir
        assert Path(accepted_artifact.ai_md_path).is_file()

    merged_path = Path(result["merged_md_path"])
    merged = merged_path.read_text(encoding="utf-8")
    assert f"> 任务 ID: {accepted_id}" in merged
    assert str(source_dir) not in merged
    assert quote(str(accepted_dir), safe="/") in merged
    assert list((accepted_dir / "images").iterdir())

    source_image = next((source_dir / "images").iterdir())
    target_image = accepted_dir / "images" / source_image.name
    source_image_content = source_image.read_bytes()
    assert target_image.read_bytes() == source_image_content

    for source_path, source_content in source_files.items():
        assert source_path.is_file()
        assert source_path.read_bytes() == source_content
        relative_path = source_path.relative_to(source_dir)
        target_path = accepted_dir / relative_path
        assert target_path.is_file()
        assert not os.path.samefile(source_path, target_path)

    orch.purge(source_id)
    assert not source_dir.exists()
    assert target_image.read_bytes() == source_image_content
    assert all(
        str(source_dir) not in path.read_text(encoding="utf-8")
        for path in accepted_dir.glob("*.md")
    )
    orch.purge(accepted_id)
    assert not accepted_dir.exists()
    conn.close()


def test_corrupt_file_cache_falls_back_to_full_parse_for_explicit_task_id(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_section = repo.list_sections(source_id)[0]
    source_artifact = repo.get_artifact_by_section(source_section.id)
    assert source_artifact is not None
    Path(source_artifact.ai_md_path).unlink()

    accepted_id = "accepted-corrupt-cache"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = 0
    llm_calls = 0
    real_parse = orch.parser.parse
    real_interpret = orch.llm.interpret

    def counting_parse(path):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(path)

    def counting_interpret(section, raw_md):
        nonlocal llm_calls
        llm_calls += 1
        return real_interpret(section, raw_md)

    monkeypatch.setattr(orch.parser, "parse", counting_parse)
    monkeypatch.setattr(orch.llm, "interpret", counting_interpret)

    result = orch.parse_file(
        str(source_file),
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result["task_id"] == accepted_id
    assert result["cached"] is False
    assert parse_calls == 1
    assert llm_calls >= 1
    assert Path(result["merged_md_path"]).is_file()
    assert repo.list_sections(accepted_id)
    assert all(
        repo.get_artifact_by_section(section.id) is not None
        for section in repo.list_sections(accepted_id)
    )
    assert repo.get_task(source_id).status == "COMPLETED"
    assert not Path(source_artifact.ai_md_path).exists()
    conn.close()


def test_resume_completes_pending_sections(tmp_path):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    sample = Path("tests/fixtures/sample.md").resolve()
    result = orch.parse_file(str(sample))
    task_id = result["task_id"]
    # 人为破坏：把第一节标记为 PENDING 并清空 ai_artifact
    sections = repo.list_sections(task_id)
    if sections:
        repo.update_section_ai_status(sections[0].id, "PENDING")
        conn.execute("DELETE FROM ai_artifacts WHERE section_id = ?", (sections[0].id,))
        conn.commit()
    orch.resume(task_id)
    sections2 = repo.list_sections(task_id)
    assert all(s.ai_status == "COMPLETED" for s in sections2)


def test_resume_reuses_valid_committed_artifact_without_calling_llm(tmp_path):
    class CountingLLM(StubLLMClient):
        def __init__(self):
            self.calls = []

        def interpret(self, section, raw_md):
            self.calls.append(section.id)
            return super().interpret(section, raw_md)

    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "course.md"
    source.write_text("## 第一章\n\n原子恢复正文。\n", encoding="utf-8")
    parsed = orch.parse_file(str(source), force=True)
    task_id = str(parsed["task_id"])
    section = repo.list_sections(task_id)[0]
    artifact = repo.get_artifact_by_section(section.id)
    assert artifact is not None
    expected_ai = Path(artifact.ai_md_path).read_text(encoding="utf-8")
    repo.update_section_ai_status(section.id, "PENDING")
    repo.update_task_status(task_id, "FAILED", error_msg="simulated crash window")

    llm = CountingLLM()
    resumed = Orchestrator(repo=repo, fs=fs, llm=llm, db_path=str(tmp_path / "x.db"))
    first = resumed.resume(task_id)
    second = resumed.resume(task_id)

    recovered_artifact = repo.get_artifact_by_section(section.id)
    assert first["status"] == "COMPLETED"
    assert second["status"] == "ALREADY_COMPLETED"
    assert llm.calls == []
    assert recovered_artifact is not None
    assert recovered_artifact.id == artifact.id
    assert Path(recovered_artifact.ai_md_path).read_text(encoding="utf-8") == expected_ai
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE section_id = ?",
            (section.id,),
        ).fetchone()[0]
        == 1
    )
    conn.close()


@pytest.mark.parametrize("invalid_artifact", ["missing", "outside", "symlink"])
def test_resume_regenerates_untrusted_committed_artifact_safely(tmp_path, invalid_artifact):
    class CountingLLM(StubLLMClient):
        def __init__(self):
            self.calls = []

        def interpret(self, section, raw_md):
            self.calls.append(section.id)
            artifact = super().interpret(section, raw_md)
            artifact.ai_md = f"### RECOVERED ARTIFACT\n\n{raw_md.strip()}\n"
            return artifact

    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "course.md"
    source.write_text("## 第一章\n\n不可信产物恢复正文。\n", encoding="utf-8")
    parsed = orch.parse_file(str(source), force=True)
    task_id = str(parsed["task_id"])
    section = repo.list_sections(task_id)[0]
    old_artifact = repo.get_artifact_by_section(section.id)
    assert old_artifact is not None
    expected_path = Path(fs.section_ai_path(task_id, section.seq))
    outside = tmp_path / "outside.ai.md"
    outside.write_text("EXTERNAL FILE MUST REMAIN UNCHANGED", encoding="utf-8")
    expected_path.unlink()
    if invalid_artifact == "outside":
        conn.execute(
            "UPDATE ai_artifacts SET ai_md_path = ? WHERE section_id = ?",
            (str(outside), section.id),
        )
        conn.commit()
    elif invalid_artifact == "symlink":
        expected_path.symlink_to(outside)
    repo.update_section_ai_status(section.id, "PENDING")
    repo.update_task_status(task_id, "FAILED", error_msg="simulated invalid artifact")

    llm = CountingLLM()
    resumed = Orchestrator(repo=repo, fs=fs, llm=llm, db_path=str(tmp_path / "x.db"))
    first = resumed.resume(task_id)
    second = resumed.resume(task_id)

    recovered = repo.get_artifact_by_section(section.id)
    assert first["status"] == "COMPLETED"
    assert second["status"] == "ALREADY_COMPLETED"
    assert llm.calls == [section.id]
    assert recovered is not None
    assert recovered.id != old_artifact.id
    assert Path(recovered.ai_md_path) == expected_path
    assert "RECOVERED ARTIFACT" in expected_path.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "EXTERNAL FILE MUST REMAIN UNCHANGED"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM ai_artifacts WHERE section_id = ?",
            (section.id,),
        ).fetchone()[0]
        == 1
    )
    conn.close()


@pytest.mark.parametrize(
    "raw_corruption",
    ["symlink", "outside", "tampered", "newline_tampered"],
)
def test_resume_rebuilds_from_snapshot_before_untrusted_raw_reaches_llm(
    tmp_path,
    raw_corruption,
):
    class FailingLLM(StubLLMClient):
        def interpret(self, section, raw_md):
            raise RuntimeError("stop after sectioning")

    class RecordingLLM(StubLLMClient):
        def __init__(self):
            self.raw_inputs = []

        def interpret(self, section, raw_md):
            self.raw_inputs.append(raw_md)
            return super().interpret(section, raw_md)

    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "trusted-course.md"
    source.write_text(
        "## 第一章\n\nTRUSTED_ALPHA\n\n## 第二章\n\nTRUSTED_BETA\n",
        encoding="utf-8",
    )
    failing = Orchestrator(repo=repo, fs=fs, llm=FailingLLM(), db_path=str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError, match="stop after sectioning"):
        failing.parse_file(str(source), force=True)

    task = repo.list_tasks_by_status("FAILED")[0]
    old_sections = repo.list_sections(task.id)
    assert len(old_sections) == 2
    attacked = old_sections[0]
    attacked_path = Path(attacked.raw_md_path)
    outside = tmp_path / "outside-raw.md"
    outside.write_text("UNTRUSTED_EXTERNAL_RAW", encoding="utf-8")
    if raw_corruption == "symlink":
        attacked_path.unlink()
        attacked_path.symlink_to(outside)
    elif raw_corruption == "outside":
        conn.execute(
            "UPDATE sections SET raw_md_path = ? WHERE id = ?",
            (str(outside), attacked.id),
        )
        conn.commit()
    elif raw_corruption == "tampered":
        attacked_path.write_text("UNTRUSTED_TAMPERED_RAW", encoding="utf-8")
    else:
        original_bytes = attacked_path.read_bytes()
        assert b"\n" in original_bytes
        attacked_path.write_bytes(original_bytes.replace(b"\n", b"\r\n"))
    source.unlink()

    recording = RecordingLLM()
    resumed = Orchestrator(repo=repo, fs=fs, llm=recording, db_path=str(tmp_path / "x.db"))
    result = resumed.resume(task.id)

    rebuilt_sections = repo.list_sections(task.id)
    merged = Path(result["merged_md_path"]).read_text(encoding="utf-8")
    assert result["status"] == "COMPLETED"
    assert [section.seq for section in rebuilt_sections] == [0, 1]
    assert {section.id for section in rebuilt_sections}.isdisjoint(
        {section.id for section in old_sections}
    )
    assert "TRUSTED_ALPHA" in merged and "TRUSTED_BETA" in merged
    assert all("UNTRUSTED" not in raw for raw in recording.raw_inputs)
    assert "UNTRUSTED" not in merged
    assert outside.read_text(encoding="utf-8") == "UNTRUSTED_EXTERNAL_RAW"
    assert not Path(task.snapshot_path).exists()
    conn.close()


@pytest.mark.parametrize("ai_corruption", ["outside", "symlink"])
def test_resume_regenerates_completed_section_before_untrusted_ai_reaches_merge(
    tmp_path,
    ai_corruption,
):
    class RecordingLLM(StubLLMClient):
        def __init__(self):
            self.calls = []

        def interpret(self, section, raw_md):
            self.calls.append(section.id)
            artifact = super().interpret(section, raw_md)
            artifact.ai_md = "### TRUSTED_REGENERATED_AI\n"
            return artifact

    orch, repo, fs, conn = make_orchestrator(tmp_path)
    source = tmp_path / "course.md"
    source.write_text("## 第一章\n\n可信原文。\n", encoding="utf-8")
    parsed = orch.parse_file(str(source), force=True)
    task_id = str(parsed["task_id"])
    section = repo.list_sections(task_id)[0]
    artifact = repo.get_artifact_by_section(section.id)
    assert artifact is not None
    expected_ai_path = Path(fs.section_ai_path(task_id, section.seq))
    outside = tmp_path / "outside-ai.md"
    outside.write_text("UNTRUSTED_EXTERNAL_AI", encoding="utf-8")
    if ai_corruption == "outside":
        conn.execute(
            "UPDATE ai_artifacts SET ai_md_path = ? WHERE id = ?",
            (str(outside), artifact.id),
        )
        conn.commit()
    else:
        expected_ai_path.unlink()
        expected_ai_path.symlink_to(outside)
    repo.update_task_status(task_id, "FAILED", error_msg="resume merge window")

    recording = RecordingLLM()
    resumed = Orchestrator(repo=repo, fs=fs, llm=recording, db_path=str(tmp_path / "x.db"))
    result = resumed.resume(task_id)

    merged = Path(result["merged_md_path"]).read_text(encoding="utf-8")
    recovered = repo.get_artifact_by_section(section.id)
    assert recording.calls == [section.id]
    assert "TRUSTED_REGENERATED_AI" in merged
    assert "UNTRUSTED_EXTERNAL_AI" not in merged
    assert recovered is not None
    assert Path(recovered.ai_md_path) == expected_ai_path
    assert not expected_ai_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "UNTRUSTED_EXTERNAL_AI"
    conn.close()


def _create_zero_section_resume_task(repo, snapshot_path, expected_sha256):
    now = int(time.time())
    task = Task(
        id="snapshot-recovery-task",
        file_path="/missing/original.md",
        snapshot_path=str(snapshot_path),
        file_sha256=expected_sha256,
        status="FAILED",
        created_at=now,
        updated_at=now,
    )
    repo.create_task(task)
    return task


@pytest.mark.parametrize(
    "snapshot_corruption",
    ["rewritten", "hash_mismatch", "symlink", "non_regular"],
)
def test_resume_rejects_untrusted_snapshot_before_parser(
    tmp_path,
    monkeypatch,
    snapshot_corruption,
):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    trusted = b"## Trusted\n\nORIGINAL SNAPSHOT\n"
    expected_sha256 = hashlib.sha256(trusted).hexdigest()
    snapshot_path = tmp_path / "recovery.md"
    if snapshot_corruption == "symlink":
        target = tmp_path / "snapshot-target.md"
        target.write_bytes(trusted)
        snapshot_path.symlink_to(target)
    elif snapshot_corruption == "non_regular":
        snapshot_path.mkdir()
    else:
        snapshot_path.write_bytes(trusted)
    task = _create_zero_section_resume_task(repo, snapshot_path, expected_sha256)
    if snapshot_corruption == "rewritten":
        snapshot_path.write_bytes(b"## Changed\n\nMUTATED SNAPSHOT\n")
    elif snapshot_corruption == "hash_mismatch":
        conn.execute(
            "UPDATE tasks SET file_sha256 = ? WHERE id = ?",
            ("0" * 64, task.id),
        )
        conn.commit()
    parser_called = False

    def forbidden_parser(_path):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("untrusted snapshot reached parser")

    monkeypatch.setattr(orch.parser, "parse", forbidden_parser)
    with pytest.raises(RuntimeError, match="snapshot"):
        orch.resume(task.id)

    failed = repo.get_task(task.id)
    assert failed is not None
    assert failed.status == "FAILED"
    assert failed.error_msg and "snapshot" in failed.error_msg.lower()
    assert parser_called is False
    assert snapshot_path.exists()
    assert not (Path(fs.task_path(task.id)) / "merged.md").exists()
    conn.close()


def test_resume_parser_receives_verified_private_snapshot_with_original_suffix(
    tmp_path, monkeypatch
):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    snapshot_path = tmp_path / "source.material.md"
    snapshot_path.write_text("## 第一章\n\nPRIVATE COPY BODY\n", encoding="utf-8")
    expected_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    task = _create_zero_section_resume_task(repo, snapshot_path, expected_sha256)
    parser_paths = []

    def record_private_copy(path):
        parser_path = Path(path)
        parser_paths.append(parser_path)
        assert parser_path != snapshot_path
        assert parser_path.suffix == snapshot_path.suffix
        assert parser_path.read_bytes() == snapshot_path.read_bytes()
        return parser_path.read_text(encoding="utf-8")

    monkeypatch.setattr(orch.parser, "parse", record_private_copy)
    result = orch.resume(task.id)

    assert result["status"] == "COMPLETED"
    assert len(parser_paths) == 1
    assert not parser_paths[0].exists()
    assert not snapshot_path.exists()
    conn.close()


@pytest.mark.parametrize("failure_stage", ["reset", "parser"])
def test_resume_cleans_verified_private_snapshot_on_failure(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    snapshot_path = tmp_path / "failed-recovery.md"
    snapshot_path.write_text("## 第一章\n\nPRIVATE FAILURE BODY\n", encoding="utf-8")
    task = _create_zero_section_resume_task(
        repo,
        snapshot_path,
        hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    )
    private_paths = []
    real_verified_copy = orch._verified_snapshot_copy

    def record_verified_copy(resume_task):
        private_path = real_verified_copy(resume_task)
        private_paths.append(Path(private_path))
        return private_path

    def fail_reset(_task_id):
        raise RuntimeError("reset failed")

    def fail_parser(path):
        private_paths.append(Path(path))
        raise RuntimeError("parser failed")

    monkeypatch.setattr(orch, "_verified_snapshot_copy", record_verified_copy)
    if failure_stage == "reset":
        monkeypatch.setattr(orch, "_reset_task_outputs_for_reparse", fail_reset)
    else:
        monkeypatch.setattr(orch.parser, "parse", fail_parser)

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        orch.resume(task.id)

    assert private_paths
    assert all(not private_path.exists() for private_path in private_paths)
    assert snapshot_path.exists()
    failed = repo.get_task(task.id)
    assert failed is not None and failed.status == "FAILED"
    conn.close()


def test_resume_rejects_snapshot_changed_during_verified_copy(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_orchestrator(tmp_path)
    initial = b"A" * orchestrator_module._SNAPSHOT_COPY_CHUNK_BYTES
    appended = b"CHANGED_DURING_COPY"
    snapshot_path = tmp_path / "changing-recovery.md"
    snapshot_path.write_bytes(initial)
    task = _create_zero_section_resume_task(
        repo,
        snapshot_path,
        hashlib.sha256(initial + appended).hexdigest(),
    )
    real_read = os.read
    changed = False
    parser_called = False

    def change_after_first_read(fd, size):
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            with snapshot_path.open("ab") as stream:
                stream.write(appended)
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    def forbidden_parser(_path):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("changing snapshot reached parser")

    monkeypatch.setattr(orchestrator_module.os, "read", change_after_first_read)
    monkeypatch.setattr(orch.parser, "parse", forbidden_parser)

    with pytest.raises(RuntimeError, match="changed while being verified"):
        orch.resume(task.id)

    assert changed is True
    assert parser_called is False
    assert snapshot_path.read_bytes() == initial + appended
    failed = repo.get_task(task.id)
    assert failed is not None and failed.status == "FAILED"
    conn.close()


@pytest.mark.parametrize(
    "link_kind",
    ["absolute_escape", "relative_escape", "file_uri", "missing_relative"],
)
def test_unsafe_cached_local_image_link_falls_back_to_parse(tmp_path, monkeypatch, link_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    outside = source_dir.parent / "outside.png"
    outside.write_bytes(b"outside")
    targets = {
        "absolute_escape": str(source_dir / ".." / outside.name),
        "relative_escape": f"../{outside.name}",
        "file_uri": outside.as_uri(),
        "missing_relative": "images/missing.png",
    }
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![unsafe]({targets[link_kind]})\n",
    )
    accepted_id = f"accepted-{link_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["task_id"] == accepted_id
    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


@pytest.mark.parametrize("destination", ["images/%00.png", "http://[invalid-ipv6"])
def test_untrusted_image_url_errors_invalidate_cache(tmp_path, monkeypatch, destination):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![invalid]({destination})\n",
    )
    accepted_id = f"accepted-invalid-url-{len(destination)}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert result["status"] == "COMPLETED"
    assert len(parse_calls) == 1
    conn.close()


@pytest.mark.parametrize("cached_file_kind", ["raw", "ai", "merged", "task_dir"])
def test_cached_markdown_symlink_falls_back_to_parse(tmp_path, monkeypatch, cached_file_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    section = repo.list_sections(source_id)[0]
    artifact = repo.get_artifact_by_section(section.id)
    assert artifact is not None
    if cached_file_kind == "task_dir":
        cached_path = Path(fs.task_path(source_id))
        backing = tmp_path / "task-dir-backing"
        cached_path.rename(backing)
        cached_path.symlink_to(backing, target_is_directory=True)
    else:
        paths = {
            "raw": Path(section.raw_md_path),
            "ai": Path(artifact.ai_md_path),
            "merged": Path(fs.merged_path(source_id)),
        }
        cached_path = paths[cached_file_kind]
        backing = tmp_path / f"{cached_file_kind}.backing.md"
        backing.write_bytes(cached_path.read_bytes())
        cached_path.unlink()
        cached_path.symlink_to(backing)
    accepted_id = f"accepted-symlink-{cached_file_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["task_id"] == accepted_id
    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


@pytest.mark.parametrize("corruption", ["merged_truncated", "ai_tampered"])
def test_cached_merged_must_match_section_and_ai_content(tmp_path, monkeypatch, corruption):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    if corruption == "merged_truncated":
        Path(fs.merged_path(source_id)).write_text(
            f"> 任务 ID: {source_id}\n",
            encoding="utf-8",
        )
    else:
        section = repo.list_sections(source_id)[0]
        artifact = repo.get_artifact_by_section(section.id)
        assert artifact is not None
        ai_path = Path(artifact.ai_md_path)
        ai_path.write_text(
            f"{ai_path.read_text(encoding='utf-8')}\n合法 UTF-8 篡改内容。\n",
            encoding="utf-8",
        )
    accepted_id = f"accepted-{corruption}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)
    llm_calls = []
    real_interpret = orch.llm.interpret

    def counting_interpret(section, raw_md):
        llm_calls.append(section.id)
        return real_interpret(section, raw_md)

    monkeypatch.setattr(orch.llm, "interpret", counting_interpret)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["task_id"] == accepted_id
    assert result["cached"] is False
    assert len(parse_calls) == 1
    assert llm_calls
    assert "合法 UTF-8 篡改内容" not in Path(result["merged_md_path"]).read_text(encoding="utf-8")
    conn.close()


@pytest.mark.parametrize("symlink_kind", ["file", "ancestor"])
def test_cached_image_symlink_falls_back_to_parse(tmp_path, monkeypatch, symlink_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,aW1hZ2U=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    images_dir = Path(fs.images_dir(source_id))
    image_path = next(images_dir.iterdir())
    if symlink_kind == "file":
        backing = tmp_path / "outside.png"
        backing.write_bytes(image_path.read_bytes())
        image_path.unlink()
        image_path.symlink_to(backing)
    else:
        backing_dir = tmp_path / "outside-images"
        backing_dir.mkdir()
        (backing_dir / "nested.png").write_bytes(b"nested")
        nested = images_dir / "nested"
        nested.symlink_to(backing_dir, target_is_directory=True)
        section = repo.list_sections(source_id)[0]
        raw = Path(section.raw_md_path).read_text(encoding="utf-8")
        rewrite_cached_raw(
            orch,
            repo,
            source_id,
            str(source_file),
            f"{raw}\n![nested](images/nested/nested.png)\n",
        )
    accepted_id = "accepted-image-symlink"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


def test_valid_relative_and_remote_images_materialize_without_path_rewrite(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    (source_dir / "images" / "relative.png").write_bytes(b"relative")
    (source_dir / "images" / "a(b).png").write_bytes(b"balanced")
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![relative](images/relative.png)\n"
        "![balanced](images/a(b).png)\n"
        "![angle](<images/a(b).png>)\n"
        r"![escaped](images/a\(b\).png)"
        "\n"
        '![quoted-title](images/relative.png "Figure 1")\n'
        "![parenthesized-title](images/relative.png (Figure 2))\n"
        "![https](https://example.com/a.png)\n"
        "![remote-balanced](https://example.com/a(b).png)\n"
        "![data](data:image/png;base64,aW1hZ2U=)\n",
    )
    accepted_id = "accepted-valid-links"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    monkeypatch.setattr(
        orch.parser,
        "parse",
        lambda _path: (_ for _ in ()).throw(AssertionError("parser reran")),
    )

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is True
    accepted_raw = Path(repo.list_sections(accepted_id)[0].raw_md_path).read_text(encoding="utf-8")
    assert "![relative](images/relative.png)" in accepted_raw
    assert "![balanced](images/a(b).png)" in accepted_raw
    assert "![angle](<images/a(b).png>)" in accepted_raw
    assert r"![escaped](images/a\(b\).png)" in accepted_raw
    assert '![quoted-title](images/relative.png "Figure 1")' in accepted_raw
    assert "![parenthesized-title](images/relative.png (Figure 2))" in accepted_raw
    assert "![https](https://example.com/a.png)" in accepted_raw
    assert "![remote-balanced](https://example.com/a(b).png)" in accepted_raw
    assert "![data](data:image/png;base64,aW1hZ2U=)" in accepted_raw
    assert (Path(fs.task_dir(accepted_id)) / "images" / "relative.png").is_file()
    conn.close()


def test_cached_absolute_image_rewrite_preserves_query_and_fragment(tmp_path, monkeypatch):
    workspace = tmp_path / "Application Support"
    workspace.mkdir()
    orch, repo, fs, conn = make_serving_orchestrator(workspace)
    source_file = workspace / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    source_svg = source_dir / "images" / "model.svg"
    source_svg.write_text("<svg></svg>", encoding="utf-8")
    encoded_source_svg = quote(str(source_svg), safe="/")
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![fragment](<{encoded_source_svg}#node-a>)\n"
        f"![query](<{encoded_source_svg}?theme=dark%20mode#layer%201>)\n",
    )
    accepted_id = "accepted-query-fragment"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    monkeypatch.setattr(
        orch.parser,
        "parse",
        lambda _path: (_ for _ in ()).throw(AssertionError("parser reran")),
    )

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is True
    target_svg = Path(fs.task_dir(accepted_id)) / "images" / source_svg.name
    encoded_target_svg = quote(str(target_svg), safe="/")
    accepted_raw = Path(repo.list_sections(accepted_id)[0].raw_md_path).read_text(encoding="utf-8")
    assert f"![fragment](<{encoded_target_svg}#node-a>)" in accepted_raw
    assert f"![query](<{encoded_target_svg}?theme=dark%20mode#layer%201>)" in accepted_raw
    assert target_svg.read_text(encoding="utf-8") == "<svg></svg>"
    conn.close()


@pytest.mark.parametrize("legacy_kind", ["raw", "encoded_unclosed"])
def test_legacy_bare_absolute_image_path_with_spaces_invalidates_cache(
    tmp_path, monkeypatch, legacy_kind
):
    workspace = tmp_path / "Application Support"
    workspace.mkdir()
    orch, repo, fs, conn = make_serving_orchestrator(workspace)
    source_file = workspace / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,aW1hZ2U=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    source_image = next((source_dir / "images").iterdir())
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    legacy_destination = (
        str(source_image) if legacy_kind == "raw" else quote(str(source_image), safe="/")
    )
    closing = ")" if legacy_kind == "raw" else ""
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![legacy]({legacy_destination}{closing}\n",
    )
    accepted_id = "accepted-legacy-space-path"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert len(parse_calls) == 1
    accepted_dir = Path(fs.task_dir(accepted_id))
    assert all(
        str(source_dir) not in path.read_text(encoding="utf-8")
        for path in accepted_dir.glob("*.md")
    )
    assert all(
        quote(str(source_dir), safe="/") not in path.read_text(encoding="utf-8")
        for path in accepted_dir.glob("*.md")
    )
    conn.close()


@pytest.mark.parametrize("encoded_component", ["images", "i%6Dages", "i%6dages"])
def test_unclosed_image_link_with_once_decoded_source_path_invalidates_cache(
    tmp_path, monkeypatch, encoded_component
):
    workspace = tmp_path / "Application Support"
    workspace.mkdir()
    orch, repo, fs, conn = make_serving_orchestrator(workspace)
    source_file = workspace / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,aW1hZ2U=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    source_image = next((source_dir / "images").iterdir())
    section = repo.list_sections(source_id)[0]
    raw = Path(section.raw_md_path).read_text(encoding="utf-8")
    encoded_source_image = quote(str(source_image), safe="/")
    encoded_source_image = encoded_source_image.replace("images", encoded_component, 1)
    rewrite_cached_raw(
        orch,
        repo,
        source_id,
        str(source_file),
        f"{raw}\n![legacy](<{encoded_source_image}\n",
    )
    accepted_id = f"accepted-once-decoded-{encoded_component.replace('%', '').lower()}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


def test_repository_materialization_error_cleans_cache_hit_snapshot(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    orch.parse_file(str(source_file))
    accepted_id = "accepted-repository-error"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)
    monkeypatch.setattr(
        repo,
        "materialize_cached_task",
        lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("db unavailable")),
    )

    with pytest.raises(sqlite3.OperationalError, match="db unavailable"):
        orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert snapshots and not Path(snapshots[-1]).exists()
    assert not Path(fs.task_path(accepted_id)).exists()
    conn.close()


def test_target_publication_error_cleans_new_empty_target_snapshot_and_staging(
    tmp_path, monkeypatch
):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    orch.parse_file(str(source_file))
    accepted_id = "accepted-target-write-error"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    def fail_target_write(_path, _content):
        raise orchestrator_module.TaskPublicationError("target disk unavailable")

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)
    monkeypatch.setattr(orch, "_write_publication_text", fail_target_write)
    monkeypatch.setattr(
        orch.parser,
        "parse",
        lambda _path: (_ for _ in ()).throw(AssertionError("target error fell back to parser")),
    )

    with pytest.raises(orchestrator_module.TaskPublicationError, match="target disk unavailable"):
        orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    accepted = repo.get_task(accepted_id)
    assert accepted is not None and accepted.status == "WAITING"
    assert snapshots and not Path(snapshots[-1]).exists()
    assert not Path(fs.task_path(accepted_id)).exists()
    assert not list(Path(fs.tasks_dir()).glob(f".{accepted_id}.*.tmp"))
    conn.close()


def multi_section_markdown():
    return "".join(f"## 第 {index} 章\n\n{'正文内容。' * 40}\n\n" for index in range(1, 4))


def test_valid_section_cache_loads_source_task_once_per_parse_lifecycle(tmp_path):
    orch, repo, fs, conn = make_counting_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(multi_section_markdown(), encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    assert len(repo.list_sections(source_id)) == 3
    accepted_id = "accepted-section-cache"
    preregister_waiting_task(repo, accepted_id, str(source_file))

    result = orch.parse_file(
        str(source_file),
        force=True,
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result["status"] == "COMPLETED"
    assert orch.cache_load_counts[source_id] == 1
    conn.close()


@pytest.mark.parametrize("link_kind", ["absolute", "relative"])
def test_section_cache_with_local_ai_image_reruns_llm(tmp_path, monkeypatch, link_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    source_image = source_dir / "images" / "ai-local.png"
    source_image.write_bytes(b"local-image")
    destination = (
        f"<{quote(str(source_image), safe='/')}>"
        if link_kind == "absolute"
        else f"images/{source_image.name}"
    )
    rewrite_cached_ai(
        orch,
        repo,
        source_id,
        str(source_file),
        f"### AI 解读\n\n![local]({destination})\n",
    )
    accepted_id = f"accepted-local-section-{link_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    llm_calls = 0
    real_interpret = orch.llm.interpret

    def counting_interpret(section, raw_md):
        nonlocal llm_calls
        llm_calls += 1
        return real_interpret(section, raw_md)

    monkeypatch.setattr(orch.llm, "interpret", counting_interpret)

    result = orch.parse_file(
        str(source_file),
        force=True,
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result["cached"] is False
    assert llm_calls == 1
    accepted_section = repo.list_sections(accepted_id)[0]
    accepted_artifact = repo.get_artifact_by_section(accepted_section.id)
    assert accepted_artifact is not None
    accepted_ai = Path(accepted_artifact.ai_md_path).read_text(encoding="utf-8")
    assert str(source_dir) not in accepted_ai
    assert "![local]" not in accepted_ai
    conn.close()


def test_section_cache_with_remote_ai_images_still_hits(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    remote_ai = (
        "### AI 解读\n\n"
        "![https](https://example.com/chart(a).png)\n"
        "![data](data:image/png;base64,aW1hZ2U=)\n"
        "[ordinary reference][source]\n\n"
        "[source]: https://example.com/source\n"
    )
    rewrite_cached_ai(orch, repo, source_id, str(source_file), remote_ai)
    accepted_id = "accepted-remote-section"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    monkeypatch.setattr(
        orch.llm,
        "interpret",
        lambda *_args: (_ for _ in ()).throw(AssertionError("LLM reran")),
    )

    result = orch.parse_file(
        str(source_file),
        force=True,
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result["cached"] is False
    accepted_section = repo.list_sections(accepted_id)[0]
    accepted_artifact = repo.get_artifact_by_section(accepted_section.id)
    assert accepted_artifact is not None
    assert Path(accepted_artifact.ai_md_path).read_text(encoding="utf-8") == remote_ai
    conn.close()


@pytest.mark.parametrize(
    ("image_reference", "definition"),
    [
        ("![local][fig]", "[fig]: images/local.png"),
        ("![local][]", "[local]: https://example.com/remote.png"),
        ("![local]", "[local]: data:image/png;base64,aW1hZ2U="),
    ],
)
def test_section_cache_with_reference_style_image_reruns_llm(
    tmp_path,
    monkeypatch,
    image_reference,
    definition,
):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    (Path(fs.images_dir(source_id)) / "local.png").write_bytes(b"local")
    cached_ai = f"### AI 解读\n\n{image_reference}\n\n{definition}\n"
    rewrite_cached_ai(orch, repo, source_id, str(source_file), cached_ai)
    accepted_id = f"accepted-reference-image-{len(image_reference)}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    llm_calls = 0
    real_interpret = orch.llm.interpret

    def counting_interpret(section, raw_md):
        nonlocal llm_calls
        llm_calls += 1
        return real_interpret(section, raw_md)

    monkeypatch.setattr(orch.llm, "interpret", counting_interpret)

    result = orch.parse_file(
        str(source_file),
        force=True,
        task_id=accepted_id,
        batch_id="batch-1",
    )

    assert result["cached"] is False
    assert llm_calls == 1
    accepted_section = repo.list_sections(accepted_id)[0]
    accepted_artifact = repo.get_artifact_by_section(accepted_section.id)
    assert accepted_artifact is not None
    assert image_reference not in Path(accepted_artifact.ai_md_path).read_text(encoding="utf-8")
    conn.close()


def test_invalid_file_cache_is_not_revalidated_for_each_section(tmp_path):
    orch, repo, fs, conn = make_counting_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(multi_section_markdown(), encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    assert len(repo.list_sections(source_id)) == 3
    Path(fs.merged_path(source_id)).write_text(
        f"> 任务 ID: {source_id}\n",
        encoding="utf-8",
    )
    accepted_id = "accepted-invalid-section-cache"
    preregister_waiting_task(repo, accepted_id, str(source_file))

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert orch.cache_load_counts[source_id] == 1
    conn.close()


@pytest.mark.parametrize("swap_kind", ["file", "directory"])
def test_copy_stage_symlink_swap_falls_back_without_publishing_outside_content(
    tmp_path, monkeypatch, swap_kind
):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,b3JpZ2luYWw=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_dir = Path(fs.task_dir(source_id))
    images_dir = source_dir / "images"
    source_image = next(images_dir.iterdir())
    outside_content = b"outside-secret-content"
    outside_file = tmp_path / "outside.png"
    outside_file.write_bytes(outside_content)
    nested = images_dir / "nested"
    nested_backup = tmp_path / "nested-original"
    outside_dir = tmp_path / "outside-images"
    outside_dir.mkdir()
    (outside_dir / "nested.png").write_bytes(outside_content)
    if swap_kind == "directory":
        nested.mkdir()
        (nested / "nested.png").write_bytes(b"inside")
        section = repo.list_sections(source_id)[0]
        raw = Path(section.raw_md_path).read_text(encoding="utf-8")
        rewrite_cached_raw(
            orch,
            repo,
            source_id,
            str(source_file),
            f"{raw}\n![nested](images/nested/nested.png)\n",
        )

    accepted_id = f"accepted-race-{swap_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)
    real_open = orchestrator_module.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        is_source_root = not isinstance(path, int) and Path(path) == images_dir
        if not swapped and is_source_root and flags & getattr(os, "O_DIRECTORY", 0):
            swapped = True
            if swap_kind == "file":
                source_image.unlink()
                source_image.symlink_to(outside_file)
            else:
                nested.rename(nested_backup)
                nested.symlink_to(outside_dir, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestrator_module.os, "open", racing_open)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert swapped
    assert result["cached"] is False
    assert len(parse_calls) == 1
    accepted_images = Path(fs.images_dir(accepted_id))
    assert all(
        path.read_bytes() != outside_content
        for path in accepted_images.rglob("*")
        if path.is_file()
    )
    conn.close()


@pytest.mark.parametrize("mutation", ["truncate", "grow", "replace"])
def test_cached_image_change_during_copy_falls_back_to_parse(tmp_path, monkeypatch, mutation):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,b3JpZ2luYWw=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_image = next(Path(fs.images_dir(source_id)).iterdir())
    original_content = b"0123456789abcdef"
    source_image.write_bytes(original_content)
    replaced_path = tmp_path / "replaced-original.png"
    mutated = False
    real_fdopen = orchestrator_module.os.fdopen

    class MutatingReader:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.wrapped.close()

        def read(self, size=-1):
            nonlocal mutated
            chunk = self.wrapped.read(size)
            if chunk and not mutated:
                mutated = True
                if mutation == "truncate":
                    os.truncate(source_image, 2)
                elif mutation == "grow":
                    with source_image.open("ab") as file:
                        file.write(b"grow")
                else:
                    source_image.rename(replaced_path)
                    source_image.write_bytes(b"replacement-data")
            return chunk

    def mutating_fdopen(fd, mode="r", *args, **kwargs):
        wrapped = real_fdopen(fd, mode, *args, **kwargs)
        if mode == "rb":
            return MutatingReader(wrapped)
        return wrapped

    monkeypatch.setattr(orchestrator_module, "_IMAGE_COPY_CHUNK_BYTES", 4)
    monkeypatch.setattr(orchestrator_module.os, "fdopen", mutating_fdopen)
    accepted_id = f"accepted-file-change-{mutation}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert mutated
    assert result["cached"] is False
    assert len(parse_calls) == 1
    assert not list(Path(fs.tasks_dir()).glob(f".{accepted_id}.*.tmp"))
    accepted_images = list(Path(fs.images_dir(accepted_id)).iterdir())
    assert accepted_images
    assert all(path.read_bytes() != b"replacement-data" for path in accepted_images)
    conn.close()


def test_cached_image_removed_after_copy_falls_back_to_full_parse(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,aW1hZ2U=)\n",
        encoding="utf-8",
    )
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    source_image = next(Path(fs.images_dir(source_id)).iterdir())
    real_copy = orch._copy_cached_images_bounded
    image_removed = False

    def copy_then_remove(source_images, target_images):
        nonlocal image_removed
        real_copy(source_images, target_images)
        source_image.unlink()
        image_removed = True

    monkeypatch.setattr(orch, "_copy_cached_images_bounded", copy_then_remove)
    parse_calls = count_parser_calls(orch, monkeypatch)
    llm_calls = 0
    real_interpret = orch.llm.interpret

    def counting_interpret(section, raw_md):
        nonlocal llm_calls
        llm_calls += 1
        return real_interpret(section, raw_md)

    monkeypatch.setattr(orch.llm, "interpret", counting_interpret)
    accepted_id = "accepted-source-damaged-after-copy"
    preregister_waiting_task(repo, accepted_id, str(source_file))

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert image_removed
    assert result["cached"] is False
    assert result["status"] == "COMPLETED"
    assert len(parse_calls) == 1
    assert llm_calls == 1
    assert not list(Path(fs.tasks_dir()).glob(f".{accepted_id}.*.tmp"))
    assert list(Path(fs.images_dir(accepted_id)).iterdir())
    conn.close()


def test_deleted_accepted_task_is_not_revived_during_materialization(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text(
        "# 第一章\n\n![图](data:image/png;base64,aW1hZ2U=)\n",
        encoding="utf-8",
    )
    orch.parse_file(str(source_file))
    accepted_id = "accepted-deleted-during-materialization"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    snapshots = []
    real_snapshot = orchestrator_module.snapshot
    real_materialize = repo.materialize_cached_task
    files_published = threading.Event()
    allow_database_write = threading.Event()
    errors = []

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    def paused_materialize(*args):
        files_published.set()
        assert allow_database_write.wait(timeout=2)
        return real_materialize(*args)

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)
    monkeypatch.setattr(repo, "materialize_cached_task", paused_materialize)

    def run_parse():
        try:
            orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run_parse)
    worker.start()
    assert files_published.wait(timeout=2)
    repo.delete_task(accepted_id)
    allow_database_write.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert "no longer eligible" in str(errors[0])
    assert repo.get_task(accepted_id) is None
    assert not Path(fs.task_path(accepted_id)).exists()
    assert not list(Path(fs.tasks_dir()).glob(f".{accepted_id}.*.tmp"))
    assert snapshots and not Path(snapshots[-1]).exists()
    conn.close()


def test_cache_publication_inode_swap_during_db_commit_rolls_back_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "cache-race.md"
    source_file.write_text("# Cache\n\nbody", encoding="utf-8")
    orch.parse_file(str(source_file), force=True)
    accepted_id = "accepted-cache-db-race"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    real_materialize = repo.materialize_cached_task
    target = Path(fs.task_path(accepted_id))
    displaced = target.with_name(f"{accepted_id}-displaced")
    replacement_marker = target / "replacement.md"
    swapped = False

    def swap_before_database_commit(*args: object) -> None:
        nonlocal swapped
        target.rename(displaced)
        target.mkdir(mode=0o700)
        replacement_marker.write_text("replacement", encoding="utf-8")
        swapped = True
        real_materialize(*args)

    monkeypatch.setattr(repo, "materialize_cached_task", swap_before_database_commit)

    with pytest.raises(RuntimeError, match="identity changed"):
        orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    accepted = repo.get_task(accepted_id)
    assert swapped
    assert accepted is not None and accepted.status == "WAITING"
    assert repo.list_sections(accepted_id) == []
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    assert (displaced / "merged.md").is_file()
    conn.close()


def test_cache_publication_receipt_matches_published_directory_identity(tmp_path: Path) -> None:
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "cache-receipt.md"
    source_file.write_text("# Cache receipt\n\nbody", encoding="utf-8")
    orch.parse_file(str(source_file), force=True)
    accepted_id = "accepted-cache-receipt"
    preregister_waiting_task(repo, accepted_id, str(source_file))

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    receipt = Path(fs.task_path(accepted_id)) / ".publication-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    task_stat = receipt.parent.stat(follow_symlinks=False)
    assert result["cached"] is True
    assert payload["kind"] == "cache"
    assert payload["task_id"] == accepted_id
    assert payload["directory_identity"] == [task_stat.st_dev, task_stat.st_ino]
    conn.close()


def test_explicit_task_id_full_parse_uses_update_only_promotion_and_cleans_snapshot(
    tmp_path, monkeypatch
):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n正文。\n", encoding="utf-8")
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)

    with pytest.raises(repository_module.TaskPromotionConflict, match="no longer eligible"):
        orch.parse_file(
            str(source_file),
            force=True,
            task_id="deleted-before-full-parse",
            batch_id="batch-1",
        )

    assert repo.get_task("deleted-before-full-parse") is None
    assert not Path(fs.task_path("deleted-before-full-parse")).exists()
    assert snapshots and not Path(snapshots[-1]).exists()
    conn.close()


def test_cache_target_symlink_is_nonrecoverable_and_never_writes_outside(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n正文。\n", encoding="utf-8")
    orch.parse_file(str(source_file))
    accepted_id = "accepted-target-symlink"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "0.raw.md"
    sentinel.write_text("external", encoding="utf-8")
    (Path(fs.tasks_dir()) / accepted_id).symlink_to(outside, target_is_directory=True)
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)

    with pytest.raises(fs_layout_module.UnsafeTaskPathError, match="unsafe|symlink|target"):
        orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    accepted = repo.get_task(accepted_id)
    assert accepted is not None and accepted.status == "WAITING"
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert not (outside / "merged.md").exists()
    assert snapshots and not Path(snapshots[-1]).exists()
    conn.close()


class CountingScandir:
    def __init__(self, wrapped, counter):
        self.wrapped = wrapped
        self.counter = counter

    def __enter__(self):
        self.wrapped.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.wrapped.__exit__(exc_type, exc_value, traceback)

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.wrapped)
        self.counter.append(entry.name)
        return entry


def create_limited_image_tree(images_dir, limit_kind):
    if limit_kind == "directories":
        for index in range(5):
            (images_dir / f"dir-{index}").mkdir()
        return 2
    if limit_kind == "mixed_entries":
        (images_dir / "dir-a").mkdir()
        (images_dir / "dir-b").mkdir()
        for index in range(3):
            (images_dir / f"file-{index}.png").write_bytes(b"x")
        return 2
    if limit_kind == "depth":
        (images_dir / "one" / "two" / "three").mkdir(parents=True)
        return 2
    for index in range(5):
        (images_dir / f"file-{index}.png").write_bytes(b"x")
    return 2


@pytest.mark.parametrize(
    "limit_kind",
    ["directories", "mixed_entries", "depth", "file_count"],
)
def test_cached_source_image_tree_validation_stops_at_boundary(tmp_path, monkeypatch, limit_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    images_dir = Path(fs.images_dir(source_id))
    limit = create_limited_image_tree(images_dir, limit_kind)
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_ENTRIES",
        10 if limit_kind in {"depth", "file_count"} else 2,
        raising=False,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_DEPTH",
        2 if limit_kind == "depth" else 10,
        raising=False,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_FILES",
        2 if limit_kind == "file_count" else 10,
    )
    consumed = []
    real_scandir = orchestrator_module.os.scandir

    def counting_scandir(path):
        return CountingScandir(real_scandir(path), consumed)

    monkeypatch.setattr(orchestrator_module.os, "scandir", counting_scandir)
    accepted_id = f"accepted-source-limit-{limit_kind}"

    with pytest.raises(orchestrator_module._InvalidCachedTask):
        orch._load_cached_task(repo.get_task(source_id))

    assert len(consumed) == limit + 1
    assert not Path(fs.task_path(accepted_id)).exists()
    conn.close()


@pytest.mark.parametrize(
    "limit_kind",
    ["directories", "mixed_entries", "depth", "file_count"],
)
def test_cached_copy_tree_limits_fall_back_without_cache_publish(tmp_path, monkeypatch, limit_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    images_dir = Path(fs.images_dir(source_id))
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_ENTRIES",
        10 if limit_kind in {"depth", "file_count"} else 2,
        raising=False,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_DEPTH",
        2 if limit_kind == "depth" else 10,
        raising=False,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_CACHED_IMAGE_FILES",
        2 if limit_kind == "file_count" else 10,
    )
    real_open = orchestrator_module.os.open
    root_open_count = 0
    injected = False

    def injecting_open(path, flags, *args, **kwargs):
        nonlocal root_open_count, injected
        is_source_root = not isinstance(path, int) and Path(path) == images_dir
        if is_source_root and flags & getattr(os, "O_DIRECTORY", 0):
            root_open_count += 1
            if root_open_count == 2:
                create_limited_image_tree(images_dir, limit_kind)
                injected = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(orchestrator_module.os, "open", injecting_open)
    accepted_id = f"accepted-copy-limit-{limit_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert injected
    assert result["cached"] is False
    assert len(parse_calls) == 1
    assert not list(Path(fs.tasks_dir()).glob(f".{accepted_id}.*.tmp"))
    assert not list(Path(fs.images_dir(accepted_id)).rglob("*"))
    conn.close()


@pytest.mark.parametrize("limit_kind", ["file_count", "single_bytes", "total_bytes"])
def test_cached_image_copy_limits_fall_back_to_parse(tmp_path, monkeypatch, limit_kind):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    images_dir = Path(fs.images_dir(source_id))
    if limit_kind == "file_count":
        (images_dir / "one.png").write_bytes(b"1")
        (images_dir / "two.png").write_bytes(b"2")
        monkeypatch.setattr(orchestrator_module, "MAX_CACHED_IMAGE_FILES", 1, raising=False)
    elif limit_kind == "single_bytes":
        (images_dir / "large.png").write_bytes(b"1234")
        monkeypatch.setattr(
            orchestrator_module,
            "MAX_CACHED_IMAGE_FILE_BYTES",
            3,
            raising=False,
        )
    else:
        (images_dir / "one.png").write_bytes(b"123")
        (images_dir / "two.png").write_bytes(b"456")
        monkeypatch.setattr(
            orchestrator_module,
            "MAX_CACHED_IMAGE_TOTAL_BYTES",
            5,
            raising=False,
        )
    accepted_id = f"accepted-limit-{limit_kind}"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


def test_markdown_image_scan_work_is_linearly_bounded(monkeypatch):
    malformed = "![" * 300
    work_factor = 4
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_MARKDOWN_IMAGE_SCAN_WORK_FACTOR",
        work_factor,
        raising=False,
    )
    expected_limit = len(malformed) * work_factor

    with pytest.raises(orchestrator_module._InvalidCachedTask) as exc_info:
        Orchestrator._markdown_image_links(malformed)

    message = str(exc_info.value)
    assert f"used={expected_limit + 1}" in message
    assert f"limit={expected_limit}" in message


def test_markdown_image_scan_budget_failure_falls_back_to_parse(tmp_path, monkeypatch):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    source_result = orch.parse_file(str(source_file))
    source_id = source_result["task_id"]
    malformed_ai = "### AI 解读\n\n" + "![" * 300
    rewrite_cached_ai(orch, repo, source_id, str(source_file), malformed_ai)
    monkeypatch.setattr(
        orchestrator_module,
        "MAX_MARKDOWN_IMAGE_SCAN_WORK_FACTOR",
        4,
        raising=False,
    )
    accepted_id = "accepted-scan-budget"
    preregister_waiting_task(repo, accepted_id, str(source_file))
    parse_calls = count_parser_calls(orch, monkeypatch)

    result = orch.parse_file(str(source_file), task_id=accepted_id, batch_id="batch-1")

    assert result["cached"] is False
    assert len(parse_calls) == 1
    conn.close()


@pytest.mark.parametrize(
    "failure_stage",
    ["file_hash", "cache_lookup", "cache_load", "create_task"],
)
def test_pre_registration_failures_clean_snapshot(tmp_path, monkeypatch, failure_stage):
    orch, repo, fs, conn = make_serving_orchestrator(tmp_path)
    source_file = tmp_path / "course.md"
    source_file.write_text("# 第一章\n\n管理决策正文。\n", encoding="utf-8")
    if failure_stage == "cache_load":
        orch.parse_file(str(source_file))
    snapshots = []
    real_snapshot = orchestrator_module.snapshot

    def tracking_snapshot(path):
        snap = real_snapshot(path)
        snapshots.append(snap)
        return snap

    monkeypatch.setattr(orchestrator_module, "snapshot", tracking_snapshot)
    if failure_stage == "file_hash":
        monkeypatch.setattr(
            orchestrator_module,
            "file_sha256",
            lambda _path: (_ for _ in ()).throw(RuntimeError("hash failed")),
        )
    elif failure_stage == "cache_lookup":
        monkeypatch.setattr(
            orch.cache,
            "find_completed_task_by_file_sha256",
            lambda _sha: (_ for _ in ()).throw(sqlite3.OperationalError("lookup failed")),
        )
    elif failure_stage == "cache_load":
        monkeypatch.setattr(
            repo,
            "list_sections",
            lambda _task_id: (_ for _ in ()).throw(sqlite3.OperationalError("load failed")),
        )
    else:
        monkeypatch.setattr(
            repo,
            "create_task",
            lambda _task: (_ for _ in ()).throw(sqlite3.OperationalError("create failed")),
        )

    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        orch.parse_file(str(source_file), force=failure_stage == "create_task")

    assert snapshots and not Path(snapshots[-1]).exists()
    conn.close()
