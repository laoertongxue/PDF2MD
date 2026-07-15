import hashlib
import json
from pathlib import Path

import pytest
from test_ocr_orchestrator import FakeEngines, _orchestrator, _run

from parsing_core.workbench.ocr.workflow import (
    OcrWorkflow,
    WorkflowStatus,
    build_confirmation,
    status_payload,
)


def _complete_workflow_fixture(tmp_path: Path):
    engines = FakeEngines()
    orchestrator = _orchestrator(tmp_path, engines)
    assert _run(orchestrator, engines).status.value == "completed"
    state_root = tmp_path / "ocr-state"
    final = json.loads((state_root / "batch-final.json").read_text(encoding="utf-8"))
    page = final["pages"]["1"]
    markdown = "\n".join(
        [
            "# 1 战略管理",
            f"> 输入指纹：`{final['input_fingerprint']}`",
            "> 章节指纹：`chapter-fingerprint`",
            f"> OCR 证据指纹：`{page['evidence_fingerprint']}`",
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
    ) + "\n"
    (state_root / "intensive-reading.md").write_text(markdown, encoding="utf-8")
    final.update(
        {
            "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
            "model": "deepseek-v4-pro",
            "ruleset": "mba-intensive-reading-v1",
            "chapter_fingerprint": "chapter-fingerprint",
            "prompt_fingerprint": "prompt-fingerprint",
            "note_input_fingerprint": final["input_fingerprint"],
            "note_evidence_fingerprint": page["evidence_fingerprint"],
        }
    )
    (state_root / "batch-final.json").write_text(json.dumps(final), encoding="utf-8")
    return engines, state_root, final


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


@pytest.mark.parametrize(
    "mutation", ["batch", "page", "markdown", "fingerprint", "chapter", "model", "ruleset"]
)
def test_status_payload_blocks_incomplete_or_tampered_publication(tmp_path: Path, mutation: str):
    _engines, state_root, final = _complete_workflow_fixture(tmp_path)
    if mutation == "batch":
        final["status"] = "running"
    elif mutation == "page":
        del final["pages"]["1"]["decision"]
    elif mutation == "markdown":
        note = state_root / "intensive-reading.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("概念内容", "被篡改内容"),
            encoding="utf-8",
        )
    elif mutation == "fingerprint":
        final["input_fingerprint"] = "foreign-input"
    if mutation == "chapter":
        final["chapter_fingerprint"] = "foreign-chapter"
    elif mutation == "model":
        final["model"] = "other-model"
    elif mutation == "ruleset":
        final["ruleset"] = "other-ruleset"
    (state_root / "batch-final.json").write_text(json.dumps(final), encoding="utf-8")

    payload = status_payload(
        status=WorkflowStatus.COMPLETED,
        source_path=tmp_path / "book.pdf",
        state_root=state_root,
    )

    assert payload["status"] == "blocked"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None


def test_status_payload_does_not_report_unpublished_result_as_completed(tmp_path: Path):
    payload = status_payload(
        status=WorkflowStatus.RUNNING,
        source_path=tmp_path / "book.pdf",
        state_root=tmp_path / "state",
    )

    assert payload["status"] == "running"
    assert payload["publishable"] is False
    assert payload["markdown_path"] is None


def test_cold_start_does_not_publish_completed_batch_without_note(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "batch-final.json").write_text(
        '{"status":"completed","input_fingerprint":"input-1","pages":{}}',
        encoding="utf-8",
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
