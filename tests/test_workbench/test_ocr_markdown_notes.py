from __future__ import annotations

import json
import os
import stat

import pytest

from parsing_core.workbench.ocr.chapters import _chapter_fingerprint
from parsing_core.workbench.ocr.markdown_notes import (
    MarkdownNoteError,
    build_intensive_reading_note,
    persist_intensive_reading_note,
    validate_intensive_reading_note,
    validate_mermaid_block,
)


def _page(number: int, *texts: str) -> dict:
    blocks = []
    for index, text in enumerate(texts):
        blocks.append(
            {
                "id": f"p{number}-b{index}",
                "type": "paragraph",
                "text": text,
                "region": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
                "bounding_box": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.1},
                "confidence": 0.98,
                "reading_order": index + 1,
                "candidates": [],
                "uncertainty_reason": "",
                "table": None,
                "formula": None,
                "source_region": f"p{number}-r{index}",
            }
        )
    return {
        "page": number,
        "decision": {
            "payload": {
                "page": {"number": number, "width": 1200, "height": 1600},
                "final_blocks": blocks,
                "resolved_conflicts": [],
                "tables": [],
                "formulas": [],
                "decision_evidence": ["decision evidence"],
                "confidence": 0.98,
                "status": "accepted",
            }
        },
        "page_input_fingerprint": "book-input",
        "evidence_fingerprint": f"evidence-{number}",
    }


def _inputs() -> tuple[dict, dict, list[dict]]:
    chapter = {
        "id": "chapter-1",
        "number": "第一章",
        "title": "战略管理",
        "level": 1,
        "toc_page": 1,
        "page_start": 2,
        "page_end": 3,
        "source_evidence": [
            {
                "kind": "body",
                "page": 2,
                "block_id": "p2-b0",
                "evidence_fingerprint": "evidence-2",
                "excerpt": "第一章 战略管理",
            }
        ],
        "confidence": 0.95,
        "warnings": [],
        "needs_confirmation": False,
        "children": [],
    }
    tree = {
        "schema_version": 1,
        "input_fingerprint": "book-input",
        "evidence_fingerprint": "chapter-evidence",
        "proposal_fingerprint": "proposal",
        "chapters": [chapter],
        "warnings": [],
        "needs_confirmation": False,
    }
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": chapter["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": chapter,
        "chapter_fingerprint": _chapter_fingerprint(chapter),
    }
    return tree, confirmation, [
        _page(2, "第一章 战略管理", "战略是组织的长期方向。"),
        _page(3, "选择决定资源配置。"),
    ]


def test_builds_stable_note_with_citations_slots_and_previewable_mermaid():
    tree, confirmation, pages = _inputs()
    first = build_intensive_reading_note(
        tree,
        confirmation,
        pages,
        source_id="source-1",
        prompt_rules_version="mba-rules-v1",
    )
    second = build_intensive_reading_note(
        tree,
        confirmation,
        list(reversed(pages)),
        source_id="source-1",
        prompt_rules_version="mba-rules-v1",
    )

    assert first == second
    assert first["metadata"]["input_fingerprint"] == "book-input"
    assert first["metadata"]["chapter_fingerprint"] == confirmation["chapter_fingerprint"]
    assert "## 原文证据" in first["markdown"]
    assert "## 核心概念" in first["markdown"]
    assert "## 通俗、有趣、生活化的解释" in first["markdown"]
    assert "## 教材案例解读" in first["markdown"]
    assert "## 实际例子与问题解决" in first["markdown"]
    assert "## 实际应用" in first["markdown"]
    assert "待由 DeepSeek" in first["markdown"]
    assert first["markdown"].count("```mermaid\n") == 2
    assert "[src:source-1:p2:p2-b1]" in first["markdown"]
    validate_intensive_reading_note(first)


