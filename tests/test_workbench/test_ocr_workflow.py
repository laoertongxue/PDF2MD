import copy
import hashlib
import json
import multiprocessing
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest
from test_ocr_orchestrator import FakeEngines, _orchestrator, _run

from parsing_core.workbench.ocr import workflow as workflow_module
from parsing_core.workbench.ocr.chapters import detect_chapter_tree
from parsing_core.workbench.ocr.orchestrator import BatchStatus
from parsing_core.workbench.ocr.workflow import (
    OcrWorkflow,
    WorkflowBlockedError,
    WorkflowStatus,
    bind_published_note,
    build_confirmation,
    status_payload,
)


def _hold_publication_lock(state_root: str, started, entered, release) -> None:
    started.set()
    paths = workflow_module.workflow_paths(state_root)
    with workflow_module._publication_transaction_lock(paths):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("publication lock test timed out")


def _acquire_publication_lock_once(state_root: str, result) -> None:
    try:
        paths = workflow_module.workflow_paths(state_root)
        with workflow_module._publication_transaction_lock(paths):
            result.send(("acquired", ""))
    except BaseException as exc:
        result.send(("failed", f"{type(exc).__name__}: {exc}"))
    finally:
        result.close()


def _assert_child_can_acquire_publication_lock(state_root: Path) -> None:
    context = multiprocessing.get_context("fork")
    parent_result, child_result = context.Pipe(duplex=False)
    process = context.Process(
        target=_acquire_publication_lock_once,
        args=(str(state_root), child_result),
    )
    process.start()
    child_result.close()
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("a new process could not acquire the released publication locks")
    try:
        assert process.exitcode == 0
        assert parent_result.poll(1)
        assert parent_result.recv() == ("acquired", "")
    finally:
        parent_result.close()


_EXIT_DURING_PUBLICATION_TOKEN_WRITE = 97


def _crash_during_publication_token_write(state_root: str, prefix_length: int) -> None:
    paths = workflow_module.workflow_paths(state_root)

    def write_prefix_and_exit(fd: int, content: bytes) -> None:
        assert len(content) == 65
        assert content[-1:] == b"\n"
        if prefix_length:
            assert os.write(fd, content[:prefix_length]) == prefix_length
        os.fsync(fd)
        os._exit(_EXIT_DURING_PUBLICATION_TOKEN_WRITE)

    workflow_module._write_all = write_prefix_and_exit
    with workflow_module._publication_transaction_lock(paths):
        raise AssertionError("token write crash point was not reached")


def _complete_workflow_fixture(tmp_path: Path, *, publish_note: bool = True):
    engines = FakeEngines()
    adjudicate = engines.codex.adjudicate_page

    def adjudicate_with_chapter(*args, **kwargs):
        result = adjudicate(*args, **kwargs)
        result.payload["final_blocks"][0]["text"] = "1 战略管理"
        return result

    engines.codex.adjudicate_page = adjudicate_with_chapter
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status.value == "completed"
    state_root = tmp_path / "ocr-state"
    final = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    if not publish_note:
        return engines, state_root, final
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    metadata = _note_metadata(final, tree, confirmation)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    def generate(output_path: Path):
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    workflow.generate_and_publish(
        generate,
        expected_final=final,
        expected_tree=tree,
        confirmation=confirmation,
    )
    return engines, state_root, final


def _legacy_v1_final(final: dict) -> dict:
    legacy = copy.deepcopy(final)
    legacy["schema_version"] = 1
    legacy.pop("run_config", None)
    return legacy


def _legacy_v1_state(state: dict) -> dict:
    legacy = copy.deepcopy(state)
    legacy["schema_version"] = 1
    legacy.pop("run_config", None)
    return legacy


def _prepare_chapter_context(state_root: Path, final: dict):
    pages = workflow_module._normalized_completed_pages(final)
    tree = detect_chapter_tree(pages, input_fingerprint=final["input_fingerprint"])
    confirmation = build_confirmation(tree, tree["chapters"][0]["id"])
    (state_root / "chapter-tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (state_root / "chapter-confirmation.json").write_text(
        json.dumps(confirmation), encoding="utf-8"
    )
    return pages, tree, confirmation


def _valid_markdown(
    final: dict, tree: dict, confirmation: dict, *, concept: str = "概念内容"
) -> str:
    return (
        "\n".join(
            [
                "# 1 战略管理",
                f"> 输入指纹：`{final['input_fingerprint']}`",
                f"> 章节指纹：`{confirmation['chapter_fingerprint']}`",
                f"> OCR 证据指纹：`{tree['evidence_fingerprint']}`",
                "> 精读规则版本：`mba-intensive-reading-v1`",
                "> 模型：`deepseek-v4-pro`",
                "> Prompt 指纹：`prompt-fingerprint`",
                "",
                "## 原文证据",
                "[src:test:p1:codex-block]",
                "## 核心概念",
                concept,
                "## 通俗、有趣、生活化的解释",
                "生活化解释",
                "## 教材案例解读",
                "案例内容",
                "## 实际例子与问题解决",
                "问题解决",
                "## 实际应用",
                "应用内容",
                "## 知识结构图",
                "```mermaid",
                "flowchart TD",
                "  A[概念] --> B[应用]",
                "```",
                "## 应用流程图",
                "```mermaid",
                "flowchart LR",
                "  A[识别] --> B[行动]",
                "```",
            ]
        )
        + "\n"
    )


def _note_metadata(final: dict, tree: dict, confirmation: dict) -> dict[str, object]:
    chapter = confirmation["chapter"]
    return {
        "model": "deepseek-v4-pro",
        "prompt_rules_version": "mba-intensive-reading-v1",
        "source_id": "test",
        "chapter_id": confirmation["chapter_id"],
        "chapter_number": chapter["number"],
        "chapter_title": chapter["title"],
        "page_start": chapter["page_start"],
        "page_end": chapter["page_end"],
        "citation_ids": ["[src:test:p1:codex-block]"],
        "chapter_fingerprint": confirmation["chapter_fingerprint"],
        "prompt_fingerprint": "prompt-fingerprint",
        "input_fingerprint": final["input_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
    }


def _published_note_metadata(state_root: Path, final: dict[str, object]) -> dict[str, object]:
    tree = json.loads((state_root / "chapter-tree.json").read_text(encoding="utf-8"))
    confirmation = json.loads(
        (state_root / "chapter-confirmation.json").read_text(encoding="utf-8")
    )
    return _note_metadata(final, tree, confirmation)


def _write_content_addressed_publication(
    state_root: Path, final: dict, tree: dict, confirmation: dict, markdown: str
) -> Path:
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    artifact = state_root / f"intensive-reading.{digest}.md"
    artifact.write_text(markdown, encoding="utf-8")
    (state_root / "note-publication.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "final_snapshot_sha256": workflow_module._json_fingerprint(final),
                "artifact_basename": artifact.name,
                "artifact_sha256": digest,
                "proposal_fingerprint": tree["proposal_fingerprint"],
                "metadata": _note_metadata(final, tree, confirmation),
            }
        ),
        encoding="utf-8",
    )
    return artifact


def _seed_legacy_published_final(tmp_path: Path) -> tuple[Path, Path, dict]:
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
    legacy = _legacy_v1_final(final)
    legacy.update(
        {
            "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
            "model": "deepseek-v4-pro",
            "ruleset": "mba-intensive-reading-v1",
            "prompt_fingerprint": "prompt-fingerprint",
            "chapter_fingerprint": confirmation["chapter_fingerprint"],
            "note_input_fingerprint": legacy["input_fingerprint"],
            "note_evidence_fingerprint": tree["evidence_fingerprint"],
        }
    )
    workflow_module._atomic_json(state_root / "batch-final.json", legacy)
    return tmp_path / "book.pdf", state_root, legacy


def _seed_inferable_legacy_running_state(
    tmp_path: Path,
) -> tuple[FakeEngines, Path, dict]:
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    result = orchestrator.run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=320,
        languages=["en-US"],
        sample_rate=0.05,
    )
    assert result.status is BatchStatus.COMPLETED
    state_root = tmp_path / "ocr-state"
    final_path = state_root / "batch-final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    legacy = _legacy_v1_final(final)
    legacy["status"] = BatchStatus.RUNNING.value
    final_path.unlink()
    workflow_module._atomic_json(state_root / "batch-state.json", legacy)
    engines.calls.clear()
    return engines, state_root, legacy


def _replace_regular_bytes(path: Path, content: bytes) -> None:
    try:
        original_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        original_mode = 0o600
    temporary = path.with_name(f".{path.name}.competitor-{os.getpid()}")
    temporary.write_bytes(content)
    temporary.chmod(original_mode)
    os.replace(temporary, path)


def _migrate_legacy_completed_process(
    source_path: str,
    state_root: str,
    ready,
    start,
    results,
) -> None:
    ready.set()
    if not start.wait(5):
        raise RuntimeError("migration race test timed out")

    def unexpected_factory(_cancel):
        raise AssertionError("completed work must not rerun")

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=unexpected_factory,
    )
    results.put(workflow.status())


def _pause_completed_migration_process(
    source_path: str,
    state_root: str,
    pause_point: str,
    entered,
    release,
    results,
) -> None:
    def checkpoint(point: str) -> None:
        if point == pause_point:
            entered.set()
            if not release.wait(10):
                raise RuntimeError("migration pause timed out")

    workflow_module._migration_checkpoint = checkpoint
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    results.put(workflow.status())


_EXIT_AFTER_SOURCE_SWAP = 81
_EXIT_DURING_POST_SWAP_FSYNC = 82
_EXIT_AFTER_COMMIT_READY = 83
_EXIT_DURING_JOURNAL_RETIRE = 84
_EXIT_DURING_CLEANUP = 85
_EXIT_AFTER_RUNNING_SOURCE_SWAP = 86
_EXIT_AFTER_REAL_BOUNDARY = 87
_EXIT_DURING_WORKER = 88
_EXIT_WITHOUT_EXPECTED_CRASH = 89
_EXIT_DURING_PRE_INTENT_WINDOW = 90
_EXIT_DURING_CANDIDATE_PUBLICATION = 91
_EXIT_DURING_DURABLE_SIDECAR = 92
_EXIT_DURING_SIDECAR_RECEIPT = 93


def _append_transaction_marker(path: str, transaction_id: str) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.write(fd, f"{transaction_id}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _crash_during_pre_intent_window_process(
    source_path: str,
    state_root: str,
    journal_kind: str,
    boundary: str,
    transaction_marker: str,
) -> None:
    original_create_anchor = workflow_module._create_transaction_anchor
    original_create_manifest = workflow_module._create_manifest_candidate
    original_publish = workflow_module._rename_no_replace
    original_resume_completed = workflow_module._resume_completed_migration
    original_resume_running = workflow_module._resume_running_migration
    active_transaction_id: str | None = None

    def create_anchor(paths, transaction_id, *, error_code, **kwargs):
        nonlocal active_transaction_id
        active_transaction_id = transaction_id
        if boundary == "before-anchor":
            _append_transaction_marker(transaction_marker, transaction_id)
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        existed = (Path(paths.root) / f".ocr-v1-v2-{transaction_id}.anchor").exists()
        result = original_create_anchor(
            paths,
            transaction_id,
            error_code=error_code,
            **kwargs,
        )
        if boundary == "after-anchor" and existed:
            _append_transaction_marker(transaction_marker, transaction_id)
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        return result

    def create_manifest(paths, transaction_id, publication, **kwargs):
        nonlocal active_transaction_id
        active_transaction_id = transaction_id
        existed = (Path(paths.root) / f".ocr-v1-v2-{transaction_id}.manifest").exists()
        result = original_create_manifest(
            paths,
            transaction_id,
            publication,
            **kwargs,
        )
        if boundary == "after-manifest" and existed:
            _append_transaction_marker(transaction_marker, transaction_id)
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        return result

    def publish(source, target):
        result = original_publish(source, target)
        target_path = Path(target)
        if active_transaction_id is None:
            return result
        if (
            boundary == "after-anchor"
            and target_path.name == f".ocr-v1-v2-{active_transaction_id}.anchor"
        ) or (
            boundary == "after-manifest"
            and target_path.name == f".ocr-v1-v2-{active_transaction_id}.manifest"
        ):
            _append_transaction_marker(transaction_marker, active_transaction_id)
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        return result

    def checkpoint(point: str) -> None:
        if point != f"{journal_kind}_intent_prepared_cas" or boundary != "before-prepared-cas":
            return
        journal = json.loads(
            (Path(state_root) / "ocr-v1-v2-migration.json").read_text(encoding="utf-8")
        )
        _append_transaction_marker(transaction_marker, journal["transaction_id"])
        os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)

    def resume_completed(paths, source, journal, publication_lock):
        if boundary == "before-prepared-cas" and journal["phase"] == "prepared":
            _append_transaction_marker(transaction_marker, journal["transaction_id"])
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        return original_resume_completed(paths, source, journal, publication_lock)

    def resume_running(paths, source, journal, publication_lock):
        if boundary == "before-prepared-cas" and journal["phase"] == "prepared":
            _append_transaction_marker(transaction_marker, journal["transaction_id"])
            os._exit(_EXIT_DURING_PRE_INTENT_WINDOW)
        return original_resume_running(paths, source, journal, publication_lock)

    workflow_module._create_transaction_anchor = create_anchor
    workflow_module._create_manifest_candidate = create_manifest
    workflow_module._rename_no_replace = publish
    workflow_module._resume_completed_migration = resume_completed
    workflow_module._resume_running_migration = resume_running
    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("worker must not start before migration commit")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _candidate_prefix_matches_label(prefix: str, label: str) -> bool:
    suffixes = {
        "anchor": ".anchor.new-",
        "manifest": ".manifest.new-",
        "backup": ".batch-final.v1.backup.json.",
        "artifact": ".intensive-reading.",
        "swap": ".swap.new-",
        "commit": ".commit.new-",
    }
    return suffixes[label] in prefix


def _unpublished_candidate_paths(
    state_root: Path,
    transaction_id: str,
    label: str,
) -> tuple[Path, ...]:
    owned_prefix = f".ocr-v1-v2-{transaction_id}.candidate-{label}."
    durable_prefix = f".ocr-v1-v2-{transaction_id}.candidate-{label}-"
    legacy_predicates = {
        "anchor": lambda name: name.startswith(f"..ocr-v1-v2-{transaction_id}.anchor.new-"),
        "manifest": lambda name: name.startswith(f"..ocr-v1-v2-{transaction_id}.manifest.new-"),
        "backup": lambda name: name.startswith(".batch-final.v1.backup.json."),
        "artifact": lambda name: name.startswith(".note-artifact."),
        "swap": lambda name: name.startswith(f"..ocr-v1-v2-{transaction_id}.swap.new-"),
        "commit": lambda name: name.startswith(f"..ocr-v1-v2-{transaction_id}.commit.new-"),
    }
    return tuple(
        sorted(
            (
                path
                for path in state_root.iterdir()
                if path.name.startswith(owned_prefix)
                or path.name.startswith(durable_prefix)
                or legacy_predicates[label](path.name)
            ),
            key=lambda path: path.name,
        )
    )


def _crash_during_candidate_publication_process(
    source_path: str,
    state_root: str,
    label: str,
    transaction_marker: str,
) -> None:
    original_write_temporary = workflow_module._write_temporary_regular

    def write_temporary(parent, *, prefix, content, error_code="ocr_state_invalid", **kwargs):
        result = original_write_temporary(
            parent,
            prefix=prefix,
            content=content,
            error_code=error_code,
            **kwargs,
        )
        if not _candidate_prefix_matches_label(prefix, label):
            return result
        journal = json.loads(
            (Path(state_root) / "ocr-v1-v2-migration.json").read_text(encoding="utf-8")
        )
        _append_transaction_marker(transaction_marker, journal["transaction_id"])
        os._exit(_EXIT_DURING_CANDIDATE_PUBLICATION)

    workflow_module._write_temporary_regular = write_temporary
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_during_durable_sidecar_process(
    source_path: str,
    state_root: str,
    label: str,
    boundary: str,
    transaction_marker: str,
) -> None:
    expected_point = f"migration-sidecar:{label}:{boundary}"

    def checkpoint(point: str) -> None:
        if point != expected_point:
            return
        journal = json.loads(
            (Path(state_root) / "ocr-v1-v2-migration.json").read_text(encoding="utf-8")
        )
        _append_transaction_marker(transaction_marker, journal["transaction_id"])
        os._exit(_EXIT_DURING_DURABLE_SIDECAR)

    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_during_sidecar_receipt_process(
    source_path: str,
    state_root: str,
    boundary: str,
    transaction_marker: str,
) -> None:
    expected_point = f"migration-sidecar-receipt:anchor:declared:{boundary}"

    def checkpoint(point: str) -> None:
        if point != expected_point:
            return
        journal = json.loads(
            (Path(state_root) / "ocr-v1-v2-migration.json").read_text(encoding="utf-8")
        )
        _append_transaction_marker(transaction_marker, journal["transaction_id"])
        os._exit(_EXIT_DURING_SIDECAR_RECEIPT)

    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _sidecar_receipt_path(state_root: Path, transaction_id: str, label: str) -> Path:
    return state_root / f".ocr-v1-v2-{transaction_id}.sidecar-{label}.json"


def _read_sidecar_receipt(
    state_root: Path,
    transaction_id: str,
    label: str,
) -> dict[str, object]:
    return json.loads(
        _sidecar_receipt_path(state_root, transaction_id, label).read_text(encoding="utf-8")
    )


def _receipt_object_path(
    state_root: Path,
    receipt: dict[str, object],
    field: str,
) -> Path:
    basename = receipt[field]
    assert isinstance(basename, str)
    assert Path(basename).name == basename
    return state_root / basename


