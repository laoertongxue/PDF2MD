from pathlib import Path

import pytest

from parsing_core.workbench.ocr.workflow import (
    OcrWorkflow,
    WorkflowStatus,
    build_confirmation,
    status_payload,
)


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

    assert payload["status"] == "completed"
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
