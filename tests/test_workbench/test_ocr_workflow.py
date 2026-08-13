import hashlib
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_ocr_orchestrator import FakeEngines, _orchestrator, _run

from parsing_core.workbench.ocr import workflow as workflow_module
from parsing_core.workbench.ocr.chapters import detect_chapter_tree
from parsing_core.workbench.ocr.workflow import (
    OcrWorkflow,
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
    assert payload["markdown_path"] == str(state_root / "intensive-reading.md")


def test_legacy_status_rejects_final_replaced_after_publication_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path, publish_note=False)
    _pages, tree, confirmation = _prepare_chapter_context(state_root, final)
    markdown = _valid_markdown(final, tree, confirmation)
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
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
    original_status = workflow_module._legacy_publication_status
    replaced = False

    def validate_then_replace(expected_final: dict, paths: workflow_module.WorkflowPaths):
        nonlocal replaced
        result = original_status(expected_final, paths)
        if result[0] and not replaced:
            workflow_module._atomic_json(final_path, replacement)
            replaced = True
        return result

    monkeypatch.setattr(workflow_module, "_legacy_publication_status", validate_then_replace)
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
        lock_path.symlink_to(backing)
    elif attack == "hardlink":
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
    assert payload["status"] == "completed"
    assert payload["publishable"] is False
    assert payload["error"] == "ocr_publication_invalid"


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
        {"status": status.value, "error": "safe provider error"}
    )

    assert restored == (status, "safe provider error")


@pytest.mark.parametrize("error", [{"detail": "secret"}, "x" * 241, ""])
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