def _crash_completed_source_swap_process(
    source_path: str,
    state_root: str,
    boundary: str,
    competitor: bytes | None,
) -> None:
    final_path = Path(state_root) / "batch-final.json"
    original_rename_swap = workflow_module._rename_swap
    original_fsync_directory = workflow_module._fsync_directory
    source_swap_happened = False
    competitor_installed = False

    def checkpoint(point: str) -> None:
        nonlocal competitor_installed
        if (
            point == "completed_source_cas_ready"
            and competitor is not None
            and not competitor_installed
        ):
            _replace_regular_bytes(final_path, competitor)
            competitor_installed = True

    def rename_swap(first: Path, second: Path) -> None:
        nonlocal source_swap_happened
        original_rename_swap(first, second)
        if Path(second) != final_path:
            return
        source_swap_happened = True
        if boundary == "after-swap":
            os._exit(_EXIT_AFTER_SOURCE_SWAP)

    def fsync_directory(path: Path) -> None:
        if (
            boundary == "post-swap-fsync"
            and source_swap_happened
            and Path(path) == final_path.parent
        ):
            os._exit(_EXIT_DURING_POST_SWAP_FSYNC)
        original_fsync_directory(path)

    workflow_module._migration_checkpoint = checkpoint
    workflow_module._rename_swap = rename_swap
    workflow_module._fsync_directory = fsync_directory
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_running_source_swap_process(source_path: str, state_root: str) -> None:
    state_path = Path(state_root) / "batch-state.json"
    original_rename_swap = workflow_module._rename_swap

    def rename_swap(first: Path, second: Path) -> None:
        original_rename_swap(first, second)
        if Path(second) == state_path:
            os._exit(_EXIT_AFTER_RUNNING_SOURCE_SWAP)

    workflow_module._rename_swap = rename_swap
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("worker must not start before migration commit")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_completed_at_commit_ready_process(source_path: str, state_root: str) -> None:
    def checkpoint(point: str) -> None:
        if point == "completed_commit_ready":
            os._exit(_EXIT_AFTER_COMMIT_READY)

    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_completed_during_cleanup_process(source_path: str, state_root: str) -> None:
    def checkpoint(point: str) -> None:
        if point == "completed_cleanup_ready":
            os._exit(_EXIT_DURING_CLEANUP)

    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_during_journal_retire_process(
    source_path: str,
    state_root: str,
    competitor: bytes,
    identity_marker: str,
) -> None:
    journal_path = Path(state_root) / "ocr-v1-v2-migration.json"
    marker_path = Path(identity_marker)
    original_path_identity = workflow_module._path_identity
    original_unlink = Path.unlink
    original_rename_no_replace = getattr(workflow_module, "_rename_no_replace", None)
    injected = False

    def path_identity(path: Path) -> tuple[int, int]:
        nonlocal injected
        identity = original_path_identity(path)
        if Path(path) == journal_path and not injected:
            _replace_regular_bytes(journal_path, competitor)
            info = journal_path.lstat()
            marker_path.write_text(f"{info.st_dev}:{info.st_ino}", encoding="ascii")
            injected = True
        return identity

    def unlink(path: Path, *args, **kwargs) -> None:
        original_unlink(path, *args, **kwargs)
        if Path(path) == journal_path and injected:
            os._exit(_EXIT_DURING_JOURNAL_RETIRE)

    workflow_module._path_identity = path_identity
    Path.unlink = unlink
    if original_rename_no_replace is not None:

        def rename_no_replace(source: Path, target: Path) -> None:
            original_rename_no_replace(source, target)
            if Path(source) == journal_path and injected:
                os._exit(_EXIT_DURING_JOURNAL_RETIRE)

        workflow_module._rename_no_replace = rename_no_replace

    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_completed_after_real_boundary_process(
    source_path: str,
    state_root: str,
    boundary: str,
) -> None:
    root = Path(state_root)
    journal_path = root / "ocr-v1-v2-migration.json"
    backup_path = root / "batch-final.v1.backup.json"
    manifest_path = root / "note-publication.json"
    original_publish = workflow_module._rename_no_replace
    original_swap = workflow_module._rename_swap
    original_link = workflow_module.os.link
    original_fsync_directory = workflow_module._fsync_directory
    pending_fsync: str | None = None

    def publish(source: Path, target: Path) -> None:
        nonlocal pending_fsync
        original_publish(source, target)
        target_path = Path(target)
        if boundary == "journal-publish-fsync" and target_path == journal_path:
            pending_fsync = boundary
        elif boundary == "backup-publish-fsync" and target_path == backup_path:
            pending_fsync = boundary
        elif (
            boundary == "artifact-publish-fsync"
            and target_path.name.startswith("intensive-reading.")
            and target_path.suffix == ".md"
        ):
            pending_fsync = boundary
        elif boundary == "witness-create-fsync" and target_path.name.endswith(".commit"):
            pending_fsync = boundary

    def swap(first: Path, second: Path) -> None:
        original_swap(first, second)
        if boundary == "journal-phase-cas" and Path(second) == journal_path:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    def link(source, target, *, follow_symlinks=True):
        nonlocal pending_fsync
        result = original_link(source, target, follow_symlinks=follow_symlinks)
        if boundary == "manifest-link-fsync" and Path(target) == manifest_path:
            pending_fsync = boundary
        return result

    def fsync_directory(path: Path) -> None:
        original_fsync_directory(path)
        if pending_fsync == boundary:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    def checkpoint(point: str) -> None:
        points = {
            "journal-commit": "completed_journal_committed",
            "witness-done": "completed_witness_done",
            "commit-sealed": "completed_commit_sealed",
        }
        if points.get(boundary) == point:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    workflow_module._rename_no_replace = publish
    workflow_module._rename_swap = swap
    workflow_module.os.link = link
    workflow_module._fsync_directory = fsync_directory
    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_after_journal_phase_cas_process(
    source_path: str,
    state_root: str,
    journal_kind: str,
    target_phase: str,
) -> None:
    journal_path = Path(state_root) / "ocr-v1-v2-migration.json"
    original_swap = workflow_module._rename_swap

    def swap(first: Path, second: Path) -> None:
        original_swap(first, second)
        if Path(second) != journal_path:
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal["kind"] == journal_kind and journal["phase"] == target_phase:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    workflow_module._rename_swap = swap
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("worker must not start before migration commit")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_after_publication_before_fsync_process(
    source_path: str,
    state_root: str,
    boundary: str,
) -> None:
    root = Path(state_root)
    journal_path = root / "ocr-v1-v2-migration.json"
    backup_path = root / "batch-final.v1.backup.json"
    manifest_path = root / "note-publication.json"
    original_publish = workflow_module._rename_no_replace
    original_link = workflow_module.os.link

    def publish(source: Path, target: Path) -> None:
        original_publish(source, target)
        target_path = Path(target)
        should_exit = (
            (boundary == "journal-publish" and target_path == journal_path)
            or (boundary == "backup-publish" and target_path == backup_path)
            or (
                boundary == "artifact-publish"
                and target_path.name.startswith("intensive-reading.")
                and target_path.suffix == ".md"
            )
            or (boundary == "witness-publish" and target_path.name.endswith(".commit"))
        )
        if should_exit:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    def link(source, target, *, follow_symlinks=True):
        result = original_link(source, target, follow_symlinks=follow_symlinks)
        if boundary == "manifest-link" and Path(target) == manifest_path:
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)
        return result

    workflow_module._rename_no_replace = publish
    workflow_module.os.link = link
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_after_journal_identity_publication_process(
    source_path: str,
    state_root: str,
) -> None:
    def checkpoint(point: str) -> None:
        if point == "completed_journal_identity_published":
            os._exit(_EXIT_AFTER_REAL_BOUNDARY)

    workflow_module._migration_checkpoint = checkpoint
    OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _crash_running_worker_process(
    source_path: str,
    state_root: str,
    attempt_path: str,
) -> None:
    class CrashingOrchestrator:
        def run_batch(self, pdf_path, **kwargs):
            record = {
                "source_path": str(pdf_path),
                "pages": list(kwargs["pages"]),
                "dpi": kwargs["dpi"],
                "languages": list(kwargs["languages"]),
                "sample_rate": kwargs["sample_rate"],
            }
            with Path(attempt_path).open("w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(_EXIT_DURING_WORKER)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: CrashingOrchestrator(),
    )
    if workflow._thread is not None:
        workflow._thread.join(timeout=10)
    os._exit(_EXIT_WITHOUT_EXPECTED_CRASH)


def _directory_contains_identity(root: Path, identity: tuple[int, int]) -> bool:
    for path in root.iterdir():
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            return True
    return False


def _observe_completed_migration_process(
    source_path: str,
    state_root: str,
    observed_point: str,
    started,
    entered,
    results,
) -> None:
    def checkpoint(point: str) -> None:
        if point == observed_point:
            entered.set()

    workflow_module._migration_checkpoint = checkpoint
    started.set()
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: (_ for _ in ()).throw(
            AssertionError("completed work must not rerun")
        ),
    )
    results.put(workflow.status())


def _resume_legacy_running_process(
    label: str,
    source_path: str,
    state_root: str,
    worker_started,
    release_worker,
    constructed,
    worker_count,
    results,
) -> None:
    class BlockingOrchestrator:
        def run_batch(self, *_args, **_kwargs):
            with worker_count.get_lock():
                worker_count.value += 1
            worker_started.set()
            if not release_worker.wait(10):
                raise RuntimeError("running migration worker test timed out")

            class Result:
                status = BatchStatus.CANCELLED
                error = "ocr_cancelled"

            return Result()

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: BlockingOrchestrator(),
    )
    results.put((label, workflow._thread is not None, workflow.status()["status"]))
    constructed.set()
    if workflow._thread is not None:
        workflow._thread.join(timeout=12)
        if workflow._thread.is_alive():
            raise RuntimeError("running migration worker did not stop")


def _published_artifact_path(state_root: Path) -> Path:
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    return state_root / manifest["artifact_basename"]


