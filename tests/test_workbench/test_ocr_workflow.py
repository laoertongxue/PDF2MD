import json
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
    pages = workflow_module._normalized_completed_pages(final)
    tree = detect_chapter_tree(pages, input_fingerprint=final["input_fingerprint"])
    confirmation = build_confirmation(tree, tree["chapters"][0]["id"])
    (state_root / "chapter-tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (state_root / "chapter-confirmation.json").write_text(
        json.dumps(confirmation), encoding="utf-8"
    )
    markdown = (
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
                "概念内容",
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
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
    bind_published_note(
        state_root / "batch-final.json",
        state_root / "intensive-reading.md",
        {
            "model": "deepseek-v4-pro",
            "prompt_rules_version": "mba-intensive-reading-v1",
            "chapter_id": confirmation["chapter_id"],
            "chapter_fingerprint": confirmation["chapter_fingerprint"],
            "prompt_fingerprint": "prompt-fingerprint",
            "input_fingerprint": final["input_fingerprint"],
            "evidence_fingerprint": tree["evidence_fingerprint"],
        },
        expected_final=final,
        expected_tree=tree,
        confirmation=confirmation,
    )
    return engines, state_root, final


def _published_note_metadata(state_root: Path, final: dict[str, object]) -> dict[str, object]:
    tree = json.loads((state_root / "chapter-tree.json").read_text(encoding="utf-8"))
    confirmation = json.loads(
        (state_root / "chapter-confirmation.json").read_text(encoding="utf-8")
    )
    return {
        "model": "deepseek-v4-pro",
        "prompt_rules_version": "mba-intensive-reading-v1",
        "chapter_id": confirmation["chapter_id"],
        "prompt_fingerprint": "prompt-fingerprint",
        "input_fingerprint": final["input_fingerprint"],
        "chapter_fingerprint": confirmation["chapter_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
    }


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
        note = state_root / "intensive-reading.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("概念内容", "被篡改内容"),
            encoding="utf-8",
        )
    elif mutation == "chapter":
        publication["chapter_fingerprint"] = "foreign-chapter"
    elif mutation == "model":
        publication["model"] = "other-model"
    elif mutation == "ruleset":
        publication["ruleset"] = "other-ruleset"
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
    note = state_root / "intensive-reading.md"
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
    note_path = state_root / "intensive-reading.md"
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
    note_path = state_root / "intensive-reading.md"
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
            state_root / "intensive-reading.md",
            metadata,
            expected_final=final,
        )


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