@pytest.mark.parametrize(
    "source",
    [
        "flowchart TD\n  A[\"开始\"] --> B[\"结束\"]",
        "graph LR\n  A[\"概念\"] --- B[\"应用\"]",
        "mindmap\n  root((主题))\n    概念",
    ],
)
def test_mermaid_whitelist_is_renderable(source):
    assert validate_mermaid_block(source) == source


@pytest.mark.parametrize(
    "source",
    [
        "sequenceDiagram\n  A->>B: hi",
        "flowchart TD\n  A[\"<script>alert(1)</script>\"]",
        "flowchart TD\n  A --> B\n```",
    ],
)
def test_mermaid_rejects_unsupported_or_dangerous_syntax(source):
    with pytest.raises(MarkdownNoteError):
        validate_mermaid_block(source)


def test_rejects_incomplete_or_unaccepted_ocr_and_wrong_chapter(tmp_path):
    tree, confirmation, pages = _inputs()
    pages[0]["decision"]["payload"]["status"] = "needs_review"
    with pytest.raises(MarkdownNoteError, match="accepted OCR"):
        build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")

    tree, confirmation, pages = _inputs()
    confirmation["chapter_id"] = "other"
    with pytest.raises(MarkdownNoteError, match="confirmation"):
        build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")

    target = tmp_path / "note.md"
    target.write_text("previous", encoding="utf-8")
    with pytest.raises(MarkdownNoteError):
        persist_intensive_reading_note(target, {"bad": True})
    assert target.read_text(encoding="utf-8") == "previous"


def test_rejects_foreign_page_input_fingerprint():
    tree, confirmation, pages = _inputs()
    pages[1]["page_input_fingerprint"] = "foreign-book"
    with pytest.raises(MarkdownNoteError, match="input fingerprint"):
        build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")


def test_persists_contract_atomically_and_rejects_symlink(tmp_path):
    tree, confirmation, pages = _inputs()
    note = build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")
    target = tmp_path / "note.json"
    persist_intensive_reading_note(target, note)
    assert json.loads(target.read_text(encoding="utf-8")) == note
    markdown_target = tmp_path / "note.md"
    persist_intensive_reading_note(markdown_target, note)
    assert markdown_target.read_text(encoding="utf-8") == note["markdown"]

    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(MarkdownNoteError, match="target"):
        persist_intensive_reading_note(link, note)
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_persist_rejects_unsafe_parent_and_hardlinked_target(tmp_path):
    tree, confirmation, pages = _inputs()
    note = build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")

    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MarkdownNoteError, match="parent"):
        persist_intensive_reading_note(parent_link / "note.md", note)

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(MarkdownNoteError, match="parent"):
        persist_intensive_reading_note(fifo / "note.md", note)

    target = tmp_path / "hardlinked.md"
    target.write_text("outside", encoding="utf-8")
    alias = tmp_path / "alias.md"
    os.link(target, alias)
    assert stat.S_ISREG(alias.stat().st_mode)
    with pytest.raises(MarkdownNoteError, match="target"):
        persist_intensive_reading_note(target, note)
    assert target.read_text(encoding="utf-8") == "outside"


def test_persist_replaces_using_the_open_directory_fd(tmp_path, monkeypatch):
    tree, confirmation, pages = _inputs()
    note = build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")
    target = tmp_path / "note.md"
    calls = []
    original_replace = os.replace

    def record_replace(source, destination, **kwargs):
        calls.append((source, destination, kwargs))
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", record_replace)
    persist_intensive_reading_note(target, note)
    assert calls
    assert calls[0][1] == target.name
    assert calls[0][2]["src_dir_fd"] == calls[0][2]["dst_dir_fd"]


def test_validation_rejects_tampered_metadata_or_fences():
    tree, confirmation, pages = _inputs()
    note = build_intensive_reading_note(tree, confirmation, pages, source_id="source-1")
    note["markdown"] = note["markdown"].replace("```mermaid", "```python", 1)
    with pytest.raises(MarkdownNoteError):
        validate_intensive_reading_note(note)