def test_status_payload_publishes_only_a_complete_validated_result(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path)

    payload = status_payload(
        status=WorkflowStatus.COMPLETED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert payload["markdown_path"] is not None


def test_generate_and_publish_uses_immutable_content_addressed_artifact(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    markdown = _valid_markdown(final, tree, confirmation)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    generated_paths: list[Path] = []

    def generate(output_path: Path):
        generated_paths.append(output_path)
        assert output_path.parent == state_root
        assert output_path != workflow.paths.note
        assert output_path.name.startswith(".intensive-reading.")
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    note, artifact_path = workflow.generate_and_publish(
        generate,
        expected_final=final,
        expected_tree=tree,
        confirmation=confirmation,
    )

    digest = hashlib.sha256(markdown.encode()).hexdigest()
    assert note["markdown"] == markdown
    assert generated_paths and not generated_paths[0].exists()
    assert artifact_path == state_root / f"intensive-reading.{digest}.md"
    assert artifact_path.read_text(encoding="utf-8") == markdown
    assert workflow.paths.note.exists() is False
    manifest = json.loads(workflow.paths.publication.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "final_snapshot_sha256",
        "artifact_basename",
        "artifact_sha256",
        "proposal_fingerprint",
        "metadata",
    }
    assert manifest["artifact_basename"] == artifact_path.name
    assert manifest["artifact_sha256"] == digest
    assert manifest["metadata"] == metadata
    payload = workflow.status()
    assert payload["publishable"] is True
    assert payload["markdown_path"] == str(artifact_path)


def test_generate_and_publish_accepts_evidence_bound_edit_confirmation(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    edited = copy.deepcopy(confirmation["chapter"])
    edited["title"] = "战略管理：编辑确认"
    confirmation.update(
        revision=2,
        action="edit",
        chapter=edited,
        chapter_fingerprint=workflow_module._chapter_fingerprint(edited),
    )
    workflow_module.persist_chapter_confirmation(
        state_root / "chapter-confirmation.json", confirmation
    )
    metadata = _note_metadata(final, tree, confirmation)
    markdown = _valid_markdown(final, tree, confirmation)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    def generate(output_path: Path):
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    _note, artifact = workflow.generate_and_publish(
        generate,
        expected_final=final,
        expected_tree=tree,
        confirmation=confirmation,
    )

    assert artifact.is_file()
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["chapter_title"] == "战略管理：编辑确认"


def test_failed_generation_keeps_previous_publication_unchanged(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    before = workflow.status()
    before_path = Path(before["markdown_path"])
    before_content = before_path.read_text(encoding="utf-8")

    def fail_after_writing(output_path: Path):
        output_path.write_text("partial replacement", encoding="utf-8")
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        workflow.generate_and_publish(
            fail_after_writing,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    after = workflow.status()
    assert after["publishable"] is True
    assert after["markdown_path"] == str(before_path)
    assert before_path.read_text(encoding="utf-8") == before_content


def test_failed_manifest_commit_keeps_previous_publication_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    replacement = _valid_markdown(final, tree, confirmation, concept="替代概念")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    before = workflow.status()
    before_path = Path(before["markdown_path"])
    before_content = before_path.read_text(encoding="utf-8")
    original_atomic_json = workflow_module._atomic_json

    def reject_manifest(path: Path, value: dict):
        original_atomic_json(path, value)
        if path == workflow.paths.publication:
            raise OSError("manifest commit failed after replace")

    monkeypatch.setattr(workflow_module, "_atomic_json", reject_manifest)

    def generate(output_path: Path):
        output_path.write_text(replacement, encoding="utf-8")
        return {"markdown": replacement, "metadata": metadata}

    with pytest.raises(OSError, match="manifest commit failed after replace"):
        workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    after = workflow.status()
    assert after["publishable"] is True
    assert after["markdown_path"] == str(before_path)
    assert before_path.read_text(encoding="utf-8") == before_content


def test_failed_post_commit_publication_validation_restores_previous_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    replacement = _valid_markdown(final, tree, confirmation, concept="复核失败替代概念")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    previous_manifest = workflow.paths.publication.read_bytes()

    monkeypatch.setattr(
        workflow_module,
        "_publication_status",
        lambda _final, _paths: (False, "ocr_publication_invalid", None),
    )

    def generate(output_path: Path):
        output_path.write_text(replacement, encoding="utf-8")
        return {"markdown": replacement, "metadata": metadata}

    with pytest.raises(ValueError, match="post-commit validation"):
        workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    assert workflow.paths.publication.read_bytes() == previous_manifest


def test_two_workflow_instances_serialize_generation_with_state_root_lock(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    first_markdown = _valid_markdown(final, tree, confirmation, concept="第一版概念")
    second_markdown = _valid_markdown(final, tree, confirmation, concept="第二版概念")
    first = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    second = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()

    def first_generate(output_path: Path):
        first_entered.set()
        assert release_first.wait(5)
        output_path.write_text(first_markdown, encoding="utf-8")
        return {"markdown": first_markdown, "metadata": metadata}

    def second_generate(output_path: Path):
        second_entered.set()
        output_path.write_text(second_markdown, encoding="utf-8")
        return {"markdown": second_markdown, "metadata": metadata}

    def run_second():
        second_started.set()
        return second.generate_and_publish(
            second_generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first.generate_and_publish,
            first_generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
        try:
            assert first_entered.wait(2)
            second_future = executor.submit(run_second)
            assert second_started.wait(2)
            assert second_entered.wait(0.2) is False
            release_first.set()
            _first_note, first_path = first_future.result(timeout=5)
            _second_note, second_path = second_future.result(timeout=5)
        finally:
            release_first.set()

    assert second_entered.is_set()
    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == first_markdown
    assert second_path.read_text(encoding="utf-8") == second_markdown
    payload = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    ).status()
    assert payload["publishable"] is True
    assert payload["markdown_path"] == str(second_path)


def test_status_does_not_wait_for_model_generation_on_the_same_workflow(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    markdown = _valid_markdown(final, tree, confirmation)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    generation_entered = threading.Event()
    release_generation = threading.Event()
    status_finished = threading.Event()

    def generate(output_path: Path):
        generation_entered.set()
        assert release_generation.wait(5)
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    def read_status():
        payload = workflow.status()
        status_finished.set()
        return payload

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(
            workflow.generate_and_publish,
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
        try:
            assert generation_entered.wait(2)
            status = executor.submit(read_status)
            assert status_finished.wait(0.2) is True
            payload = status.result(timeout=2)
            assert payload["status"] == "completed"
            assert payload["publishable"] is False
            release_generation.set()
            publication.result(timeout=5)
        finally:
            release_generation.set()


@pytest.mark.parametrize("changed_file", ["tree", "confirmation"])
def test_generate_rejects_chapter_context_changed_while_waiting_for_lock(
    tmp_path: Path, changed_file: str
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    holder_entered = threading.Event()
    release_holder = threading.Event()
    generation_started = threading.Event()
    callback_called = False

    def hold_lock():
        with workflow_module._publication_transaction_lock(workflow.paths):
            holder_entered.set()
            assert release_holder.wait(5)

    def generate(_output_path: Path):
        nonlocal callback_called
        callback_called = True
        pytest.fail("stale chapter context must fail before generation")

    def run_generation():
        generation_started.set()
        return workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        assert holder_entered.wait(2)
        generation = executor.submit(run_generation)
        assert generation_started.wait(2)
        if changed_file == "tree":
            changed = json.loads(json.dumps(tree))
            changed["warnings"].append("concurrent chapter update")
            workflow_module._atomic_json(workflow.paths.chapter_tree, changed)
        else:
            changed = json.loads(json.dumps(confirmation))
            changed["revision"] += 1
            workflow_module._atomic_json(workflow.paths.confirmation, changed)
        release_holder.set()
        holder.result(timeout=5)
        with pytest.raises(ValueError, match="chapter"):
            generation.result(timeout=5)

    assert callback_called is False


def test_confirm_chapter_waits_for_generation_and_updates_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    markdown = _valid_markdown(final, tree, confirmation)
    publisher = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    confirmer = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    generation_entered = threading.Event()
    release_generation = threading.Event()
    confirmation_started = threading.Event()
    confirmation_write_entered = threading.Event()
    original_build_confirmation = workflow_module.build_confirmation
    original_persist_confirmation = workflow_module.persist_chapter_confirmation

    def generate(output_path: Path):
        generation_entered.set()
        assert release_generation.wait(5)
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    def build_updated_confirmation(value: dict, chapter_id: str):
        updated = original_build_confirmation(value, chapter_id)
        updated["revision"] = 2
        return updated

    def persist_updated_confirmation(path: Path, value: dict):
        confirmation_write_entered.set()
        return original_persist_confirmation(path, value)

    monkeypatch.setattr(workflow_module, "build_confirmation", build_updated_confirmation)
    monkeypatch.setattr(
        workflow_module, "persist_chapter_confirmation", persist_updated_confirmation
    )

    def run_confirmation():
        confirmation_started.set()
        return confirmer.confirm_chapter(confirmation["chapter_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(
            publisher.generate_and_publish,
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
        try:
            assert generation_entered.wait(2)
            confirmation_future = executor.submit(run_confirmation)
            assert confirmation_started.wait(2)
            assert confirmation_write_entered.wait(0.2) is False
            release_generation.set()
            publication.result(timeout=5)
            current_confirmation = confirmation_future.result(timeout=5)
        finally:
            release_generation.set()

    assert current_confirmation["revision"] == 2
    manifest = json.loads(publisher.paths.publication.read_text(encoding="utf-8"))
    assert manifest["metadata"]["chapter_id"] == current_confirmation["chapter_id"]
    assert (
        manifest["metadata"]["chapter_fingerprint"] == current_confirmation["chapter_fingerprint"]
    )


def test_detect_chapters_waits_for_generation_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    markdown = _valid_markdown(final, tree, confirmation)
    publisher = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    detector = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    generation_entered = threading.Event()
    release_generation = threading.Event()
    detection_started = threading.Event()
    tree_write_entered = threading.Event()
    original_atomic_json = workflow_module._atomic_json

    def generate(output_path: Path):
        generation_entered.set()
        assert release_generation.wait(5)
        output_path.write_text(markdown, encoding="utf-8")
        return {"markdown": markdown, "metadata": metadata}

    def observe_tree_write(path: Path, value: dict):
        if path == detector.paths.chapter_tree:
            tree_write_entered.set()
        return original_atomic_json(path, value)

    monkeypatch.setattr(workflow_module, "_atomic_json", observe_tree_write)

    def run_detection():
        detection_started.set()
        return detector.detect_chapters()

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(
            publisher.generate_and_publish,
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
        try:
            assert generation_entered.wait(2)
            detection = executor.submit(run_detection)
            assert detection_started.wait(2)
            assert tree_write_entered.wait(0.2) is False
            release_generation.set()
            publication.result(timeout=5)
            detected = detection.result(timeout=5)
        finally:
            release_generation.set()

    assert detected == tree
    assert tree_write_entered.is_set()


def test_publication_lock_serializes_independent_processes(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    context = multiprocessing.get_context("fork")
    first_started = context.Event()
    first_entered = context.Event()
    release_first = context.Event()
    second_started = context.Event()
    second_entered = context.Event()
    release_second = context.Event()
    first = context.Process(
        target=_hold_publication_lock,
        args=(str(state_root), first_started, first_entered, release_first),
    )
    second = context.Process(
        target=_hold_publication_lock,
        args=(str(state_root), second_started, second_entered, release_second),
    )
    try:
        first.start()
        assert first_started.wait(2)
        assert first_entered.wait(2)
        second.start()
        assert second_started.wait(2)
        assert second_entered.wait(0.2) is False
        release_first.set()
        first.join(5)
        assert first.exitcode == 0
        assert second_entered.wait(2)
        release_second.set()
        second.join(5)
        assert second.exitcode == 0
    finally:
        release_first.set()
        release_second.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
            process.join(5)


def test_migration_bootstrap_serializes_cooperative_processes_at_unbound_allocation(
    tmp_path: Path,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    release_first = context.Event()
    second_started = context.Event()
    second_entered = context.Event()
    results = context.Queue()
    bootstrap_point = "migration-sidecar:anchor:allocation-opened"
    first = context.Process(
        target=_pause_completed_migration_process,
        args=(
            str(source_path),
            str(state_root),
            bootstrap_point,
            first_entered,
            release_first,
            results,
        ),
    )
    second = context.Process(
        target=_observe_completed_migration_process,
        args=(
            str(source_path),
            str(state_root),
            bootstrap_point,
            second_started,
            second_entered,
            results,
        ),
    )
    try:
        first.start()
        assert first_entered.wait(5)
        inventory = tuple(sorted(path.name for path in state_root.iterdir()))
        second.start()
        assert second_started.wait(5)
        assert second_entered.wait(0.3) is False
        assert tuple(sorted(path.name for path in state_root.iterdir())) == inventory
        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)
    finally:
        release_first.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    payloads = [results.get(timeout=2), results.get(timeout=2)]
    assert [payload["status"] for payload in payloads] == ["completed", "completed"]
    assert second_entered.is_set() is False


def test_migration_bootstrap_hardens_owned_state_root_before_sidecar_creation(
    tmp_path: Path,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    state_root.chmod(0o755)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert workflow.status()["status"] == "completed"
    assert stat.S_IMODE(state_root.lstat().st_mode) == 0o700


def test_migration_bootstrap_rejects_symlink_transaction_root(tmp_path: Path):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    real_root = tmp_path / "real-ocr-state"
    state_root.rename(real_root)
    state_root.symlink_to(real_root, target_is_directory=True)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("symlink root must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert json.loads((real_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert not (real_root / "ocr-v1-v2-migration.json").exists()


@pytest.mark.parametrize("mutation", ["batch", "page", "fingerprint"])
def test_status_payload_blocks_invalid_ocr_evidence(tmp_path: Path, mutation: str):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    if mutation == "batch":
        final["status"] = "running"
    elif mutation == "page":
        del final["pages"]["1"]["decision"]
    elif mutation == "fingerprint":
        final["input_fingerprint"] = "foreign-input"
    (state_root / "batch-final.json").write_text(json.dumps(final), encoding="utf-8")

    payload = status_payload(
        status=WorkflowStatus.COMPLETED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "blocked"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_evidence_invalid"


@pytest.mark.parametrize("mutation", ["markdown", "chapter", "model", "ruleset"])
def test_status_payload_keeps_valid_ocr_completed_when_publication_is_invalid(
    tmp_path: Path, mutation: str
):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path)
    publication_path = state_root / "note-publication.json"
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    if mutation == "markdown":
        note = _published_artifact_path(state_root)
        note.write_text(
            note.read_text(encoding="utf-8").replace("概念内容", "被篡改内容"),
            encoding="utf-8",
        )
    elif mutation == "chapter":
        publication["metadata"]["chapter_fingerprint"] = "foreign-chapter"
    elif mutation == "model":
        publication["metadata"]["model"] = "other-model"
    elif mutation == "ruleset":
        publication["metadata"]["prompt_rules_version"] = "other-ruleset"
    if mutation != "markdown":
        publication_path.write_text(json.dumps(publication), encoding="utf-8")

    payload = status_payload(
        status=WorkflowStatus.COMPLETED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_publication_invalid"


def test_status_rejects_coordinated_manifest_and_artifact_chapter_rebinding(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path)
    manifest_path = state_root / "note-publication.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = state_root / manifest["artifact_basename"]
    old_fingerprint = manifest["metadata"]["chapter_fingerprint"]
    markdown = artifact.read_text(encoding="utf-8").replace(
        f"> 章节指纹：`{old_fingerprint}`",
        "> 章节指纹：`foreign-chapter`",
    )
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    replacement = state_root / f"intensive-reading.{digest}.md"
    replacement.write_text(markdown, encoding="utf-8")
    manifest["artifact_basename"] = replacement.name
    manifest["artifact_sha256"] = digest
    manifest["metadata"]["chapter_fingerprint"] = "foreign-chapter"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    payload = status_payload(
        status=WorkflowStatus.COMPLETED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_publication_invalid"


def test_status_retries_when_publication_manifest_changes_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    first_markdown = _valid_markdown(final, tree, confirmation, concept="第一份状态快照")
    first_artifact = _write_content_addressed_publication(
        state_root, final, tree, confirmation, first_markdown
    )
    manifest_path = state_root / "note-publication.json"
    first_manifest = manifest_path.read_bytes()
    second_markdown = _valid_markdown(final, tree, confirmation, concept="第二份状态快照")
    second_artifact = _write_content_addressed_publication(
        state_root, final, tree, confirmation, second_markdown
    )
    second_manifest = manifest_path.read_bytes()
    manifest_path.write_bytes(first_manifest)
    original_read = workflow_module._read_regular_json
    manifest_reads = 0

    def switch_manifest_after_first_read(path: Path):
        nonlocal manifest_reads
        value = original_read(path)
        if path == manifest_path:
            manifest_reads += 1
            if manifest_reads == 1:
                manifest_path.write_bytes(second_manifest)
        return value

    monkeypatch.setattr(workflow_module, "_read_regular_json", switch_manifest_after_first_read)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert first_artifact != second_artifact
    assert manifest_reads >= 3
    assert payload["publishable"] is True
    assert payload["markdown_path"] == str(second_artifact)


def test_status_rejects_final_replaced_after_manifest_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    final_path = state_root / "batch-final.json"
    replacement = dict(final)
    replacement["generation"] = 2
    original_status = workflow_module._publication_manifest_status
    replaced = False

    def validate_then_replace(
        expected_final: dict, paths: workflow_module.WorkflowPaths, publication: dict
    ):
        nonlocal replaced
        result = original_status(expected_final, paths, publication)
        if result[0] and not replaced:
            workflow_module._atomic_json(final_path, replacement)
            replaced = True
        return result

    monkeypatch.setattr(workflow_module, "_publication_manifest_status", validate_then_replace)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert replaced is True
    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_publication_invalid"


def test_status_does_not_publish_when_manifest_never_stabilizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    first_markdown = _valid_markdown(final, tree, confirmation, concept="抖动状态甲")
    first_artifact = _write_content_addressed_publication(
        state_root, final, tree, confirmation, first_markdown
    )
    manifest_path = state_root / "note-publication.json"
    first_manifest = manifest_path.read_bytes()
    second_markdown = _valid_markdown(final, tree, confirmation, concept="抖动状态乙")
    second_artifact = _write_content_addressed_publication(
        state_root, final, tree, confirmation, second_markdown
    )
    second_manifest = manifest_path.read_bytes()
    manifest_path.write_bytes(first_manifest)
    original_read = workflow_module._read_regular_json
    manifest_reads = 0

    def keep_switching_manifest(path: Path):
        nonlocal manifest_reads
        value = original_read(path)
        if path == manifest_path:
            manifest_reads += 1
            replacement = (
                second_manifest
                if value["artifact_basename"] == first_artifact.name
                else first_manifest
            )
            manifest_path.write_bytes(replacement)
        return value

    monkeypatch.setattr(workflow_module, "_read_regular_json", keep_switching_manifest)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert first_artifact != second_artifact
    assert manifest_reads == workflow_module._PUBLICATION_STATUS_RETRIES * 2
    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_publication_invalid"


def test_status_payload_does_not_report_unpublished_result_as_completed(tmp_path: Path):
    payload = status_payload(
        status=WorkflowStatus.RUNNING,
        source_path=tmp_path / "book.pdf",
        state_root=tmp_path / "state",
    )

    assert payload["status"] == "running"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None


def test_completed_ocr_without_note_is_not_yet_publishable(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path, publish_note=False)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] is None


def test_content_addressed_manifest_is_publishable_after_process_restart(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    artifact = _write_content_addressed_publication(state_root, final, tree, confirmation, markdown)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert payload["error"] is None
    assert payload["markdown_path"] == str(artifact)


def test_legacy_final_metadata_and_canonical_note_remain_publishable(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
    final = _legacy_v1_final(final)
    final.update(
        {
            "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
            "model": "deepseek-v4-pro",
            "ruleset": "mba-intensive-reading-v1",
            "prompt_fingerprint": "prompt-fingerprint",
            "chapter_fingerprint": confirmation["chapter_fingerprint"],
            "note_input_fingerprint": final["input_fingerprint"],
            "note_evidence_fingerprint": tree["evidence_fingerprint"],
        }
    )
    (state_root / "batch-final.json").write_text(json.dumps(final), encoding="utf-8")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert payload["error"] is None
    assert payload["markdown_path"] is not None
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    backup = json.loads((state_root / "batch-final.v1.backup.json").read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2
    assert migrated["run_config"] == {
        "pages": [1],
        "dpi": 300,
        "languages": ["zh-Hans"],
        "sample_rate": 0,
    }
    assert backup == final


def test_legacy_status_rejects_final_replaced_after_publication_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
    final = _legacy_v1_final(final)
    final.update(
        {
            "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
            "model": "deepseek-v4-pro",
            "ruleset": "mba-intensive-reading-v1",
            "prompt_fingerprint": "prompt-fingerprint",
            "chapter_fingerprint": confirmation["chapter_fingerprint"],
            "note_input_fingerprint": final["input_fingerprint"],
            "note_evidence_fingerprint": tree["evidence_fingerprint"],
        }
    )
    final_path = state_root / "batch-final.json"
    workflow_module._atomic_json(final_path, final)
    replacement = dict(final)
    replacement["generation"] = 2
    original_migrate = workflow_module._migrate_legacy_completed_final
    replaced = False

    def validate_then_replace(expected_final: dict, *args, **kwargs):
        nonlocal replaced
        result = original_migrate(expected_final, *args, **kwargs)
        if not replaced:
            workflow_module._atomic_json(final_path, replacement)
            replaced = True
        return result

    monkeypatch.setattr(workflow_module, "_migrate_legacy_completed_final", validate_then_replace)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert replaced is True
    assert payload["status"] == "blocked"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_evidence_invalid"


def test_real_v1_running_state_preserves_proven_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
    monkeypatch.setattr(workflow_module, "count_pdf_pages", lambda _source: 1)

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )

    assert workflow._thread is not None
    workflow._thread.join(timeout=3)
    assert not workflow._thread.is_alive()
    assert workflow.status()["status"] == "completed"
    backup = json.loads((state_root / "batch-state.v1.backup.json").read_text())
    migrated = json.loads((state_root / "batch-state.json").read_text())
    assert backup == legacy
    assert migrated["schema_version"] == 2
    assert migrated["run_config"] == {
        "pages": [1],
        "dpi": 320,
        "languages": ["en-US"],
        "sample_rate": 0.05,
    }


def test_v1_running_state_without_provable_run_config_fails_closed(tmp_path: Path):
    engines = FakeEngines()
    seeded = _orchestrator(tmp_path, engines)
    state = seeded._load_or_create_state(engines.pdf_path, [1], 320, ["en-US"], 0.2, deadline=None)
    seeded._set_status(state, BatchStatus.RUNNING)
    legacy = _legacy_v1_state(state)
    state_root = tmp_path / "ocr-state"
    state_path = state_root / "batch-state.json"
    original = workflow_module._canonical_json_bytes(legacy)
    workflow_module._atomic_json(state_path, legacy)
    engines.calls.clear()

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )

    if workflow._thread is not None:
        workflow._thread.join(timeout=3)
    assert workflow._thread is None
    assert workflow.status()["status"] == "blocked"
    assert workflow.status()["error"] == "ocr_state_invalid"
    assert state_path.read_bytes() == original
    assert not (state_root / "batch-state.v1.backup.json").exists()
    assert not (state_root / "ocr-v1-v2-migration.json").exists()


@pytest.mark.parametrize(
    "crash_point",
    [
        "completed_journal_prepared",
        "completed_backup_published",
        "completed_artifact_published",
        "completed_source_cas_ready",
        "completed_source_swapped",
        "completed_final_published",
        "completed_manifest_linked",
        "completed_manifest_published",
        "completed_commit_ready",
        "completed_cleanup_ready",
    ],
)
def test_legacy_completed_migration_recovers_every_atomic_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def crash_at_boundary(point: str) -> None:
        if point == crash_point:
            raise OSError("simulated migration process crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(workflow_module, "_migration_checkpoint", crash_at_boundary)
        crashed = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
        assert crashed.status()["status"] == "blocked"

    manifest_path = state_root / "note-publication.json"
    if manifest_path.exists():
        partially_migrated = json.loads(
            (state_root / "batch-final.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert partially_migrated["schema_version"] == 2
        assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(
            partially_migrated
        )
    assert journal_path.exists()

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = recovered.status()
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = state_root / manifest["artifact_basename"]
    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert payload["markdown_path"] == str(artifact)
    assert migrated["schema_version"] == 2
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
    assert manifest["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert (
        json.loads((state_root / "batch-final.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not journal_path.exists()
    assert not tuple(state_root.glob(".ocr-v1-v2-*.anchor"))
    assert not tuple(state_root.glob(".ocr-v1-v2-*.manifest"))


def test_real_exit_after_source_swap_restores_and_preserves_competing_source(
    tmp_path: Path,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    competitor = copy.deepcopy(legacy)
    competitor["updated_at"] += 1
    competitor_bytes = workflow_module._canonical_json_bytes(competitor)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_source_swap_process,
        args=(str(source_path), str(state_root), "after-swap", competitor_bytes),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_SOURCE_SWAP
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    swap_path = state_root / journal["swap_path_basename"]
    assert swap_path.read_bytes() == competitor_bytes
    assert final_path.read_bytes() != competitor_bytes

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("competing final must not rerun"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == competitor_bytes
    assert journal_path.is_file()


@pytest.mark.parametrize("attacked_path", ["source", "swap"])
def test_post_swap_recovery_rejects_byte_identical_foreign_inode_stably(
    tmp_path: Path,
    attacked_path: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_source_swap_process,
        args=(str(source_path), str(state_root), "after-swap", None),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_AFTER_SOURCE_SWAP

    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    source = state_root / "batch-final.json"
    swap = state_root / journal["swap_path_basename"]
    attacked = source if attacked_path == "source" else swap
    _replace_regular_bytes(attacked, attacked.read_bytes())
    info = attacked.lstat()
    foreign_identity = (info.st_dev, info.st_ino)

    for _attempt in range(2):
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail(
                "byte-identical foreign source/swap inode must not recover"
            ),
        )
        payload = recovered.status()
        assert payload["status"] == "blocked"
        assert payload["error"] == "ocr_evidence_invalid"
        persisted = json.loads(journal_path.read_text(encoding="utf-8"))
        assert persisted["phase"] == "source_swap_prepared"
        assert persisted["swap_displaced_dev"] is None
        assert persisted["swap_displaced_ino"] is None
        assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))

    assert _directory_contains_identity(state_root, foreign_identity)


def test_real_exit_during_post_swap_fsync_recovers_recorded_displaced_source(
    tmp_path: Path,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_source_swap_process,
        args=(str(source_path), str(state_root), "post-swap-fsync", None),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_DURING_POST_SWAP_FSYNC
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    swap_path = state_root / journal["swap_path_basename"]
    assert swap_path.read_bytes() == workflow_module._canonical_json_bytes(legacy)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = recovered.status()
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert migrated["schema_version"] == 2
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
    assert not journal_path.exists()


def test_post_swap_fsync_error_retains_recorded_displaced_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    original_rename_swap = workflow_module._rename_swap
    original_fsync_directory = workflow_module._fsync_directory
    source_swap_happened = False
    failed = False

    def rename_swap(first: Path, second: Path) -> None:
        nonlocal source_swap_happened
        original_rename_swap(first, second)
        if Path(second) == final_path:
            source_swap_happened = True

    def fail_post_swap_fsync(path: Path) -> None:
        nonlocal failed
        if source_swap_happened and Path(path) == state_root and not failed:
            failed = True
            raise OSError("post-swap fsync failed")
        original_fsync_directory(path)

    with monkeypatch.context() as failure:
        failure.setattr(workflow_module, "_rename_swap", rename_swap)
        failure.setattr(workflow_module, "_fsync_directory", fail_post_swap_fsync)
        crashed = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
        assert crashed.status()["status"] == "blocked"

    assert failed is True
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    assert (state_root / journal["swap_path_basename"]).read_bytes() == (
        workflow_module._canonical_json_bytes(legacy)
    )

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    recovered_payload = recovered.status()
    assert recovered_payload["status"] == "completed", recovered_payload
    assert not journal_path.exists()


def test_real_exit_after_running_source_swap_recovers_rerun_intent(
    tmp_path: Path,
):
    engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_running_source_swap_process,
        args=(str(engines.pdf_path), str(state_root)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_RUNNING_SOURCE_SWAP
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["kind"] == "running"
    assert journal["phase"] == "source_swap_prepared"
    assert (state_root / journal["swap_path_basename"]).read_bytes() == (
        workflow_module._canonical_json_bytes(legacy)
    )

    recovered = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )
    assert recovered._thread is not None
    recovered._thread.join(timeout=3)
    assert not recovered._thread.is_alive()
    assert recovered.status()["status"] == "completed"
    assert not journal_path.exists()


def test_running_post_swap_recovery_rejects_byte_identical_foreign_displaced_inode(
    tmp_path: Path,
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    process = multiprocessing.get_context("fork").Process(
        target=_crash_running_source_swap_process,
        args=(str(engines.pdf_path), str(state_root)),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_AFTER_RUNNING_SOURCE_SWAP

    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    swap = state_root / journal["swap_path_basename"]
    _replace_regular_bytes(swap, swap.read_bytes())
    foreign_info = swap.lstat()
    foreign_identity = (foreign_info.st_dev, foreign_info.st_ino)

    for _attempt in range(2):
        recovered = OcrWorkflow(
            source_path=engines.pdf_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail(
                "foreign displaced running state must not recover"
            ),
        )
        assert recovered._thread is None
        payload = recovered.status()
        assert payload["status"] == "blocked"
        assert payload["error"] == "ocr_state_invalid"
        persisted = json.loads(journal_path.read_text(encoding="utf-8"))
        assert persisted["phase"] == "source_swap_prepared"
        assert persisted["swap_displaced_dev"] is None
        assert persisted["swap_displaced_ino"] is None
        assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))

    assert _directory_contains_identity(state_root, foreign_identity)


def test_real_exit_after_commit_ready_revalidates_bindings_before_cleanup(
    tmp_path: Path,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_at_commit_ready_process,
        args=(str(source_path), str(state_root)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_COMMIT_READY
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "commit_ready"
    final = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(final)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert recovered.status()["status"] == "completed"
    assert not journal_path.exists()


def test_real_exit_after_cleanup_validation_recovers_before_journal_commit(
    tmp_path: Path,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_during_cleanup_process,
        args=(str(source_path), str(state_root)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_DURING_CLEANUP
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "commit_ready"

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert recovered.status()["status"] == "completed"
    assert not journal_path.exists()


def test_real_exit_during_journal_retire_never_loses_replacement_inode(
    tmp_path: Path,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    competitor = b'{"kind":"completed","foreign":true}'
    identity_marker = state_root / "cleanup-race-identity.txt"
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_during_journal_retire_process,
        args=(str(source_path), str(state_root), competitor, str(identity_marker)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_DURING_JOURNAL_RETIRE
    device, inode = (int(value) for value in identity_marker.read_text().split(":"))
    competitor_identity = (device, inode)
    assert _directory_contains_identity(state_root, competitor_identity)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("ambiguous cleanup must not rerun"),
    )
    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert _directory_contains_identity(state_root, competitor_identity)


@pytest.mark.parametrize(
    ("journal_kind", "boundary", "expected_anchor_count", "expected_manifest_count"),
    [
        ("completed", "before-anchor", 0, 0),
        ("running", "before-anchor", 0, 0),
        ("completed", "after-anchor", 1, 0),
        ("running", "after-anchor", 1, 0),
        ("completed", "after-manifest", 1, 1),
        ("completed", "before-prepared-cas", 1, 1),
        ("running", "before-prepared-cas", 1, 0),
    ],
)
def test_pre_intent_crash_reuses_one_transaction_without_growing_sidecars(
    tmp_path: Path,
    journal_kind: str,
    boundary: str,
    expected_anchor_count: int,
    expected_manifest_count: int,
):
    if journal_kind == "completed":
        source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
        source_file = state_root / "batch-final.json"
        backup = state_root / "batch-final.v1.backup.json"
    else:
        engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
        source_path = engines.pdf_path
        source_file = state_root / "batch-state.json"
        backup = state_root / "batch-state.v1.backup.json"
    marker = tmp_path / f"{journal_kind}-{boundary}-transaction-ids.txt"
    context = multiprocessing.get_context("fork")
    inventories: list[tuple[int, int]] = []

    for _attempt in range(2):
        process = context.Process(
            target=_crash_during_pre_intent_window_process,
            args=(
                str(source_path),
                str(state_root),
                journal_kind,
                boundary,
                str(marker),
            ),
        )
        process.start()
        process.join(10)
        assert process.exitcode == _EXIT_DURING_PRE_INTENT_WINDOW
        inventories.append(
            (
                len(tuple(state_root.glob(".ocr-v1-v2-*.anchor"))),
                len(tuple(state_root.glob(".ocr-v1-v2-*.manifest"))),
            )
        )

    transaction_ids = marker.read_text(encoding="ascii").splitlines()
    assert len(transaction_ids) == 2
    assert transaction_ids[0] == transaction_ids[1]
    assert inventories == [
        (expected_anchor_count, expected_manifest_count),
        (expected_anchor_count, expected_manifest_count),
    ]
    journal = json.loads((state_root / "ocr-v1-v2-migration.json").read_text(encoding="utf-8"))
    assert journal["kind"] == journal_kind
    assert journal["phase"] == "intent"
    assert journal["transaction_id"] == transaction_ids[0]
    assert journal["source_sha256"] == hashlib.sha256(source_file.read_bytes()).hexdigest()
    assert journal["source_size"] == len(source_file.read_bytes())
    assert journal["transaction_anchor_basename"] == (f".ocr-v1-v2-{transaction_ids[0]}.anchor")
    assert journal["transaction_anchor_dev"] is None
    assert journal["transaction_anchor_ino"] is None
    if journal_kind == "completed":
        migrated, content, publication = workflow_module._completed_migration_material(
            legacy,
            source_path,
            workflow_module.workflow_paths(state_root),
        )
        assert publication is not None
        assert content is not None
        assert journal["target_sha256"] == workflow_module._json_fingerprint(migrated)
        assert journal["artifact_sha256"] == hashlib.sha256(content).hexdigest()
        assert journal["publication_sha256"] == workflow_module._json_fingerprint(publication)
        assert journal["manifest_candidate_basename"] == (
            f".ocr-v1-v2-{transaction_ids[0]}.manifest"
        )
        assert journal["manifest_candidate_dev"] is None
        assert journal["manifest_candidate_ino"] is None
    else:
        target = workflow_module._legacy_running_target(legacy, source_path)
        assert journal["target_sha256"] == workflow_module._json_fingerprint(target)
        assert journal["artifact_sha256"] is None
        assert journal["publication_sha256"] is None
        assert journal["manifest_candidate_basename"] is None
    assert source_file.read_bytes() == workflow_module._canonical_json_bytes(legacy)
    assert not backup.exists()


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
def test_repeated_real_exit_before_candidate_publication_reuses_one_owned_file(
    tmp_path: Path,
    label: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"{label}-candidate-transaction-ids.txt"
    context = multiprocessing.get_context("fork")
    inventories: list[tuple[str, ...]] = []
    candidate_counts: list[int] = []

    for _attempt in range(3):
        process = context.Process(
            target=_crash_during_candidate_publication_process,
            args=(str(source_path), str(state_root), label, str(marker)),
        )
        process.start()
        process.join(10)
        assert process.exitcode == _EXIT_DURING_CANDIDATE_PUBLICATION
        transaction_ids = marker.read_text(encoding="ascii").splitlines()
        transaction_id = transaction_ids[-1]
        inventories.append(tuple(sorted(path.name for path in state_root.iterdir())))
        candidate_counts.append(
            len(_unpublished_candidate_paths(state_root, transaction_id, label))
        )

    transaction_ids = marker.read_text(encoding="ascii").splitlines()
    assert len(transaction_ids) == 3
    assert len(set(transaction_ids)) == 1
    assert inventories[1:] == [inventories[0], inventories[0]]
    assert candidate_counts == [1, 1, 1]

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert recovered.status()["status"] == "completed"
    assert not _unpublished_candidate_paths(state_root, transaction_ids[0], label)


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
@pytest.mark.parametrize("attack", ["foreign-inode", "foreign-content"])
def test_restart_rejects_foreign_or_modified_owned_candidate(
    tmp_path: Path,
    label: str,
    attack: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"{label}-{attack}-candidate.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_candidate_publication_process,
        args=(str(source_path), str(state_root), label, str(marker)),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_DURING_CANDIDATE_PUBLICATION

    transaction_id = marker.read_text(encoding="ascii").strip()
    candidates = _unpublished_candidate_paths(state_root, transaction_id, label)
    assert len(candidates) == 1
    candidate = candidates[0]
    original_content = candidate.read_bytes()
    original_identity = (candidate.stat().st_dev, candidate.stat().st_ino)
    if attack == "foreign-inode":
        _replace_regular_bytes(candidate, original_content)
        assert (candidate.stat().st_dev, candidate.stat().st_ino) != original_identity
    else:
        candidate.write_bytes(b"foreign candidate content")
        assert (candidate.stat().st_dev, candidate.stat().st_ino) == original_identity
    attacked_content = candidate.read_bytes()
    attacked_identity = (candidate.stat().st_dev, candidate.stat().st_ino)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid migration must not rerun"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert candidate.read_bytes() == attacked_content
    assert (candidate.stat().st_dev, candidate.stat().st_ino) == attacked_identity


def test_m0_bootstrap_boundary_requires_exclusive_cooperative_guard(
    tmp_path: Path,
):
    root = tmp_path / "private-transaction-root"
    root.mkdir(mode=0o700)
    owner = workflow_module._MigrationCandidateOwner(
        transaction_id="a" * 64,
        label="anchor",
        transaction_token="b" * 64,
        root=root,
        kind="completed",
    )

    with pytest.raises(workflow_module._LegacyMigrationError):
        workflow_module._write_owned_migration_candidate(
            root,
            owner=owner,
            target_basename="owned-anchor",
            content=b"owned",
            error_code="ocr_evidence_invalid",
        )

    assert tuple(root.iterdir()) == ()


def _guarded_anchor_owner(root: Path, publication_lock) -> object:
    return workflow_module._MigrationCandidateOwner(
        transaction_id="a" * 64,
        label="anchor",
        transaction_token="b" * 64,
        root=root,
        kind="completed",
        publication_lock=publication_lock,
    )


def _write_guarded_anchor(root: Path, publication_lock):
    return workflow_module._write_owned_migration_candidate(
        root,
        owner=_guarded_anchor_owner(root, publication_lock),
        target_basename="owned-anchor",
        content=b"owned",
        error_code="ocr_evidence_invalid",
    )


def test_active_publication_guard_allows_production_sidecar_write(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")

    with workflow_module._publication_transaction_lock(paths) as guard:
        candidate, snapshot = _write_guarded_anchor(paths.root, guard)

        guard.validate()
        assert candidate.read_bytes() == b"owned"
        assert snapshot.content == b"owned"


def test_worker_claim_allows_bounded_publication_transaction(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    claim = workflow_module._try_claim_ocr_worker(paths)
    assert claim is not None
    try:
        with workflow_module._publication_transaction_lock(
            paths,
            deadline=time.monotonic() + 1,
        ) as guard:
            guard.validate()
    finally:
        claim.release()


def test_publication_transaction_times_out_without_entering_critical_section(
    tmp_path: Path,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    entered = threading.Event()
    release = threading.Event()

    def hold_lock():
        with workflow_module._publication_transaction_lock(paths):
            entered.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="timed out"):
            with workflow_module._publication_transaction_lock(
                paths,
                deadline=time.monotonic() + 0.1,
            ):
                pytest.fail("timed-out publication transaction entered")
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()


def test_copied_publication_guard_cannot_forge_active_lease(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")

    with workflow_module._publication_transaction_lock(paths) as guard:
        forged = copy.copy(guard)
        assert forged is not guard

        with pytest.raises(workflow_module._LegacyMigrationError):
            _write_guarded_anchor(paths.root, forged)


def test_same_field_object_cannot_forge_active_publication_guard(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")

    with workflow_module._publication_transaction_lock(paths) as guard:

        class ForgedGuard:
            root_path = guard.root_path
            path = guard.path
            device = guard.device
            inode = guard.inode
            token = guard.token
            pid = guard.pid
            thread_id = guard.thread_id

            @staticmethod
            def validate() -> None:
                return None

        with pytest.raises(workflow_module._LegacyMigrationError):
            _write_guarded_anchor(paths.root, ForgedGuard())


def test_expired_publication_guard_rejects_production_sidecar_write(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")

    with workflow_module._publication_transaction_lock(paths) as guard:
        guard.validate()

    with pytest.raises(workflow_module._LegacyMigrationError):
        _write_guarded_anchor(paths.root, guard)


def test_cross_thread_publication_guard_rejects_production_sidecar_write(tmp_path: Path):
    paths = workflow_module.workflow_paths(tmp_path / "state")

    with workflow_module._publication_transaction_lock(paths) as guard:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_write_guarded_anchor, paths.root, guard)
            with pytest.raises(workflow_module._LegacyMigrationError):
                future.result()


def _install_publication_cleanup_faults(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unlock_failures: frozenset[str] = frozenset(),
    close_failure: str | None = None,
) -> tuple[list[str], dict[int, str]]:
    original_flock = workflow_module.fcntl.flock
    original_close = workflow_module.os.close
    lock_fds: list[int] = []
    roles: dict[int, str] = {}
    events: list[str] = []
    injected: set[tuple[str, str]] = set()

    def descriptor_role(fd: int) -> str:
        if fd not in roles and len(lock_fds) == 2:
            roles[lock_fds[0]] = "root"
            roles[lock_fds[1]] = "publication"
        return roles.get(fd, "unrelated")

    def faulting_flock(fd: int, operation: int) -> None:
        if operation == workflow_module.fcntl.LOCK_EX:
            original_flock(fd, operation)
            lock_fds.append(fd)
            return
        if operation != workflow_module.fcntl.LOCK_UN:
            original_flock(fd, operation)
            return
        role = descriptor_role(fd)
        events.append(f"{role}-unlock")
        key = (role, "unlock")
        if role in unlock_failures and key not in injected:
            injected.add(key)
            raise OSError(f"simulated {role} unlock failure")
        original_flock(fd, operation)

    def faulting_close(fd: int) -> None:
        role = descriptor_role(fd)
        if role != "unrelated":
            events.append(f"{role}-close")
        key = (role, "close")
        if role == close_failure and key not in injected:
            injected.add(key)
            original_close(fd)
            raise OSError(f"simulated {role} close failure")
        original_close(fd)

    monkeypatch.setattr(workflow_module.fcntl, "flock", faulting_flock)
    monkeypatch.setattr(workflow_module.os, "close", faulting_close)
    return events, roles


def _assert_publication_cleanup_completed(
    paths,
    guard,
    events: list[str],
    descriptors: dict[str, int],
) -> None:
    assert events == [
        "publication-unlock",
        "publication-close",
        "root-unlock",
        "root-close",
    ]
    assert workflow_module._ACTIVE_PUBLICATION_LEASES == {}
    with pytest.raises(ValueError, match="not active"):
        guard.validate()
    assert set(descriptors) == {"root", "publication"}
    for fd in descriptors.values():
        with pytest.raises(OSError):
            os.fstat(fd)
    _assert_child_can_acquire_publication_lock(paths.root)


@pytest.mark.parametrize(
    ("unlock_failures", "primary_message", "additional_message"),
    [
        (frozenset({"publication"}), "publication unlock failure", None),
        (frozenset({"root"}), "root unlock failure", None),
        (
            frozenset({"publication", "root"}),
            "publication unlock failure",
            "root unlock failure",
        ),
    ],
)
def test_publication_lock_cleanup_attempts_every_resource_after_unlock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unlock_failures: frozenset[str],
    primary_message: str,
    additional_message: str | None,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    with monkeypatch.context() as faults:
        events, descriptor_roles = _install_publication_cleanup_faults(
            faults,
            unlock_failures=unlock_failures,
        )
        with pytest.raises(OSError, match=primary_message) as caught:
            with workflow_module._publication_transaction_lock(paths) as guard:
                guard.validate()

    descriptors = {role: fd for fd, role in descriptor_roles.items()}
    _assert_publication_cleanup_completed(paths, guard, events, descriptors)
    notes = getattr(caught.value, "__notes__", [])
    assert any("publication lock cleanup primary failure" in note for note in notes)
    if additional_message is not None:
        assert any(additional_message in note for note in notes)


@pytest.mark.parametrize("close_failure", ["publication", "root"])
def test_publication_lock_cleanup_attempts_every_resource_after_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_failure: str,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    with monkeypatch.context() as faults:
        events, descriptor_roles = _install_publication_cleanup_faults(
            faults,
            close_failure=close_failure,
        )
        with pytest.raises(OSError, match=f"{close_failure} close failure") as caught:
            with workflow_module._publication_transaction_lock(paths) as guard:
                guard.validate()

    descriptors = {role: fd for fd, role in descriptor_roles.items()}
    _assert_publication_cleanup_completed(paths, guard, events, descriptors)
    assert any(f"{close_failure} close" in note for note in getattr(caught.value, "__notes__", []))


def test_business_error_remains_primary_when_publication_lock_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    business_error = RuntimeError("publication body failed")
    with monkeypatch.context() as faults:
        events, descriptor_roles = _install_publication_cleanup_faults(
            faults,
            unlock_failures=frozenset({"publication"}),
            close_failure="root",
        )
        with pytest.raises(RuntimeError, match="publication body failed") as caught:
            with workflow_module._publication_transaction_lock(paths) as guard:
                guard.validate()
                raise business_error

    descriptors = {role: fd for fd, role in descriptor_roles.items()}
    _assert_publication_cleanup_completed(paths, guard, events, descriptors)
    assert caught.value is business_error
    notes = getattr(caught.value, "__notes__", [])
    assert any("publication unlock failure" in note for note in notes)
    assert any("root close failure" in note for note in notes)


@pytest.mark.parametrize("raise_inside", [False, True])
def test_publication_guard_hides_descriptors_and_is_revoked_before_os_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_inside: bool,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    original_flock = workflow_module.fcntl.flock
    guard_holder: dict[str, object] = {}
    validation_at_unlock: list[str] = []

    def observe_unlock(fd: int, operation: int) -> None:
        guard = guard_holder.get("guard")
        if operation == workflow_module.fcntl.LOCK_UN and guard is not None:
            try:
                guard.validate()
            except (OSError, ValueError):
                validation_at_unlock.append("revoked")
            else:
                validation_at_unlock.append("active")
        original_flock(fd, operation)

    monkeypatch.setattr(workflow_module.fcntl, "flock", observe_unlock)

    with pytest.raises(RuntimeError) if raise_inside else nullcontext():
        with workflow_module._publication_transaction_lock(paths) as guard:
            guard_holder["guard"] = guard
            assert not hasattr(guard, "fd")
            assert not hasattr(guard, "root_fd")
            guard.validate()
            if raise_inside:
                raise RuntimeError("lease body failed")

    assert validation_at_unlock
    assert set(validation_at_unlock) == {"revoked"}


@pytest.mark.parametrize("partial", [b"", b"a", b"a" * 12, b"a" * 63])
def test_publication_lock_recovers_canonical_partial_without_transaction_evidence(
    tmp_path: Path,
    partial: bytes,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    paths.root.mkdir(mode=0o700)
    paths.root.chmod(0o700)
    paths.publication_lock.write_bytes(partial)
    paths.publication_lock.chmod(0o600)
    original_identity = (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    )

    with workflow_module._publication_transaction_lock(paths) as guard:
        guard.validate()
        token = guard.token

    assert workflow_module._LOCK_TOKEN_RE.fullmatch(token)
    assert paths.publication_lock.read_bytes() == f"{token}\n".encode("ascii")
    assert stat.S_IMODE(paths.publication_lock.stat().st_mode) == 0o600
    assert (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    ) == original_identity


@pytest.mark.parametrize(
    "invalid",
    [
        b"g",
        b"A" * 12,
        b"a\n",
        b"a" * 64 + b"x",
        b"a" * 66,
    ],
)
def test_publication_lock_rejects_noncanonical_or_overlong_partial_without_overwrite(
    tmp_path: Path,
    invalid: bytes,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    paths.root.mkdir(mode=0o700)
    paths.root.chmod(0o700)
    paths.publication_lock.write_bytes(invalid)
    paths.publication_lock.chmod(0o600)
    original_identity = (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    )

    with pytest.raises(ValueError, match="publication lock token"):
        with workflow_module._publication_transaction_lock(paths):
            pytest.fail("invalid partial must not acquire the publication lock")

    assert paths.publication_lock.read_bytes() == invalid
    assert (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    ) == original_identity


@pytest.mark.parametrize("partial", [b"", b"a" * 12])
@pytest.mark.parametrize(
    "evidence_basename",
    [
        "ocr-v1-v2-migration.json",
        f".ocr-v1-v2-{'c' * 64}.sealed",
        f".ocr-v1-v2-{'c' * 64}.sidecar-anchor.json",
        f".ocr-v1-v2-{'c' * 64}.candidate-anchor-{'d' * 64}",
        "batch-final.v1.backup.json",
    ],
)
def test_publication_lock_refuses_partial_when_transaction_evidence_exists(
    tmp_path: Path,
    partial: bytes,
    evidence_basename: str,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    paths.root.mkdir(mode=0o700)
    paths.root.chmod(0o700)
    paths.publication_lock.write_bytes(partial)
    paths.publication_lock.chmod(0o600)
    evidence = paths.root / evidence_basename
    evidence.write_bytes(b"persistent transaction evidence")
    original_identity = (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    )

    with pytest.raises(ValueError):
        with workflow_module._publication_transaction_lock(paths):
            pytest.fail("transaction evidence must block token replacement")

    assert paths.publication_lock.read_bytes() == partial
    assert (
        paths.publication_lock.stat().st_dev,
        paths.publication_lock.stat().st_ino,
    ) == original_identity
    assert evidence.read_bytes() == b"persistent transaction evidence"


@pytest.mark.parametrize("prefix_length", [0, 1, 12, 63])
def test_publication_lock_token_real_exit_is_bounded_and_recovers_unattended(
    tmp_path: Path,
    prefix_length: int,
):
    paths = workflow_module.workflow_paths(tmp_path / "state")
    context = multiprocessing.get_context("fork")
    inventories: list[tuple[str, ...]] = []
    identities: list[tuple[int, int]] = []

    for _attempt in range(3):
        process = context.Process(
            target=_crash_during_publication_token_write,
            args=(str(paths.root), prefix_length),
        )
        process.start()
        process.join(10)
        assert process.exitcode == _EXIT_DURING_PUBLICATION_TOKEN_WRITE
        content = paths.publication_lock.read_bytes()
        assert len(content) == prefix_length
        assert all(byte in b"0123456789abcdef" for byte in content)
        inventories.append(tuple(sorted(path.name for path in paths.root.iterdir())))
        identities.append(
            (
                paths.publication_lock.stat().st_dev,
                paths.publication_lock.stat().st_ino,
            )
        )

    assert inventories == [(paths.publication_lock.name,)] * 3
    assert identities == [identities[0]] * 3

    with workflow_module._publication_transaction_lock(paths) as guard:
        guard.validate()

    assert workflow_module._LOCK_TOKEN_RE.fullmatch(guard.token)
    assert paths.publication_lock.read_bytes() == f"{guard.token}\n".encode("ascii")
    assert tuple(sorted(path.name for path in paths.root.iterdir())) == (
        paths.publication_lock.name,
    )


@pytest.mark.parametrize("boundary", ["opened", "partial-written", "fsynced"])
def test_sidecar_receipt_partial_creation_real_exit_is_bounded_and_recovers(
    tmp_path: Path,
    boundary: str,
):
    # M0 preserves unattended recovery from our own crash under the cooperative
    # lock; malicious same-UID replacement in the pre-identity micro-window is
    # intentionally outside that bootstrap boundary.
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"anchor-receipt-{boundary}.txt"
    context = multiprocessing.get_context("fork")
    inventories: list[tuple[str, ...]] = []

    for _attempt in range(3):
        process = context.Process(
            target=_crash_during_sidecar_receipt_process,
            args=(str(source_path), str(state_root), boundary, str(marker)),
        )
        process.start()
        process.join(10)
        assert process.exitcode == _EXIT_DURING_SIDECAR_RECEIPT
        inventories.append(tuple(sorted(path.name for path in state_root.iterdir())))

    transaction_ids = marker.read_text(encoding="ascii").splitlines()
    assert len(transaction_ids) == 3
    assert len(set(transaction_ids)) == 1
    assert inventories[1:] == [inventories[0], inventories[0]]

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert recovered.status()["status"] == "completed"


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
@pytest.mark.parametrize(
    ("boundary", "expected_stage"),
    [
        ("allocation-opened", "declared"),
        ("allocation-promoted", "allocated"),
        ("partial-written", "allocated"),
        ("candidate-fsynced", "allocated"),
        ("captured-verified", "ready"),
        ("published-unverified", "ready"),
    ],
)
def test_every_sidecar_subphase_real_exit_is_bounded_and_recovers_unattended(
    tmp_path: Path,
    label: str,
    boundary: str,
    expected_stage: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"{label}-{boundary}-durable-sidecar.txt"
    context = multiprocessing.get_context("fork")
    inventories: list[tuple[str, ...]] = []

    for _attempt in range(3):
        process = context.Process(
            target=_crash_during_durable_sidecar_process,
            args=(
                str(source_path),
                str(state_root),
                label,
                boundary,
                str(marker),
            ),
        )
        process.start()
        process.join(10)
        assert process.exitcode == _EXIT_DURING_DURABLE_SIDECAR
        inventories.append(tuple(sorted(path.name for path in state_root.iterdir())))

    transaction_ids = marker.read_text(encoding="ascii").splitlines()
    assert len(transaction_ids) == 3
    assert len(set(transaction_ids)) == 1
    assert inventories[1:] == [inventories[0], inventories[0]]
    transaction_id = transaction_ids[0]
    journal = json.loads((state_root / "ocr-v1-v2-migration.json").read_text(encoding="utf-8"))
    assert journal["sidecar_transaction_tokens"][label]
    receipt = _read_sidecar_receipt(state_root, transaction_id, label)
    assert receipt["stage"] == expected_stage
    assert receipt["transaction_id"] == transaction_id
    assert receipt["label"] == label

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    recovered_payload = recovered.status()
    assert recovered_payload["status"] == "completed", recovered_payload
    sealed = tuple(state_root.glob(f".ocr-v1-v2-{transaction_id}.sealed"))
    assert len(sealed) == 1
    finalized_receipt = _read_sidecar_receipt(state_root, transaction_id, label)
    assert finalized_receipt["stage"] == "published"
    assert (
        finalized_receipt["allocation_dev"],
        finalized_receipt["allocation_ino"],
    ) == (
        finalized_receipt["candidate_dev"],
        finalized_receipt["candidate_ino"],
    )
    assert (
        finalized_receipt["candidate_dev"],
        finalized_receipt["candidate_ino"],
    ) == (
        finalized_receipt["published_dev"],
        finalized_receipt["published_ino"],
    )
    assert not tuple(
        path
        for path in state_root.iterdir()
        if path.name.startswith(f".ocr-v1-v2-{transaction_id}.allocating-{label}")
        or path.name.startswith(f".ocr-v1-v2-{transaction_id}.candidate-{label}")
        or path.name.startswith(f".ocr-v1-v2-{transaction_id}.capture-{label}")
    )


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
@pytest.mark.parametrize(
    ("boundary", "receipt_path_field"),
    [
        ("captured-verified", "capture_basename"),
        ("published-unverified", "target_basename"),
    ],
)
def test_byte_identical_foreign_inode_after_verify_or_publish_never_completes(
    tmp_path: Path,
    label: str,
    boundary: str,
    receipt_path_field: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"{label}-{boundary}-foreign-inode.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_durable_sidecar_process,
        args=(
            str(source_path),
            str(state_root),
            label,
            boundary,
            str(marker),
        ),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_DURING_DURABLE_SIDECAR

    transaction_id = marker.read_text(encoding="ascii").strip()
    receipt = _read_sidecar_receipt(state_root, transaction_id, label)
    attacked = _receipt_object_path(state_root, receipt, receipt_path_field)
    original_content = attacked.read_bytes()
    original_identity = (attacked.stat().st_dev, attacked.stat().st_ino)
    _replace_regular_bytes(attacked, original_content)
    foreign_identity = (attacked.stat().st_dev, attacked.stat().st_ino)
    assert foreign_identity != original_identity

    for _attempt in range(2):
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail(
                "foreign published inode must not rerun"
            ),
        )
        payload = recovered.status()
        assert payload["status"] == "blocked"
        assert payload["error"] == "ocr_evidence_invalid"
        assert attacked.read_bytes() == original_content
        assert (attacked.stat().st_dev, attacked.stat().st_ino) == foreign_identity
        assert not tuple(state_root.glob(f".ocr-v1-v2-{transaction_id}.sealed"))


@pytest.mark.parametrize(
    "attack",
    ["symlink", "directory", "fifo", "mode", "owner", "multiple"],
)
def test_candidate_structural_or_ownership_attack_fails_closed_without_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
):
    if attack == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is unavailable on this platform")
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    marker = tmp_path / f"anchor-{attack}-candidate.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_durable_sidecar_process,
        args=(
            str(source_path),
            str(state_root),
            "anchor",
            "candidate-fsynced",
            str(marker),
        ),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_DURING_DURABLE_SIDECAR

    transaction_id = marker.read_text(encoding="ascii").strip()
    receipt = _read_sidecar_receipt(state_root, transaction_id, "anchor")
    candidate = _receipt_object_path(state_root, receipt, "candidate_basename")
    preserved = candidate.with_name(f"{candidate.name}.preserved")
    original_content = candidate.read_bytes()
    original_identity = (candidate.stat().st_dev, candidate.stat().st_ino)

    if attack in {"symlink", "directory", "fifo"}:
        candidate.rename(preserved)
        if attack == "symlink":
            candidate.symlink_to(preserved.name)
        elif attack == "directory":
            candidate.mkdir()
        else:
            os.mkfifo(candidate, 0o600)
    elif attack == "mode":
        candidate.chmod(0o644)
    elif attack == "owner":
        real_uid = os.geteuid()
        monkeypatch.setattr(
            workflow_module,
            "_migration_owner_uid",
            lambda: real_uid + 1,
            raising=False,
        )
    else:
        extra = candidate.with_name(f"{candidate.name}.foreign")
        extra.write_bytes(original_content)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid candidate must not rerun"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    if attack in {"symlink", "directory", "fifo"}:
        assert preserved.read_bytes() == original_content
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == original_identity
        assert candidate.lstat()
    elif attack == "mode":
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o644
        assert candidate.read_bytes() == original_content
    elif attack == "owner":
        assert candidate.read_bytes() == original_content
    else:
        assert candidate.read_bytes() == original_content
        assert candidate.with_name(f"{candidate.name}.foreign").read_bytes() == original_content
    assert not tuple(state_root.glob(f".ocr-v1-v2-{transaction_id}.sealed"))


def _final_sidecar_object_path(
    state_root: Path,
    transaction_id: str,
    label: str,
    receipt: dict[str, object],
) -> Path:
    moved = {
        "anchor": f".ocr-v1-v2-{transaction_id}.anchor-retired",
        "manifest": f".ocr-v1-v2-{transaction_id}.manifest-retired",
        "swap": "batch-final.json",
        "commit": f".ocr-v1-v2-{transaction_id}.sealed",
    }
    return state_root / moved.get(label, str(receipt["target_basename"]))


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
def test_finalized_migration_rejects_byte_identical_foreign_sidecar_receipt_inode(
    tmp_path: Path,
    label: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    completed = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    assert completed.status()["status"] == "completed"
    sealed = tuple(state_root.glob(".ocr-v1-v2-*.sealed"))
    assert len(sealed) == 1
    transaction_id = sealed[0].name.split(".")[1].removeprefix("ocr-v1-v2-")
    receipt_path = _sidecar_receipt_path(state_root, transaction_id, label)
    content = receipt_path.read_bytes()
    original_identity = (receipt_path.stat().st_dev, receipt_path.stat().st_ino)
    _replace_regular_bytes(receipt_path, content)
    foreign_identity = (receipt_path.stat().st_dev, receipt_path.stat().st_ino)
    assert foreign_identity != original_identity

    payload = completed.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert receipt_path.read_bytes() == content
    assert (receipt_path.stat().st_dev, receipt_path.stat().st_ino) == foreign_identity


@pytest.mark.parametrize(
    "label",
    ["anchor", "manifest", "backup", "artifact", "swap", "commit"],
)
def test_finalized_migration_rejects_byte_identical_foreign_published_inode(
    tmp_path: Path,
    label: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    completed = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    assert completed.status()["status"] == "completed"
    sealed = tuple(state_root.glob(".ocr-v1-v2-*.sealed"))
    assert len(sealed) == 1
    transaction_id = sealed[0].name.split(".")[1].removeprefix("ocr-v1-v2-")
    receipt = _read_sidecar_receipt(state_root, transaction_id, label)
    published_path = _final_sidecar_object_path(
        state_root,
        transaction_id,
        label,
        receipt,
    )
    content = published_path.read_bytes()
    original_identity = (published_path.stat().st_dev, published_path.stat().st_ino)
    _replace_regular_bytes(published_path, content)
    foreign_identity = (published_path.stat().st_dev, published_path.stat().st_ino)
    assert foreign_identity != original_identity

    payload = completed.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert published_path.read_bytes() == content
    assert (published_path.stat().st_dev, published_path.stat().st_ino) == foreign_identity


@pytest.mark.parametrize("journal_kind", ["completed", "running"])
@pytest.mark.parametrize("attack", ["foreign-inode", "malformed"])
def test_malformed_or_foreign_intent_fails_closed_without_source_loss(
    tmp_path: Path,
    journal_kind: str,
    attack: str,
):
    if journal_kind == "completed":
        source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
        source_file = state_root / "batch-final.json"
        backup = state_root / "batch-final.v1.backup.json"
    else:
        engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
        source_path = engines.pdf_path
        source_file = state_root / "batch-state.json"
        backup = state_root / "batch-state.v1.backup.json"
    marker = tmp_path / f"{journal_kind}-{attack}-intent.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_pre_intent_window_process,
        args=(
            str(source_path),
            str(state_root),
            journal_kind,
            "before-anchor",
            str(marker),
        ),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_DURING_PRE_INTENT_WINDOW

    journal_path = state_root / "ocr-v1-v2-migration.json"
    intent_bytes = journal_path.read_bytes()
    if attack == "foreign-inode":
        original_identity = (journal_path.stat().st_dev, journal_path.stat().st_ino)
        _replace_regular_bytes(journal_path, intent_bytes)
        assert (journal_path.stat().st_dev, journal_path.stat().st_ino) != original_identity
    else:
        malformed = json.loads(intent_bytes.decode("utf-8"))
        malformed["target_sha256"] = "0" * 64
        journal_path.write_bytes(workflow_module._canonical_json_bytes(malformed))
    attacked_bytes = journal_path.read_bytes()

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid intent must not resume"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == (
        "ocr_evidence_invalid" if journal_kind == "completed" else "ocr_state_invalid"
    )
    assert source_file.read_bytes() == workflow_module._canonical_json_bytes(legacy)
    assert not backup.exists()
    assert journal_path.read_bytes() == attacked_bytes
    assert not tuple(state_root.glob(".ocr-v1-v2-*.anchor"))
    assert not tuple(state_root.glob(".ocr-v1-v2-*.manifest"))


@pytest.mark.parametrize(
    ("journal_kind", "sidecar", "crash_boundary"),
    [
        ("completed", "anchor", "before-anchor"),
        ("running", "anchor", "before-anchor"),
        ("completed", "manifest", "after-anchor"),
    ],
)
def test_conflicting_intent_sidecar_fails_closed_without_cleanup_or_source_loss(
    tmp_path: Path,
    journal_kind: str,
    sidecar: str,
    crash_boundary: str,
):
    if journal_kind == "completed":
        source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
        source_file = state_root / "batch-final.json"
        backup = state_root / "batch-final.v1.backup.json"
    else:
        engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
        source_path = engines.pdf_path
        source_file = state_root / "batch-state.json"
        backup = state_root / "batch-state.v1.backup.json"
    marker = tmp_path / f"{journal_kind}-{sidecar}-conflict.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_pre_intent_window_process,
        args=(
            str(source_path),
            str(state_root),
            journal_kind,
            crash_boundary,
            str(marker),
        ),
    )
    process.start()
    process.join(10)
    assert process.exitcode == _EXIT_DURING_PRE_INTENT_WINDOW

    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    sidecar_path = state_root / str(
        journal[
            "transaction_anchor_basename" if sidecar == "anchor" else "manifest_candidate_basename"
        ]
    )
    conflict = f"foreign-{journal_kind}-{sidecar}".encode("ascii")
    sidecar_path.write_bytes(conflict)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("conflicting sidecar must not resume"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == (
        "ocr_evidence_invalid" if journal_kind == "completed" else "ocr_state_invalid"
    )
    assert source_file.read_bytes() == workflow_module._canonical_json_bytes(legacy)
    assert not backup.exists()
    assert json.loads(journal_path.read_text(encoding="utf-8"))["phase"] == "intent"
    assert sidecar_path.read_bytes() == conflict


@pytest.mark.parametrize(
    "boundary",
    [
        "journal-publish-fsync",
        "journal-phase-cas",
        "backup-publish-fsync",
        "artifact-publish-fsync",
        "manifest-link-fsync",
        "witness-create-fsync",
        "journal-commit",
        "witness-done",
        "commit-sealed",
    ],
)
def test_real_exit_after_published_migration_boundary_recovers_idempotently(
    tmp_path: Path,
    boundary: str,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_after_real_boundary_process,
        args=(str(source_path), str(state_root), boundary),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_REAL_BOUNDARY
    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = recovered.status()
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    artifact = state_root / manifest["artifact_basename"]
    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert migrated["schema_version"] == 2
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
    assert manifest["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert (
        json.loads((state_root / "batch-final.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not (state_root / "ocr-v1-v2-migration.json").exists()


@pytest.mark.parametrize(
    ("journal_kind", "target_phase"),
    [
        *(
            ("completed", phase)
            for phase in workflow_module._LEGACY_MIGRATION_PHASES["completed"][1:]
        ),
        *(("running", phase) for phase in workflow_module._LEGACY_MIGRATION_PHASES["running"][1:]),
    ],
)
def test_real_exit_after_every_journal_phase_cas_recovers_bound_transaction(
    tmp_path: Path,
    journal_kind: str,
    target_phase: str,
):
    if journal_kind == "completed":
        source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
        engines = None
    else:
        engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
        source_path = engines.pdf_path
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_after_journal_phase_cas_process,
        args=(str(source_path), str(state_root), journal_kind, target_phase),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_REAL_BOUNDARY
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["kind"] == journal_kind
    assert journal["phase"] == target_phase
    predecessor_identity = (
        journal["journal_previous_dev"],
        journal["journal_previous_ino"],
    )
    assert _directory_contains_identity(state_root, predecessor_identity)

    if journal_kind == "completed":
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    else:
        assert engines is not None
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda cancel: _orchestrator(
                tmp_path, engines, is_cancelled=cancel
            ),
        )
        assert recovered._thread is not None
        recovered._thread.join(timeout=3)
        assert not recovered._thread.is_alive()

    payload = recovered.status()
    assert payload["status"] == "completed"
    backup_name = (
        "batch-final.v1.backup.json"
        if journal_kind == "completed"
        else "batch-state.v1.backup.json"
    )
    assert json.loads((state_root / backup_name).read_text(encoding="utf-8")) == legacy
    if journal_kind == "completed":
        migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
        manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
        artifact = state_root / manifest["artifact_basename"]
        assert payload["publishable"] is True
        assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
        assert manifest["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    else:
        persisted = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 2
        assert persisted["run_config"] == {
            "pages": [1],
            "dpi": 320,
            "languages": ["en-US"],
            "sample_rate": 0.05,
        }
    assert not journal_path.exists()


@pytest.mark.parametrize(
    "boundary",
    [
        "journal-publish",
        "backup-publish",
        "artifact-publish",
        "manifest-link",
        "witness-publish",
    ],
)
def test_real_exit_after_publication_before_directory_fsync_recovers_bindings(
    tmp_path: Path,
    boundary: str,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_after_publication_before_fsync_process,
        args=(str(source_path), str(state_root), boundary),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_REAL_BOUNDARY
    published_paths = {
        "journal-publish": (state_root / "ocr-v1-v2-migration.json",),
        "backup-publish": (state_root / "batch-final.v1.backup.json",),
        "artifact-publish": tuple(state_root.glob("intensive-reading.*.md")),
        "manifest-link": (state_root / "note-publication.json",),
        "witness-publish": tuple(state_root.glob(".ocr-v1-v2-*.commit")),
    }[boundary]
    assert published_paths
    assert all(path.is_file() for path in published_paths)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = recovered.status()
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    artifact = state_root / manifest["artifact_basename"]
    assert payload["status"] == "completed"
    assert payload["publishable"] is True
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
    assert manifest["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert (
        json.loads((state_root / "batch-final.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not (state_root / "ocr-v1-v2-migration.json").exists()


def test_real_exit_after_journal_identity_publication_fails_closed_without_rewrites(
    tmp_path: Path,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_after_journal_identity_publication_process,
        args=(str(source_path), str(state_root)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_REAL_BOUNDARY
    assert not (state_root / "ocr-v1-v2-migration.json").exists()
    assert len(tuple(state_root.glob(".ocr-v1-v2-*.journal-stage-intent"))) == 1
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("orphan identity must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()
    assert not (state_root / "note-publication.json").exists()


def test_unsynced_journal_identity_candidate_fails_closed_without_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_fsync_directory = workflow_module._fsync_directory
    failed = False

    def fail_identity_directory_sync(path: Path) -> None:
        nonlocal failed
        identity_candidates = tuple(state_root.glob(".ocr-v1-v2-*.journal-stage-intent"))
        if (
            Path(path) == state_root
            and identity_candidates
            and not journal_path.exists()
            and not failed
        ):
            failed = True
            raise OSError("journal identity directory fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(workflow_module, "_fsync_directory", fail_identity_directory_sync)
    interrupted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert failed is True
    assert interrupted.status()["status"] == "blocked"
    assert not journal_path.exists()
    assert len(tuple(state_root.glob(".ocr-v1-v2-*.journal-stage-intent"))) == 1
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()

    monkeypatch.setattr(workflow_module, "_fsync_directory", original_fsync_directory)
    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsynced identity must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()


def test_process_exit_during_journal_retirement_preserves_self_identity(
    tmp_path: Path,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_completed_after_real_boundary_process,
        args=(str(source_path), str(state_root), "journal-commit"),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_AFTER_REAL_BOUNDARY
    retired_paths = tuple(state_root.glob(".ocr-v1-v2-*.journal-retired"))
    assert len(retired_paths) == 1
    retired_path = retired_paths[0]
    retired = json.loads(retired_path.read_text(encoding="utf-8"))
    info = retired_path.lstat()
    assert (retired["journal_self_dev"], retired["journal_self_ino"]) == (
        info.st_dev,
        info.st_ino,
    )
    hash_payload = dict(retired)
    expected_hash = hash_payload.pop("journal_self_sha256")
    hash_payload["journal_self_sha256"] = None
    assert (
        expected_hash
        == hashlib.sha256(workflow_module._canonical_json_bytes(hash_payload)).hexdigest()
    )

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert recovered.status()["status"] == "completed"
    assert json.loads((state_root / "batch-final.v1.backup.json").read_text()) == legacy


@pytest.mark.parametrize("journal_kind", ["completed", "running"])
@pytest.mark.parametrize(
    "transition",
    ["journal-retirement", "commit-to-done", "done-to-sealed"],
)
def test_commit_transition_fsync_failure_rolls_back_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal_kind: str,
    transition: str,
):
    if journal_kind == "completed":
        source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
        engines = None
    else:
        engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
        source_path = engines.pdf_path
    original_publish = workflow_module._rename_no_replace
    original_fsync_directory = workflow_module._fsync_directory
    transition_published = False
    fsync_failed = False

    def observe_transition(source: Path, target: Path) -> None:
        nonlocal transition_published
        original_publish(source, target)
        target_name = Path(target).name
        matches = {
            "journal-retirement": target_name.endswith(".journal-retired"),
            "commit-to-done": target_name.endswith(".done"),
            "done-to-sealed": target_name.endswith(".sealed"),
        }
        if matches[transition]:
            transition_published = True

    def fail_transition_fsync(path: Path) -> None:
        nonlocal fsync_failed
        if transition_published and Path(path) == state_root and not fsync_failed:
            fsync_failed = True
            raise OSError(f"{transition} directory fsync failed")
        original_fsync_directory(path)

    with monkeypatch.context() as failure:
        failure.setattr(workflow_module, "_rename_no_replace", observe_transition)
        failure.setattr(workflow_module, "_fsync_directory", fail_transition_fsync)
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail(
                "worker must not start before migration commit"
            ),
        )

    assert transition_published is True
    assert fsync_failed is True
    assert interrupted._thread is None
    assert interrupted.status()["status"] == "blocked"
    assert (state_root / "ocr-v1-v2-migration.json").is_file()
    assert not tuple(state_root.glob(".ocr-v1-v2-*.sealed"))

    if journal_kind == "completed":
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    else:
        assert engines is not None
        recovered = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda cancel: _orchestrator(
                tmp_path, engines, is_cancelled=cancel
            ),
        )
        assert recovered._thread is not None
        recovered._thread.join(timeout=3)
        assert not recovered._thread.is_alive()

    payload = recovered.status()
    backup_name = (
        "batch-final.v1.backup.json"
        if journal_kind == "completed"
        else "batch-state.v1.backup.json"
    )
    assert payload["status"] == "completed"
    assert json.loads((state_root / backup_name).read_text(encoding="utf-8")) == legacy
    if journal_kind == "completed":
        migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
        manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
        artifact = state_root / manifest["artifact_basename"]
        assert payload["publishable"] is True
        assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
        assert manifest["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    else:
        persisted = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
        assert persisted["schema_version"] == 2
        assert persisted["run_config"] == {
            "pages": [1],
            "dpi": 320,
            "languages": ["en-US"],
            "sample_rate": 0.05,
        }
    assert not (state_root / "ocr-v1-v2-migration.json").exists()


def test_completed_commit_cleanup_revalidates_backup_and_withdraws_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    manifest_path = state_root / "note-publication.json"
    competitor = b"cleanup-window-backup"
    tampered = False

    def tamper_at_cleanup(point: str) -> None:
        nonlocal tampered
        if point == "completed_cleanup_ready" and not tampered:
            _replace_regular_bytes(backup_path, competitor)
            tampered = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_at_cleanup)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert tampered is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert backup_path.read_bytes() == competitor
    assert journal_path.is_file()
    assert not manifest_path.exists()


def test_running_commit_cleanup_revalidates_backup_before_worker_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    backup_path = state_root / "batch-state.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    competitor = b"running-cleanup-window-backup"
    tampered = False

    def tamper_at_cleanup(point: str) -> None:
        nonlocal tampered
        if point == "running_cleanup_ready" and not tampered:
            _replace_regular_bytes(backup_path, competitor)
            tampered = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_at_cleanup)
    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("worker must not start"),
    )

    payload = workflow.status()
    assert tampered is True
    assert workflow._thread is None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert backup_path.read_bytes() == competitor
    assert journal_path.is_file()


def test_completed_commit_revalidates_backup_after_last_precommit_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    manifest_path = state_root / "note-publication.json"
    competitor = b"completed-after-last-precommit-check"
    original_retire = workflow_module._retire_bound_path
    tampered = False

    def tamper_before_journal_commit(active, retained, expected, **kwargs):
        nonlocal tampered
        if Path(active) == journal_path and not tampered:
            _replace_regular_bytes(backup_path, competitor)
            tampered = True
        return original_retire(active, retained, expected, **kwargs)

    monkeypatch.setattr(workflow_module, "_retire_bound_path", tamper_before_journal_commit)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert tampered is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert backup_path.read_bytes() == competitor
    assert journal_path.is_file()
    assert not manifest_path.exists()


def test_running_commit_revalidates_backup_after_last_precommit_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    backup_path = state_root / "batch-state.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    competitor = b"running-after-last-precommit-check"
    original_retire = workflow_module._retire_bound_path
    worker_started = threading.Event()
    tampered = False

    def tamper_before_journal_commit(active, retained, expected, **kwargs):
        nonlocal tampered
        if Path(active) == journal_path and not tampered:
            _replace_regular_bytes(backup_path, competitor)
            tampered = True
        return original_retire(active, retained, expected, **kwargs)

    def unexpected_factory(_cancel):
        worker_started.set()
        raise AssertionError("worker must not start after a failed migration commit")

    monkeypatch.setattr(workflow_module, "_retire_bound_path", tamper_before_journal_commit)
    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=unexpected_factory,
    )
    if workflow._thread is not None:
        workflow._thread.join(timeout=3)

    payload = workflow.status()
    assert tampered is True
    assert worker_started.is_set() is False
    assert workflow._thread is None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert backup_path.read_bytes() == competitor
    assert journal_path.is_file()


def test_journal_no_replace_publish_then_fsync_error_keeps_recoverable_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_publish = workflow_module._rename_no_replace
    original_fsync_directory = workflow_module._fsync_directory
    journal_published = False
    failed = False

    def observe_journal_publish(source, target):
        nonlocal journal_published
        result = original_publish(source, target)
        if Path(target) == journal_path:
            journal_published = True
        return result

    def fail_after_journal_link(path: Path) -> None:
        nonlocal failed
        if journal_published and Path(path) == state_root and not failed:
            failed = True
            raise OSError("journal directory fsync failed after no-replace publish")
        original_fsync_directory(path)

    monkeypatch.setattr(workflow_module, "_rename_no_replace", observe_journal_publish)
    monkeypatch.setattr(workflow_module, "_fsync_directory", fail_after_journal_link)
    interrupted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    assert journal_published is True
    assert failed is True
    assert interrupted.status()["status"] == "blocked"
    assert journal_path.is_file()
    assert not tuple(state_root.glob(".ocr-v1-v2-*.anchor"))

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    assert recovered.status()["status"] == "completed"
    assert recovered.status()["publishable"] is True
    assert not journal_path.exists()


def test_uncertain_journal_publication_rejects_identical_foreign_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_publish = workflow_module._rename_no_replace
    original_fsync_directory = workflow_module._fsync_directory
    original_require_bound_snapshot = workflow_module._require_bound_snapshot
    journal_published = False
    fsync_failed = False
    replacement_installed = False
    candidate_identity: tuple[int, int] | None = None
    foreign_identity: tuple[int, int] | None = None
    journal_content: bytes | None = None

    def observe_journal_publish(source, target):
        nonlocal journal_published
        result = original_publish(source, target)
        if Path(target) == journal_path:
            journal_published = True
        return result

    def fail_after_journal_publish(path: Path) -> None:
        nonlocal fsync_failed
        if journal_published and Path(path) == state_root and not fsync_failed:
            fsync_failed = True
            raise OSError("journal directory fsync failed after publication")
        original_fsync_directory(path)

    def replace_after_candidate_validation(
        path,
        expected,
        expected_identity,
        **kwargs,
    ):
        nonlocal candidate_identity, foreign_identity, journal_content, replacement_installed
        snapshot = original_require_bound_snapshot(
            path,
            expected,
            expected_identity,
            **kwargs,
        )
        if (
            Path(path) == journal_path
            and expected_identity is not None
            and fsync_failed
            and not replacement_installed
        ):
            candidate_identity = snapshot.identity
            journal_content = snapshot.content
            _replace_regular_bytes(journal_path, snapshot.content)
            info = journal_path.lstat()
            foreign_identity = (info.st_dev, info.st_ino)
            replacement_installed = True
        return snapshot

    with monkeypatch.context() as failure:
        failure.setattr(workflow_module, "_rename_no_replace", observe_journal_publish)
        failure.setattr(workflow_module, "_fsync_directory", fail_after_journal_publish)
        failure.setattr(
            workflow_module,
            "_require_bound_snapshot",
            replace_after_candidate_validation,
        )
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )

    assert interrupted.status()["status"] == "blocked"
    assert journal_published is True
    assert fsync_failed is True
    assert replacement_installed is True
    assert candidate_identity is not None
    assert foreign_identity is not None
    assert foreign_identity != candidate_identity
    assert journal_content is not None
    assert journal_path.read_bytes() == journal_content
    assert (journal_path.stat().st_dev, journal_path.stat().st_ino) == foreign_identity

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("foreign journal must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()


def test_missing_journal_self_identity_blocks_restart_without_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def stop_after_journal(point: str) -> None:
        if point == "completed_journal_prepared":
            raise OSError("stop after journal publication")

    with monkeypatch.context() as interruption:
        interruption.setattr(workflow_module, "_migration_checkpoint", stop_after_journal)
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    assert interrupted.status()["status"] == "blocked"

    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.pop("journal_self_sha256", None)
    journal_path.write_bytes(workflow_module._canonical_json_bytes(journal))

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("missing identity must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()
    assert not (state_root / "note-publication.json").exists()


def test_foreign_journal_self_identity_blocks_restart_without_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def stop_after_journal(point: str) -> None:
        if point == "completed_journal_prepared":
            raise OSError("stop after journal publication")

    with monkeypatch.context() as interruption:
        interruption.setattr(workflow_module, "_migration_checkpoint", stop_after_journal)
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    assert interrupted.status()["status"] == "blocked"

    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    info = journal_path.lstat()
    journal["journal_self_dev"] = info.st_dev
    journal["journal_self_ino"] = info.st_ino + 1
    journal["journal_self_sha256"] = None
    journal["journal_self_sha256"] = hashlib.sha256(
        workflow_module._canonical_json_bytes(journal)
    ).hexdigest()
    journal_path.write_bytes(workflow_module._canonical_json_bytes(journal))

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("foreign identity must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()
    assert not (state_root / "note-publication.json").exists()


@pytest.mark.parametrize("corruption", ["malformed", "stale"])
def test_invalid_journal_self_identity_blocks_restart_without_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def stop_after_journal(point: str) -> None:
        if point == "completed_journal_prepared":
            raise OSError("stop after journal publication")

    with monkeypatch.context() as interruption:
        interruption.setattr(workflow_module, "_migration_checkpoint", stop_after_journal)
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    assert interrupted.status()["status"] == "blocked"

    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if corruption == "malformed":
        journal["journal_self_dev"] = "not-an-integer"
    else:
        journal["journal_self_sha256"] = "0" * 64
    journal_path.write_bytes(workflow_module._canonical_json_bytes(journal))

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid identity must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()
    assert not (state_root / "note-publication.json").exists()


def test_phase_cas_journal_replacement_blocks_without_rewriting_current_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def stop_after_final_publish(point: str) -> None:
        if point == "completed_final_published":
            raise OSError("stop after phase CAS")

    with monkeypatch.context() as interruption:
        interruption.setattr(workflow_module, "_migration_checkpoint", stop_after_final_publish)
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
    assert interrupted.status()["status"] == "blocked"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "final_published"
    source_before = final_path.read_bytes()
    backup_before = backup_path.read_bytes()
    original_identity = (journal_path.stat().st_dev, journal_path.stat().st_ino)
    _replace_regular_bytes(journal_path, journal_path.read_bytes())
    assert (journal_path.stat().st_dev, journal_path.stat().st_ino) != original_identity

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("foreign phase journal must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == source_before
    assert backup_path.read_bytes() == backup_before
    assert not (state_root / "note-publication.json").exists()


def test_restart_rejects_identical_foreign_journal_replaced_after_final_candidate_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_publish = workflow_module._rename_no_replace
    original_fsync_directory = workflow_module._fsync_directory
    original_require_bound_snapshot = workflow_module._require_bound_snapshot
    journal_published = False
    fsync_failed = False
    candidate_checks = 0
    candidate_identity: tuple[int, int] | None = None
    foreign_identity: tuple[int, int] | None = None

    def observe_journal_publish(source, target):
        nonlocal journal_published
        result = original_publish(source, target)
        if Path(target) == journal_path:
            journal_published = True
        return result

    def fail_after_journal_publish(path: Path) -> None:
        nonlocal fsync_failed
        if journal_published and Path(path) == state_root and not fsync_failed:
            fsync_failed = True
            raise OSError("journal directory fsync failed after publication")
        original_fsync_directory(path)

    def replace_after_final_candidate_check(
        path,
        expected,
        expected_identity,
        **kwargs,
    ):
        nonlocal candidate_checks, candidate_identity, foreign_identity
        snapshot = original_require_bound_snapshot(
            path,
            expected,
            expected_identity,
            **kwargs,
        )
        if Path(path) == journal_path and expected_identity is not None and fsync_failed:
            candidate_checks += 1
            if candidate_checks == 2:
                candidate_identity = snapshot.identity
                _replace_regular_bytes(journal_path, snapshot.content)
                info = journal_path.lstat()
                foreign_identity = (info.st_dev, info.st_ino)
        return snapshot

    with monkeypatch.context() as failure:
        failure.setattr(workflow_module, "_rename_no_replace", observe_journal_publish)
        failure.setattr(workflow_module, "_fsync_directory", fail_after_journal_publish)
        failure.setattr(
            workflow_module,
            "_require_bound_snapshot",
            replace_after_final_candidate_check,
        )
        interrupted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )

    assert interrupted.status()["status"] == "blocked"
    assert candidate_checks == 2
    assert candidate_identity is not None
    assert foreign_identity is not None
    assert foreign_identity != candidate_identity
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("foreign journal must not recover"),
    )

    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not backup_path.exists()
    assert not (state_root / "note-publication.json").exists()


@pytest.mark.parametrize("publisher", ["no-replace", "artifact", "backup"])
def test_transaction_temp_replacement_is_preserved_and_blocks_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher: str,
):
    original_publish = workflow_module._rename_no_replace
    competitor = f"foreign-{publisher}".encode()
    competitor_identity: tuple[int, int] | None = None
    injected = False

    if publisher == "no-replace":
        target = tmp_path / "transaction.json"
    elif publisher == "artifact":
        digest = hashlib.sha256(b"artifact").hexdigest()
        target = tmp_path / f"intensive-reading.{digest}.md"
    else:
        target = tmp_path / "legacy.backup.json"

    def replace_temp_after_publish(source, linked_target):
        nonlocal competitor_identity, injected
        result = original_publish(source, linked_target)
        if Path(linked_target) == target and not injected:
            _replace_regular_bytes(Path(source), competitor)
            info = Path(source).lstat()
            competitor_identity = (info.st_dev, info.st_ino)
            injected = True
        return result

    monkeypatch.setattr(workflow_module, "_rename_no_replace", replace_temp_after_publish)
    with pytest.raises(ValueError):
        if publisher == "no-replace":
            workflow_module._write_regular_no_replace(
                target,
                b"journal",
                error_code="ocr_state_invalid",
            )
        elif publisher == "artifact":
            workflow_module._publish_immutable_artifact(tmp_path, b"artifact", digest)
        else:
            workflow_module._write_legacy_backup(
                target,
                b"backup",
                error_code="ocr_evidence_invalid",
            )

    assert injected is True
    assert competitor_identity is not None
    assert _directory_contains_identity(tmp_path, competitor_identity)


def test_temporary_write_cleanup_preserves_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    original_read_snapshot = workflow_module._read_regular_snapshot
    competitor_identity: tuple[int, int] | None = None

    def replace_before_snapshot(path: Path, *args, **kwargs):
        nonlocal competitor_identity
        if Path(path).name.startswith(".owned-temp-") and competitor_identity is None:
            _replace_regular_bytes(Path(path), b"foreign-temporary")
            info = Path(path).lstat()
            competitor_identity = (info.st_dev, info.st_ino)
            raise ValueError("temporary path replaced")
        return original_read_snapshot(path, *args, **kwargs)

    monkeypatch.setattr(workflow_module, "_read_regular_snapshot", replace_before_snapshot)
    with pytest.raises(ValueError):
        workflow_module._write_temporary_regular(
            tmp_path,
            prefix=".owned-temp-",
            content=b"owned",
        )

    assert competitor_identity is not None
    assert _directory_contains_identity(tmp_path, competitor_identity)


def test_manifest_rollback_preserves_replacement_after_candidate_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path = tmp_path / "note-publication.json"
    candidate = {"schema_version": 1, "candidate": True}
    workflow_module._atomic_json(manifest_path, candidate)
    original_read_json = workflow_module._read_regular_json
    competitor = b'{"foreign":true}'
    competitor_identity: tuple[int, int] | None = None

    def replace_after_candidate_read(path: Path):
        nonlocal competitor_identity
        value = original_read_json(path)
        if Path(path) == manifest_path and competitor_identity is None:
            _replace_regular_bytes(manifest_path, competitor)
            info = manifest_path.lstat()
            competitor_identity = (info.st_dev, info.st_ino)
        return value

    monkeypatch.setattr(workflow_module, "_read_regular_json", replace_after_candidate_read)
    with pytest.raises(ValueError):
        workflow_module._restore_previous_manifest(
            manifest_path,
            candidate=candidate,
            previous=None,
        )

    assert competitor_identity is not None
    assert manifest_path.read_bytes() == competitor
    assert _directory_contains_identity(tmp_path, competitor_identity)


def test_completed_postcommit_backup_corruption_is_detected_on_next_read(tmp_path: Path):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    assert workflow.status()["status"] == "completed"

    backup_path = state_root / "batch-final.v1.backup.json"
    _replace_regular_bytes(backup_path, b"postcommit-completed-backup-corruption")

    assert workflow.status()["status"] == "blocked"
    assert workflow.status()["error"] == "ocr_evidence_invalid"
    reopened = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("corrupt work must not rerun"),
    )
    assert reopened.status()["status"] == "blocked"
    assert reopened.status()["error"] == "ocr_evidence_invalid"


def test_running_postcommit_backup_corruption_blocks_before_worker_start(tmp_path: Path):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    paths = workflow_module.workflow_paths(state_root)
    workflow_module._migrate_legacy_artifacts(paths, engines.pdf_path)
    assert json.loads(paths.state.read_text(encoding="utf-8"))["schema_version"] == 2

    backup_path = state_root / "batch-state.v1.backup.json"
    _replace_regular_bytes(backup_path, b"postcommit-running-backup-corruption")
    worker_started = threading.Event()

    def unexpected_factory(_cancel):
        worker_started.set()
        raise AssertionError("worker must not start with a corrupt migration receipt")

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=unexpected_factory,
    )

    assert workflow._thread is None
    assert worker_started.is_set() is False
    assert workflow.status()["status"] == "blocked"
    assert workflow.status()["error"] == "ocr_state_invalid"


def test_running_worker_recovery_is_at_least_once_with_stable_request_configuration(
    tmp_path: Path,
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    first_attempt_path = tmp_path / "first-worker-attempt.json"
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_running_worker_process,
        args=(str(engines.pdf_path), str(state_root), str(first_attempt_path)),
    )

    process.start()
    process.join(10)

    assert process.exitcode == _EXIT_DURING_WORKER
    first_attempt = json.loads(first_attempt_path.read_text(encoding="utf-8"))
    second_attempts: list[dict[str, object]] = []

    class CompletingOrchestrator:
        def run_batch(self, pdf_path, **kwargs):
            second_attempts.append(
                {
                    "source_path": str(pdf_path),
                    "pages": list(kwargs["pages"]),
                    "dpi": kwargs["dpi"],
                    "languages": list(kwargs["languages"]),
                    "sample_rate": kwargs["sample_rate"],
                }
            )

            class Result:
                status = BatchStatus.CANCELLED
                error = "ocr_cancelled"

            return Result()

    recovered = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: CompletingOrchestrator(),
    )
    assert recovered._thread is not None
    recovered._thread.join(timeout=3)

    assert not recovered._thread.is_alive()
    expected_request_configuration = {
        "source_path": str(engines.pdf_path),
        "pages": [1],
        "dpi": 320,
        "languages": ["en-US"],
        "sample_rate": 0.05,
    }
    assert first_attempt == expected_request_configuration
    assert second_attempts == [expected_request_configuration]
    persisted = json.loads((state_root / "batch-state.json").read_text(encoding="utf-8"))
    assert persisted["run_config"] == {
        "pages": [1],
        "dpi": 320,
        "languages": ["en-US"],
        "sample_rate": 0.05,
    }


@pytest.mark.parametrize("replacement", ["different", "same"])
@pytest.mark.parametrize("target_name", ["swap", "manifest-candidate", "anchor", "journal"])
def test_cleanup_move_never_loses_inode_replaced_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    replacement: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_path_identity = workflow_module._path_identity
    injected_path: Path | None = None
    competitor_identity: tuple[int, int] | None = None

    def matches(path: Path) -> bool:
        if target_name == "swap":
            return path.name.endswith(".swap")
        if target_name == "manifest-candidate":
            return path.name.endswith(".manifest")
        if target_name == "anchor":
            return path.name.endswith(".anchor")
        return path == journal_path

    def replace_after_identity_check(path: Path) -> tuple[int, int]:
        nonlocal competitor_identity, injected_path
        identity = original_path_identity(path)
        if injected_path is None and matches(Path(path)):
            content = Path(path).read_bytes() if replacement == "same" else b"foreign-cleanup"
            _replace_regular_bytes(Path(path), content)
            info = Path(path).lstat()
            competitor_identity = (info.st_dev, info.st_ino)
            injected_path = Path(path)
        return identity

    monkeypatch.setattr(workflow_module, "_path_identity", replace_after_identity_check)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("ambiguous cleanup must not rerun"),
    )

    payload = workflow.status()
    assert injected_path is not None
    assert competitor_identity is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert _directory_contains_identity(state_root, competitor_identity)


@pytest.mark.parametrize("replacement", ["different", "same"])
def test_manifest_abort_never_loses_inode_replaced_after_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    manifest_path = state_root / "note-publication.json"
    backup_path = state_root / "batch-final.v1.backup.json"
    original_path_identity = workflow_module._path_identity
    competitor_identity: tuple[int, int] | None = None

    def tamper_dependency(point: str) -> None:
        if point == "completed_manifest_published":
            _replace_regular_bytes(backup_path, b"tampered-backup")

    def replace_after_identity_check(path: Path) -> tuple[int, int]:
        nonlocal competitor_identity
        identity = original_path_identity(path)
        if Path(path) == manifest_path and competitor_identity is None:
            content = manifest_path.read_bytes() if replacement == "same" else b"foreign-manifest"
            _replace_regular_bytes(manifest_path, content)
            info = manifest_path.lstat()
            competitor_identity = (info.st_dev, info.st_ino)
        return identity

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_dependency)
    monkeypatch.setattr(workflow_module, "_path_identity", replace_after_identity_check)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert competitor_identity is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert _directory_contains_identity(state_root, competitor_identity)


@pytest.mark.parametrize("journal_kind", ["symlink", "hardlink", "fifo"])
def test_completed_migration_recovery_rejects_unsafe_journal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal_kind: str
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def crash_after_journal(point: str) -> None:
        if point == "completed_journal_prepared":
            raise OSError("simulated migration process crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(workflow_module, "_migration_checkpoint", crash_after_journal)
        crashed = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
        assert crashed.status()["status"] == "blocked"

    journal_bytes = journal_path.read_bytes()
    backing = state_root / f"migration-journal-{journal_kind}.foreign"
    if journal_kind == "symlink":
        os.replace(journal_path, backing)
        journal_path.symlink_to(backing.name)
    elif journal_kind == "hardlink":
        os.link(journal_path, backing, follow_symlinks=False)
    else:
        journal_path.unlink()
        os.mkfifo(journal_path)

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsafe work must not rerun"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()
    if journal_kind != "fifo":
        assert backing.read_bytes() == journal_bytes


def test_completed_migration_recovery_rejects_replaced_transaction_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    journal_path = state_root / "ocr-v1-v2-migration.json"

    def crash_after_journal(point: str) -> None:
        if point == "completed_journal_prepared":
            raise OSError("simulated migration process crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(workflow_module, "_migration_checkpoint", crash_after_journal)
        crashed = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
        assert crashed.status()["status"] == "blocked"

    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    anchor = state_root / journal["transaction_anchor_basename"]
    original_identity = (anchor.stat().st_dev, anchor.stat().st_ino)
    _replace_regular_bytes(anchor, anchor.read_bytes())
    assert (anchor.stat().st_dev, anchor.stat().st_ino) != original_identity

    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("replaced anchor must not recover"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert journal_path.is_file()
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()


def test_legacy_completed_migration_cas_rejects_replaced_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    replacement = copy.deepcopy(legacy)
    replacement["updated_at"] += 1
    replaced = False

    def replace_after_prepare(point: str) -> None:
        nonlocal replaced
        if point == "completed_journal_prepared" and not replaced:
            workflow_module._atomic_json(final_path, replacement)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_after_prepare)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("replaced work must not rerun"),
    )

    payload = workflow.status()
    assert replaced is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert json.loads(final_path.read_text(encoding="utf-8")) == replacement
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()


def test_migration_journal_creation_is_no_replace_even_for_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    original_publish = workflow_module._rename_no_replace
    intruder: bytes | None = None

    def collide_with_identical_journal(source, target):
        nonlocal intruder
        if Path(target) == journal_path and intruder is None:
            intruder = Path(source).read_bytes()
            journal_path.write_bytes(intruder)
            raise FileExistsError
        return original_publish(source, target)

    monkeypatch.setattr(workflow_module, "_rename_no_replace", collide_with_identical_journal)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("collided migration must not rerun"),
    )

    payload = workflow.status()
    assert intruder is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert journal_path.read_bytes() == intruder
    assert json.loads(final_path.read_text(encoding="utf-8")) == legacy
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("foreign journal must not recover"),
    )
    assert restarted.status()["status"] == "blocked"
    assert restarted.status()["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == workflow_module._canonical_json_bytes(legacy)


def test_migration_transaction_anchor_creation_is_strictly_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    original_publish = workflow_module._rename_no_replace
    intruder_path: Path | None = None
    intruder_bytes: bytes | None = None

    def collide_with_identical_anchor(source, target):
        nonlocal intruder_bytes, intruder_path
        target_path = Path(target)
        if target_path.name.endswith(".anchor") and intruder_path is None:
            intruder_path = target_path
            intruder_bytes = Path(source).read_bytes()
            target_path.write_bytes(intruder_bytes)
            raise FileExistsError
        return original_publish(source, target)

    monkeypatch.setattr(workflow_module, "_rename_no_replace", collide_with_identical_anchor)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("collided migration must not rerun"),
    )

    payload = workflow.status()
    assert intruder_path is not None
    assert intruder_bytes is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert intruder_path.read_bytes() == intruder_bytes
    assert final_path.read_bytes() == workflow_module._canonical_json_bytes(legacy)
    journal = json.loads((state_root / "ocr-v1-v2-migration.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "intent"
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()


def test_completed_source_cas_preserves_competing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    competitor = copy.deepcopy(legacy)
    competitor["updated_at"] += 1
    competitor_bytes = workflow_module._canonical_json_bytes(competitor)
    replaced = False

    def replace_at_cas_boundary(point: str) -> None:
        nonlocal replaced
        if point == "completed_source_cas_ready" and not replaced:
            _replace_regular_bytes(final_path, competitor_bytes)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_at_cas_boundary)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("competing final must not rerun"),
    )

    payload = workflow.status()
    assert replaced is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == competitor_bytes
    assert (state_root / "ocr-v1-v2-migration.json").is_file()
    assert not (state_root / "note-publication.json").exists()


def test_completed_source_cas_rejects_byte_identical_replacement_stably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    original = final_path.read_bytes()
    replaced = False
    foreign_identity: tuple[int, int] | None = None

    def replace_at_cas_boundary(point: str) -> None:
        nonlocal foreign_identity, replaced
        if point == "completed_source_cas_ready" and not replaced:
            _replace_regular_bytes(final_path, original)
            info = final_path.lstat()
            foreign_identity = (info.st_dev, info.st_ino)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_at_cas_boundary)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()
    assert replaced is True
    assert foreign_identity is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == original
    assert (final_path.stat().st_dev, final_path.stat().st_ino) == foreign_identity
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    assert (journal["swap_expected_dev"], journal["swap_expected_ino"]) != foreign_identity
    assert journal["swap_displaced_dev"] is None
    assert journal["swap_displaced_ino"] is None
    assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))

    for _attempt in range(2):
        restarted = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail(
                "byte-identical foreign source must not recover"
            ),
        )
        restarted_payload = restarted.status()
        assert restarted_payload["status"] == "blocked"
        assert restarted_payload["error"] == "ocr_evidence_invalid"
        assert json.loads(journal_path.read_text(encoding="utf-8"))["phase"] == (
            "source_swap_prepared"
        )
        assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))


def test_completed_source_swap_revalidates_target_path_after_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    replaced = False
    foreign_identity: tuple[int, int] | None = None

    def replace_after_swap_validation(point: str) -> None:
        nonlocal foreign_identity, replaced
        if point == "completed_source_swapped" and not replaced:
            _replace_regular_bytes(final_path, final_path.read_bytes())
            info = final_path.lstat()
            foreign_identity = (info.st_dev, info.st_ino)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_after_swap_validation)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()
    assert replaced is True
    assert foreign_identity is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    assert journal["swap_displaced_dev"] is None
    assert journal["swap_displaced_ino"] is None
    assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))

    restarted = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail(
            "byte-identical foreign target must not recover"
        ),
    )
    assert restarted.status()["status"] == "blocked"
    assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))


def test_running_source_cas_preserves_competing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
    state_path = state_root / "batch-state.json"
    competitor = copy.deepcopy(legacy)
    competitor["updated_at"] += 1
    competitor_bytes = workflow_module._canonical_json_bytes(competitor)
    replaced = False

    def replace_at_cas_boundary(point: str) -> None:
        nonlocal replaced
        if point == "running_source_cas_ready" and not replaced:
            _replace_regular_bytes(state_path, competitor_bytes)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_at_cas_boundary)
    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )
    if workflow._thread is not None:
        workflow._thread.join(timeout=3)

    payload = workflow.status()
    assert replaced is True
    assert workflow._thread is None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert state_path.read_bytes() == competitor_bytes
    assert (state_root / "ocr-v1-v2-migration.json").is_file()


def test_running_source_cas_rejects_byte_identical_replacement_stably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    state_path = state_root / "batch-state.json"
    original = state_path.read_bytes()
    replaced = False
    foreign_identity: tuple[int, int] | None = None

    def replace_at_cas_boundary(point: str) -> None:
        nonlocal foreign_identity, replaced
        if point == "running_source_cas_ready" and not replaced:
            _replace_regular_bytes(state_path, original)
            info = state_path.lstat()
            foreign_identity = (info.st_dev, info.st_ino)
            replaced = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", replace_at_cas_boundary)
    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )
    if workflow._thread is not None:
        workflow._thread.join(timeout=3)

    payload = workflow.status()
    assert replaced is True
    assert foreign_identity is not None
    assert workflow._thread is None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["phase"] == "source_swap_prepared"
    assert journal["swap_displaced_dev"] is None
    assert journal["swap_displaced_ino"] is None
    assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))

    restarted = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail(
            "byte-identical foreign running state must not recover"
        ),
    )
    assert restarted._thread is None
    assert restarted.status()["status"] == "blocked"
    assert not tuple(state_root.glob(f".ocr-v1-v2-{journal['transaction_id']}.sealed"))


@pytest.mark.parametrize("target_name", ["final", "artifact", "backup"])
@pytest.mark.parametrize("operation", ["replace", "same-replace", "delete"])
def test_completed_migration_revalidates_dependencies_before_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    operation: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    manifest_path = state_root / "note-publication.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    tampered_path: Path | None = None
    tampered_bytes = f"tampered-{target_name}-{operation}".encode()
    expected_after: bytes | None = None

    def tamper_before_manifest(point: str) -> None:
        nonlocal expected_after, tampered_path
        if point != "completed_final_published" or tampered_path is not None:
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        paths = {
            "final": state_root / "batch-final.json",
            "artifact": state_root / f"intensive-reading.{journal['artifact_sha256']}.md",
            "backup": state_root / "batch-final.v1.backup.json",
        }
        tampered_path = paths[target_name]
        if operation in {"replace", "same-replace"}:
            expected_after = (
                tampered_path.read_bytes() if operation == "same-replace" else tampered_bytes
            )
            _replace_regular_bytes(tampered_path, expected_after)
        else:
            tampered_path.unlink()

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_before_manifest)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert tampered_path is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert journal_path.is_file()
    assert not manifest_path.exists()
    if expected_after is not None:
        assert tampered_path.read_bytes() == expected_after
    else:
        assert not tampered_path.exists()


@pytest.mark.parametrize("target_name", ["final", "artifact", "backup"])
@pytest.mark.parametrize("operation", ["replace", "same-replace", "delete"])
def test_completed_migration_removes_own_manifest_after_dependency_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    operation: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    manifest_path = state_root / "note-publication.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    tampered_path: Path | None = None
    tampered_bytes = f"post-manifest-{target_name}-{operation}".encode()
    expected_after: bytes | None = None

    def tamper_after_manifest(point: str) -> None:
        nonlocal expected_after, tampered_path
        if point != "completed_manifest_published" or tampered_path is not None:
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        paths = {
            "final": state_root / "batch-final.json",
            "artifact": state_root / f"intensive-reading.{journal['artifact_sha256']}.md",
            "backup": state_root / "batch-final.v1.backup.json",
        }
        tampered_path = paths[target_name]
        if operation in {"replace", "same-replace"}:
            expected_after = (
                tampered_path.read_bytes() if operation == "same-replace" else tampered_bytes
            )
            _replace_regular_bytes(tampered_path, expected_after)
        else:
            tampered_path.unlink()

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_after_manifest)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert tampered_path is not None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert journal_path.is_file()
    assert not manifest_path.exists()
    if expected_after is not None:
        assert tampered_path.read_bytes() == expected_after
    else:
        assert not tampered_path.exists()


@pytest.mark.parametrize(
    "intruder_kind",
    ["same-regular", "different-regular", "symlink", "hardlink", "fifo"],
)
def test_migration_manifest_publication_is_strictly_no_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intruder_kind: str,
):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    manifest_path = state_root / "note-publication.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    backing = state_root / f"manifest-{intruder_kind}.foreign"
    original_link = workflow_module.os.link
    collided = False
    expected_regular: bytes | None = None

    def collide_before_manifest_link(source, target, *, follow_symlinks=True):
        nonlocal collided, expected_regular
        if Path(target) != manifest_path or collided:
            return original_link(source, target, follow_symlinks=follow_symlinks)
        candidate = Path(source).read_bytes()
        if intruder_kind == "same-regular":
            expected_regular = candidate
            manifest_path.write_bytes(candidate)
        elif intruder_kind == "different-regular":
            expected_regular = b'{"foreign":true}'
            manifest_path.write_bytes(expected_regular)
        elif intruder_kind == "symlink":
            backing.write_bytes(candidate)
            manifest_path.symlink_to(backing.name)
        elif intruder_kind == "hardlink":
            backing.write_bytes(candidate)
            original_link(backing, manifest_path, follow_symlinks=False)
        else:
            os.mkfifo(manifest_path)
        collided = True
        raise FileExistsError

    monkeypatch.setattr(workflow_module.os, "link", collide_before_manifest_link)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("collided work must not rerun"),
    )

    payload = workflow.status()
    assert collided is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert journal_path.is_file()
    info = manifest_path.lstat()
    if expected_regular is not None:
        assert stat.S_ISREG(info.st_mode)
        assert manifest_path.read_bytes() == expected_regular
    elif intruder_kind == "symlink":
        assert stat.S_ISLNK(info.st_mode)
    elif intruder_kind == "hardlink":
        assert stat.S_ISREG(info.st_mode)
        assert info.st_nlink == 2
    else:
        assert stat.S_ISFIFO(info.st_mode)


@pytest.mark.parametrize("target_name", ["backup", "artifact"])
@pytest.mark.parametrize("intruder_kind", ["symlink", "hardlink", "fifo"])
def test_migration_rejects_same_content_unsafe_dependency_paths(
    tmp_path: Path, target_name: str, intruder_kind: str
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    expected = (
        final_path.read_bytes()
        if target_name == "backup"
        else (state_root / "intensive-reading.md").read_bytes()
    )
    target = (
        state_root / "batch-final.v1.backup.json"
        if target_name == "backup"
        else state_root / f"intensive-reading.{legacy['markdown_sha256']}.md"
    )
    backing = state_root / f"{target_name}-{intruder_kind}.foreign"
    if intruder_kind == "symlink":
        backing.write_bytes(expected)
        target.symlink_to(backing.name)
    elif intruder_kind == "hardlink":
        backing.write_bytes(expected)
        os.link(backing, target, follow_symlinks=False)
    else:
        os.mkfifo(target)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsafe work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert final_path.read_bytes() == workflow_module._canonical_json_bytes(legacy)
    assert target.lstat()
    assert not (state_root / "note-publication.json").exists()


@pytest.mark.parametrize("target_name", ["backup", "artifact"])
def test_migration_rejects_unbound_same_content_regular_dependency(
    tmp_path: Path,
    target_name: str,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    if target_name == "backup":
        target = state_root / "batch-final.v1.backup.json"
        target.write_bytes(final_path.read_bytes())
    else:
        target = state_root / f"intensive-reading.{legacy['markdown_sha256']}.md"
        target.write_bytes((state_root / "intensive-reading.md").read_bytes())
    original_content = target.read_bytes()
    original_identity = (target.stat().st_dev, target.stat().st_ino)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert target.read_bytes() == original_content
    assert (target.stat().st_dev, target.stat().st_ino) == original_identity
    assert payload["publishable"] is False
    assert target.is_file()
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize("source_kind", ["symlink", "hardlink", "fifo"])
def test_migration_rejects_unsafe_legacy_source_path(tmp_path: Path, source_kind: str):
    source_path, state_root, _legacy = _seed_legacy_published_final(tmp_path)
    final_path = state_root / "batch-final.json"
    backing = state_root / f"batch-final-{source_kind}.foreign"
    if source_kind == "symlink":
        os.replace(final_path, backing)
        final_path.symlink_to(backing.name)
    elif source_kind == "hardlink":
        os.link(final_path, backing, follow_symlinks=False)
    else:
        final_path.unlink()
        os.mkfifo(final_path)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsafe work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert not (state_root / "ocr-v1-v2-migration.json").exists()
    assert not (state_root / "batch-final.v1.backup.json").exists()
    assert not (state_root / "note-publication.json").exists()


def test_legacy_completed_migration_never_overwrites_foreign_backup(tmp_path: Path):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    backup_path = state_root / "batch-final.v1.backup.json"
    foreign_backup = b'{"schema_version":1,"tampered":true}'
    backup_path.write_bytes(foreign_backup)

    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsafe work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert backup_path.read_bytes() == foreign_backup
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert not (state_root / "note-publication.json").exists()


def test_legacy_completed_migration_never_overwrites_backup_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    backup_path = state_root / "batch-final.v1.backup.json"
    foreign_backup = b'{"schema_version":1,"raced":true}'
    original_publish = workflow_module._rename_no_replace
    collided = False

    def collide_before_backup_publish(source, target):
        nonlocal collided
        if Path(target) == backup_path and not collided:
            backup_path.write_bytes(foreign_backup)
            collided = True
            raise FileExistsError
        return original_publish(source, target)

    monkeypatch.setattr(workflow_module, "_rename_no_replace", collide_before_backup_publish)
    workflow = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unsafe work must not rerun"),
    )

    payload = workflow.status()
    assert collided is True
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert backup_path.read_bytes() == foreign_backup
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert not (state_root / "note-publication.json").exists()


def test_legacy_completed_migration_blocks_tampered_backup_during_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    backup_path = state_root / "batch-final.v1.backup.json"

    def crash_after_backup(point: str) -> None:
        if point == "completed_backup_published":
            raise OSError("simulated migration process crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(workflow_module, "_migration_checkpoint", crash_after_backup)
        crashed = OcrWorkflow(
            source_path=source_path,
            state_root=state_root,
            orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
        )
        assert crashed.status()["status"] == "blocked"

    backup_path.write_bytes(b'{"schema_version":1,"tampered":true}')
    recovered = OcrWorkflow(
        source_path=source_path,
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = recovered.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert backup_path.read_bytes() == b'{"schema_version":1,"tampered":true}'
    assert not (state_root / "note-publication.json").exists()


def test_two_processes_migrate_one_legacy_completed_final_once(tmp_path: Path):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    first_ready = context.Event()
    second_ready = context.Event()
    start = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_migrate_legacy_completed_process,
        args=(str(source_path), str(state_root), first_ready, start, results),
    )
    second = context.Process(
        target=_migrate_legacy_completed_process,
        args=(str(source_path), str(state_root), second_ready, start, results),
    )
    first.start()
    second.start()
    assert first_ready.wait(5)
    assert second_ready.wait(5)
    start.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    payloads = [results.get(timeout=2), results.get(timeout=2)]
    assert [payload["status"] for payload in payloads] == ["completed", "completed"]
    assert all(payload["publishable"] is True for payload in payloads)
    migrated = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    manifest = json.loads((state_root / "note-publication.json").read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2
    assert manifest["final_snapshot_sha256"] == workflow_module._json_fingerprint(migrated)
    assert (
        json.loads((state_root / "batch-final.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not (state_root / "ocr-v1-v2-migration.json").exists()


def test_publication_lock_inode_replacement_cannot_split_migration_transaction(
    tmp_path: Path,
):
    source_path, state_root, legacy = _seed_legacy_published_final(tmp_path)
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    release_first = context.Event()
    second_started = context.Event()
    second_entered = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_pause_completed_migration_process,
        args=(
            str(source_path),
            str(state_root),
            "completed_journal_prepared",
            first_entered,
            release_first,
            results,
        ),
    )
    second = context.Process(
        target=_observe_completed_migration_process,
        args=(
            str(source_path),
            str(state_root),
            "completed_backup_published",
            second_started,
            second_entered,
            results,
        ),
    )
    raced = False
    try:
        first.start()
        assert first_entered.wait(5)
        lock_path = state_root / ".note-publication.lock"
        old_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        replacement = state_root / ".replacement-publication.lock"
        replacement.write_bytes(lock_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, lock_path)
        assert (lock_path.stat().st_dev, lock_path.stat().st_ino) != old_identity

        second.start()
        assert second_started.wait(5)
        raced = second_entered.wait(2)
    finally:
        release_first.set()
        first.join(timeout=10)
        second.join(timeout=10)

    assert raced is False
    assert first.exitcode == 0
    assert second.exitcode == 0
    payloads = [results.get(timeout=2), results.get(timeout=2)]
    assert [payload["status"] for payload in payloads] == ["blocked", "blocked"]
    assert all(payload["error"] == "ocr_evidence_invalid" for payload in payloads)
    assert json.loads((state_root / "batch-final.json").read_text(encoding="utf-8")) == legacy
    assert (state_root / "ocr-v1-v2-migration.json").is_file()
    assert not (state_root / "note-publication.json").exists()


def test_second_process_cannot_start_duplicate_worker_for_migrated_running_state(
    tmp_path: Path,
):
    engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)

    context = multiprocessing.get_context("fork")
    worker_started = context.Event()
    release_worker = context.Event()
    first_constructed = context.Event()
    second_constructed = context.Event()
    worker_count = context.Value("i", 0)
    results = context.Queue()
    first = context.Process(
        target=_resume_legacy_running_process,
        args=(
            "first",
            str(engines.pdf_path),
            str(state_root),
            worker_started,
            release_worker,
            first_constructed,
            worker_count,
            results,
        ),
    )
    second = context.Process(
        target=_resume_legacy_running_process,
        args=(
            "second",
            str(engines.pdf_path),
            str(state_root),
            worker_started,
            release_worker,
            second_constructed,
            worker_count,
            results,
        ),
    )
    first.start()
    assert worker_started.wait(5)
    assert first_constructed.wait(5)
    second.start()
    assert second_constructed.wait(5)

    observed = dict(
        (label, (has_thread, status))
        for label, has_thread, status in [
            results.get(timeout=2),
            results.get(timeout=2),
        ]
    )
    assert worker_count.value == 1
    assert observed["first"] == (True, "running")
    assert observed["second"] == (False, "blocked")
    release_worker.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    migrated = json.loads((state_root / "batch-state.json").read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2
    assert migrated["status"] == "running"
    assert (
        json.loads((state_root / "batch-state.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not (state_root / "ocr-v1-v2-migration.json").exists()
    assert not tuple(state_root.glob(".ocr-v1-v2-*.anchor"))


def test_worker_lock_inode_replacement_does_not_admit_duplicate_worker(tmp_path: Path):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    context = multiprocessing.get_context("fork")
    worker_started = context.Event()
    release_worker = context.Event()
    first_constructed = context.Event()
    second_constructed = context.Event()
    worker_count = context.Value("i", 0)
    results = context.Queue()
    first = context.Process(
        target=_resume_legacy_running_process,
        args=(
            "first",
            str(engines.pdf_path),
            str(state_root),
            worker_started,
            release_worker,
            first_constructed,
            worker_count,
            results,
        ),
    )
    second = context.Process(
        target=_resume_legacy_running_process,
        args=(
            "second",
            str(engines.pdf_path),
            str(state_root),
            worker_started,
            release_worker,
            second_constructed,
            worker_count,
            results,
        ),
    )
    try:
        first.start()
        assert worker_started.wait(5)
        assert first_constructed.wait(5)
        lock_path = state_root / ".ocr-worker.lock"
        old_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        replacement = state_root / ".replacement-worker.lock"
        replacement.write_bytes(lock_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, lock_path)
        assert (lock_path.stat().st_dev, lock_path.stat().st_ino) != old_identity

        second.start()
        assert second_constructed.wait(5)
        observed = dict(
            (label, (has_thread, status))
            for label, has_thread, status in [
                results.get(timeout=2),
                results.get(timeout=2),
            ]
        )
        assert worker_count.value == 1
        assert observed["first"] == (True, "running")
        assert observed["second"] == (False, "blocked")
    finally:
        release_worker.set()
        first.join(timeout=10)
        second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0


def test_running_resume_releases_worker_claim_when_thread_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engines = FakeEngines()
    seeded = _orchestrator(tmp_path, engines)
    state = seeded._load_or_create_state(engines.pdf_path, [1], 320, ["en-US"], 0.2, deadline=None)
    seeded._set_status(state, BatchStatus.RUNNING)
    original_start = workflow_module.threading.Thread.start

    with monkeypatch.context() as failed_start:
        failed_start.setattr(
            workflow_module.threading.Thread,
            "start",
            lambda _thread: (_ for _ in ()).throw(RuntimeError("thread start failed")),
        )
        failed = OcrWorkflow(
            source_path=engines.pdf_path,
            state_root=tmp_path / "ocr-state",
            orchestrator_factory=lambda cancel: _orchestrator(
                tmp_path, engines, is_cancelled=cancel
            ),
        )
        assert failed._thread is None
        assert failed.status()["status"] == "blocked"
        assert failed.status()["error"] == "ocr_state_invalid"

    assert workflow_module.threading.Thread.start is original_start
    recovered = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=tmp_path / "ocr-state",
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )
    assert recovered._thread is not None
    recovered._thread.join(timeout=3)
    assert not recovered._thread.is_alive()
    assert recovered.status()["status"] == "completed"


@pytest.mark.parametrize(
    "crash_point",
    [
        "running_journal_prepared",
        "running_backup_published",
        "running_source_cas_ready",
        "running_source_swapped",
        "running_state_published",
        "running_commit_ready",
        "running_cleanup_ready",
    ],
)
def test_legacy_running_migration_preserves_rerun_intent_across_every_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
):
    engines, state_root, legacy = _seed_inferable_legacy_running_state(tmp_path)
    state_path = state_root / "batch-state.json"

    def crash_at_boundary(point: str) -> None:
        if point == crash_point:
            raise OSError("simulated migration process crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(workflow_module, "_migration_checkpoint", crash_at_boundary)
        crashed = OcrWorkflow(
            source_path=engines.pdf_path,
            state_root=state_root,
            orchestrator_factory=lambda cancel: _orchestrator(
                tmp_path, engines, is_cancelled=cancel
            ),
        )
        assert crashed._thread is None
        assert crashed.status()["status"] == "blocked"

    assert state_path.is_file()
    assert (state_root / "ocr-v1-v2-migration.json").is_file()
    recovered = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )

    assert recovered._thread is not None
    recovered._thread.join(timeout=3)
    assert not recovered._thread.is_alive()
    assert recovered.status()["status"] == "completed"
    assert (
        json.loads((state_root / "batch-state.v1.backup.json").read_text(encoding="utf-8"))
        == legacy
    )
    assert not (state_root / "ocr-v1-v2-migration.json").exists()
    assert not tuple(state_root.glob(".ocr-v1-v2-*.anchor"))


@pytest.mark.parametrize("operation", ["replace", "same-replace", "delete"])
def test_running_migration_revalidates_backup_before_journal_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    engines, state_root, _legacy = _seed_inferable_legacy_running_state(tmp_path)
    backup_path = state_root / "batch-state.v1.backup.json"
    journal_path = state_root / "ocr-v1-v2-migration.json"
    tampered = False
    tampered_bytes = b'{"tampered-running-backup":true}'
    expected_after: bytes | None = None

    def tamper_after_state(point: str) -> None:
        nonlocal expected_after, tampered
        if point != "running_state_published" or tampered:
            return
        if operation in {"replace", "same-replace"}:
            expected_after = (
                backup_path.read_bytes() if operation == "same-replace" else tampered_bytes
            )
            _replace_regular_bytes(backup_path, expected_after)
        else:
            backup_path.unlink()
        tampered = True

    monkeypatch.setattr(workflow_module, "_migration_checkpoint", tamper_after_state)
    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=state_root,
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )
    if workflow._thread is not None:
        workflow._thread.join(timeout=3)

    payload = workflow.status()
    assert tampered is True
    assert workflow._thread is None
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"
    assert journal_path.is_file()
    if expected_after is not None:
        assert backup_path.read_bytes() == expected_after
    else:
        assert not backup_path.exists()


def test_corrupt_v1_completed_final_is_blocked_without_migration(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    legacy = _legacy_v1_final(final)
    legacy["pdf_snapshot"]["sha256"] = "0" * 64
    workflow_module._atomic_json(state_root / "batch-final.json", legacy)

    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("corrupt final must not rerun"),
    )

    assert workflow.status()["status"] == "blocked"
    assert not (state_root / "batch-final.v1.backup.json").exists()


def test_v1_completed_final_with_unknown_field_is_blocked_without_migration(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    legacy = _legacy_v1_final(final)
    legacy["generation"] = 2
    workflow_module._atomic_json(state_root / "batch-final.json", legacy)

    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("unknown final must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    assert not (state_root / "batch-final.v1.backup.json").exists()


@pytest.mark.parametrize(
    "attack",
    ["manifest-symlink", "path-traversal", "artifact-symlink", "artifact-hardlink"],
)
def test_untrusted_publication_manifest_or_artifact_is_rejected(tmp_path: Path, attack: str):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    artifact = _write_content_addressed_publication(state_root, final, tree, confirmation, markdown)
    manifest_path = state_root / "note-publication.json"
    if attack == "manifest-symlink":
        backing = state_root / "untrusted-manifest.json"
        manifest_path.replace(backing)
        manifest_path.symlink_to(backing)
    elif attack == "path-traversal":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_basename"] = f"../{artifact.name}"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif attack == "artifact-symlink":
        backing = state_root / "untrusted-note.md"
        artifact.replace(backing)
        artifact.symlink_to(backing)
    else:
        os.link(artifact, state_root / "extra-artifact-link.md")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_publication_invalid"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "permissions"])
def test_generate_rejects_untrusted_state_root_publication_lock(tmp_path: Path, attack: str):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    lock_path = state_root / ".note-publication.lock"
    backing = state_root / "untrusted-publication-lock"
    backing.write_text("", encoding="utf-8")
    if attack == "symlink":
        lock_path.unlink()
        lock_path.symlink_to(backing)
    elif attack == "hardlink":
        lock_path.unlink()
        os.link(backing, lock_path)
    else:
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o644)
    callback_called = False
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    def generate(_output_path: Path):
        nonlocal callback_called
        callback_called = True
        pytest.fail("unsafe lock must be rejected before generation")

    with pytest.raises(ValueError, match="publication lock"):
        workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )
    assert callback_called is False


def test_generation_rejects_replaced_lock_before_manifest_commit(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    replacement = _valid_markdown(final, tree, confirmation, concept="锁替换后的内容")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    previous_manifest = workflow.paths.publication.read_bytes()

    def replace_lock(output_path: Path):
        workflow.paths.publication_lock.unlink()
        workflow.paths.publication_lock.write_bytes(b"")
        workflow.paths.publication_lock.chmod(0o600)
        output_path.write_text(replacement, encoding="utf-8")
        return {"markdown": replacement, "metadata": metadata}

    with pytest.raises(ValueError, match="publication lock"):
        workflow.generate_and_publish(
            replace_lock,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    assert workflow.paths.publication.read_bytes() == previous_manifest


def test_generation_rolls_back_when_lock_is_replaced_after_manifest_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    replacement = _valid_markdown(final, tree, confirmation, concept="提交后锁替换")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    previous_manifest = workflow.paths.publication.read_bytes()
    original_atomic_json = workflow_module._atomic_json

    def replace_lock_after_commit(path: Path, value: dict):
        original_atomic_json(path, value)
        if path == workflow.paths.publication:
            workflow.paths.publication_lock.unlink()
            workflow.paths.publication_lock.write_bytes(b"")
            workflow.paths.publication_lock.chmod(0o600)

    monkeypatch.setattr(workflow_module, "_atomic_json", replace_lock_after_commit)

    def generate(output_path: Path):
        output_path.write_text(replacement, encoding="utf-8")
        return {"markdown": replacement, "metadata": metadata}

    with pytest.raises(ValueError, match="publication lock"):
        workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    assert workflow.paths.publication.read_bytes() == previous_manifest


def test_invalid_completed_final_is_blocked(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "batch-final.json").write_text(
        '{"status":"completed","input_fingerprint":"input-1","pages":{}}', encoding="utf-8"
    )
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: None,  # type: ignore[return-value]
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None
    assert payload["error"] == "ocr_evidence_invalid"


def test_completed_workflow_remains_completed_after_process_restart(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path, publish_note=False)
    (state_root / "batch-state.json").write_text('{"status":"running"}', encoding="utf-8")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["error"] is None
    assert workflow.detect_chapters()["chapters"]


def test_completed_workflow_restart_uses_persisted_nondefault_sample_rate(tmp_path: Path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    result = orchestrator.run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=300,
        languages=["zh-Hans"],
        sample_rate=0.7,
    )
    assert result.status is BatchStatus.COMPLETED

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=tmp_path / "ocr-state",
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "completed"
    assert payload["error"] is None


def test_completed_workflow_blocks_tampered_run_config_and_input_fingerprint(
    tmp_path: Path,
):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    result = orchestrator.run_batch(
        engines.pdf_path,
        pages=[1],
        dpi=300,
        languages=["zh-Hans"],
        sample_rate=0.7,
    )
    assert result.status is BatchStatus.COMPLETED
    final_path = tmp_path / "ocr-state" / "batch-final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["run_config"]["sample_rate"] = 0.6
    final["input_fingerprint"] = workflow_module._json_fingerprint(
        {"pdf_snapshot": final["pdf_snapshot"], **final["run_config"]}
    )
    workflow_module._atomic_json(final_path, final)

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=tmp_path / "ocr-state",
        orchestrator_factory=lambda _cancel: pytest.fail("tampered work must not rerun"),
    )

    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"


def test_invalid_completed_final_cannot_detect_chapters(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    del final["pages"]["1"]["decision"]
    (state_root / "batch-final.json").write_text(json.dumps(final), encoding="utf-8")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"
    with pytest.raises(ValueError, match="OCR 尚未完成"):
        workflow.detect_chapters()


def test_invalid_publication_does_not_block_chapter_detection(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path)
    note = _published_artifact_path(state_root)
    note.write_text(
        note.read_text(encoding="utf-8").replace("概念内容", "被篡改内容"),
        encoding="utf-8",
    )
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["error"] == "ocr_publication_invalid"
    assert workflow.detect_chapters()["chapters"]


def test_detect_chapters_uses_the_same_validated_final_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path)
    final_path = state_root / "batch-final.json"
    original_read = workflow_module._read_regular_json
    final_reads = 0

    def replace_final_after_read(path: Path):
        nonlocal final_reads
        value = original_read(path)
        if path == final_path:
            final_reads += 1
            final_path.write_text(
                '{"status":"completed","input_fingerprint":"unverified","pages":{}}',
                encoding="utf-8",
            )
        return value

    monkeypatch.setattr(workflow_module, "_read_regular_json", replace_final_after_read)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    tree = workflow.detect_chapters()

    assert tree["chapters"]
    assert final_reads == 1


def test_status_does_not_publish_when_final_changes_during_publication_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    final_path = state_root / "batch-final.json"
    note_path = _published_artifact_path(state_root)
    replacement = dict(final)
    replacement["generation"] = 2
    original_read = workflow_module._read_regular_bytes
    replaced = False

    def replace_final_while_reading_note(path: Path):
        nonlocal replaced
        content = original_read(path)
        if path == note_path and not replaced:
            final_path.write_text(json.dumps(replacement), encoding="utf-8")
            replaced = True
        return content

    monkeypatch.setattr(workflow_module, "_read_regular_bytes", replace_final_while_reading_note)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )

    payload = workflow.status()

    assert replaced is True
    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["error"] == "ocr_publication_invalid"


def test_bind_published_note_rejects_a_replaced_final_snapshot(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    note = state_root / "intensive-reading.md"
    note.write_text("# generated\n", encoding="utf-8")
    replaced = dict(final)
    replaced["concurrent_update"] = True
    (state_root / "batch-final.json").write_text(json.dumps(replaced), encoding="utf-8")

    with pytest.raises(ValueError, match="changed during note generation"):
        bind_published_note(
            state_root / "batch-final.json",
            note,
            {
                "model": "deepseek-v4-pro",
                "prompt_rules_version": "mba-intensive-reading-v1",
            },
            expected_final=final,
        )


def test_bind_published_note_never_overwrites_a_final_replaced_after_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    final_path = state_root / "batch-final.json"
    note_path = _published_artifact_path(state_root)
    replacement = dict(final)
    replacement["generation"] = 2
    metadata = _published_note_metadata(state_root, final)
    original_read = workflow_module._read_regular_bytes
    replaced = False

    def replace_final_after_comparison(path: Path):
        nonlocal replaced
        content = original_read(path)
        if path == note_path and not replaced:
            final_path.write_text(json.dumps(replacement), encoding="utf-8")
            replaced = True
        return content

    monkeypatch.setattr(workflow_module, "_read_regular_bytes", replace_final_after_comparison)

    with pytest.raises(ValueError, match="changed during note generation"):
        bind_published_note(
            final_path,
            note_path,
            metadata,
            expected_final=final,
        )

    assert replaced is True
    assert json.loads(final_path.read_text(encoding="utf-8")) == replacement
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["publishable"] is False
    assert payload["error"] == "ocr_evidence_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_fingerprint", ""),
        ("chapter_fingerprint", ""),
        ("chapter_fingerprint", "foreign-chapter"),
        ("evidence_fingerprint", ""),
        ("evidence_fingerprint", "foreign-evidence"),
    ],
)
def test_bind_published_note_rejects_metadata_outside_verified_context(
    tmp_path: Path, field: str, value: str
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    metadata = _published_note_metadata(state_root, final)
    metadata[field] = value

    with pytest.raises(ValueError, match="fingerprint is invalid"):
        bind_published_note(
            state_root / "batch-final.json",
            _published_artifact_path(state_root),
            metadata,
            expected_final=final,
        )


def test_generate_rejects_unknown_publication_metadata_without_replacing_manifest(tmp_path: Path):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    metadata = _note_metadata(final, tree, confirmation)
    metadata["api_secret"] = "sk-secret-must-not-be-persisted"
    replacement = _valid_markdown(final, tree, confirmation, concept="未知元数据")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("completed work must not rerun"),
    )
    previous_manifest = workflow.paths.publication.read_bytes()

    def generate(output_path: Path):
        output_path.write_text(replacement, encoding="utf-8")
        return {"markdown": replacement, "metadata": metadata}

    with pytest.raises(ValueError, match="metadata.*unexpected"):
        workflow.generate_and_publish(
            generate,
            expected_final=final,
            expected_tree=tree,
            confirmation=confirmation,
        )

    assert workflow.paths.publication.read_bytes() == previous_manifest


@pytest.mark.parametrize("persisted", [{}, {"status": "future-status"}])
def test_unknown_or_missing_persisted_status_is_stably_blocked(
    tmp_path: Path, persisted: dict[str, object]
):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "batch-state.json").write_text(json.dumps(persisted), encoding="utf-8")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("invalid work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"


def test_persisted_running_status_is_blocked_after_process_restart(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "batch-state.json").write_text(
        json.dumps({"status": "running", "error": "provider still active"}), encoding="utf-8"
    )
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("running work must not resume itself"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_interrupted"


def test_valid_interrupted_running_state_auto_resumes_exact_persisted_config(tmp_path: Path):
    engines = FakeEngines()
    seeded = _orchestrator(tmp_path, engines)
    state = seeded._load_or_create_state(
        engines.pdf_path,
        [1],
        320,
        ["en-US", "zh-Hans"],
        0.2,
        deadline=None,
    )
    seeded._set_status(state, BatchStatus.RUNNING)
    engines.calls.clear()

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=tmp_path / "ocr-state",
        orchestrator_factory=lambda cancel: _orchestrator(tmp_path, engines, is_cancelled=cancel),
    )

    assert workflow._thread is not None
    workflow._thread.join(timeout=3)
    assert not workflow._thread.is_alive()
    assert workflow._status is WorkflowStatus.COMPLETED
    final = json.loads((tmp_path / "ocr-state" / "batch-final.json").read_text())
    assert final["run_config"] == {
        "pages": [1],
        "dpi": 320,
        "languages": ["en-US", "zh-Hans"],
        "sample_rate": 0.2,
    }
    assert engines.calls == ["vision:1", "codex:1", "adjudicate:1"]


def test_interrupted_state_for_changed_pdf_never_auto_resumes(tmp_path: Path):
    engines = FakeEngines()
    seeded = _orchestrator(tmp_path, engines)
    state = seeded._load_or_create_state(
        engines.pdf_path, [1], 300, ["zh-Hans"], 0.05, deadline=None
    )
    seeded._set_status(state, BatchStatus.RUNNING)
    engines.pdf_path.write_bytes(b"%PDF-1.7\nchanged immutable input\n")

    workflow = OcrWorkflow(
        source_path=engines.pdf_path,
        state_root=tmp_path / "ocr-state",
        orchestrator_factory=lambda _cancel: pytest.fail("changed PDF must not auto-resume"),
    )

    assert workflow._thread is None
    payload = workflow.status()
    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_interrupted"


@pytest.mark.parametrize(
    ("exception", "expected_status", "expected_error"),
    [
        (
            WorkflowBlockedError("provider /Users/private/codex API_KEY=secret unavailable"),
            "blocked",
            "ocr_provider_unavailable",
        ),
        (
            RuntimeError("provider /Users/private/codex API_KEY=secret crashed"),
            "failed",
            "ocr_workflow_failed",
        ),
    ],
)
def test_workflow_provider_exceptions_are_stable_redacted_codes(
    tmp_path: Path, monkeypatch, exception, expected_status, expected_error
):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.7\nprovider error fixture\n")
    monkeypatch.setattr(workflow_module, "count_pdf_pages", lambda _source: 1)

    def fail_factory(_cancel):
        raise exception

    workflow = OcrWorkflow(
        source_path=source,
        state_root=tmp_path / "state",
        orchestrator_factory=fail_factory,
    )
    workflow.start()
    assert workflow._thread is not None
    workflow._thread.join(timeout=2)

    payload = workflow.status()
    assert payload["status"] == expected_status
    assert payload["error"] == expected_error
    assert "/Users/" not in payload["error"]
    assert "secret" not in payload["error"]


def test_corrupted_final_is_stably_blocked_before_state_fallback(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "batch-final.json").write_text("{", encoding="utf-8")
    (state_root / "batch-state.json").write_text('{"status":"idle"}', encoding="utf-8")
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("corrupt work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"


def test_symlinked_final_is_rejected_as_invalid_ocr_evidence(tmp_path: Path):
    _engines, state_root, _final = _complete_workflow_fixture(tmp_path, publish_note=False)
    final_path = state_root / "batch-final.json"
    backing = state_root / "untrusted-final.json"
    final_path.replace(backing)
    final_path.symlink_to(backing)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("untrusted work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_evidence_invalid"


def test_symlinked_state_is_rejected_as_invalid_persisted_state(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    backing = state_root / "untrusted-state.json"
    backing.write_text('{"status":"failed","error":"unsafe"}', encoding="utf-8")
    (state_root / "batch-state.json").symlink_to(backing)
    workflow = OcrWorkflow(
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
        orchestrator_factory=lambda _cancel: pytest.fail("untrusted work must not rerun"),
    )

    payload = workflow.status()

    assert payload["status"] == "blocked"
    assert payload["error"] == "ocr_state_invalid"


@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_restored_workflow_status_preserves_known_enum_values(status: WorkflowStatus):
    restored = workflow_module.restored_workflow_status(
        {"status": status.value, "error": "ocr_engine_failed"}
    )

    assert restored == (status, "ocr_engine_failed")


@pytest.mark.parametrize("error", [{"detail": "secret"}, "x" * 241, "", "safe provider error"])
def test_restored_workflow_status_rejects_unsafe_error_values(error: object):
    restored = workflow_module.restored_workflow_status(
        {"status": WorkflowStatus.FAILED.value, "error": error}
    )

    assert restored == (WorkflowStatus.FAILED, None)


def test_build_confirmation_binds_selected_proposal_chapter():
    tree = {
        "schema_version": 1,
        "input_fingerprint": "input-1",
        "evidence_fingerprint": "evidence-1",
        "proposal_fingerprint": "proposal-1",
        "needs_confirmation": False,
        "chapters": [
            {
                "id": "chapter-1",
                "number": "1",
                "title": "战略管理",
                "level": 1,
                "toc_page": 1,
                "page_start": 1,
                "page_end": 3,
                "source_evidence": [],
                "confidence": 1.0,
                "children": [],
                "needs_confirmation": False,
                "warnings": [],
            }
        ],
        "warnings": [],
    }

    confirmation = build_confirmation(tree, "chapter-1")

    assert confirmation["action"] == "confirm"
    assert confirmation["chapter"]["title"] == "战略管理"
    assert confirmation["chapter_fingerprint"]


def test_build_confirmation_rejects_unknown_chapter():
    tree = {
        "schema_version": 1,
        "input_fingerprint": "input-1",
        "evidence_fingerprint": "evidence-1",
        "proposal_fingerprint": "proposal-1",
        "needs_confirmation": False,
        "chapters": [],
        "warnings": [],
    }
    with pytest.raises(ValueError, match="chapter not found"):
        build_confirmation(tree, "missing")
