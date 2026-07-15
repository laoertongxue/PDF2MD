import json

import pytest

from parsing_core.workbench.ocr.chapters import (
    ChapterConfirmationError,
    detect_chapter_tree,
    load_chapter_confirmation,
    persist_chapter_confirmation,
    validate_chapter_confirmation,
)


def _page(number, *lines, evidence=None, fingerprint=None):
    blocks = []
    for index, text in enumerate(lines):
        blocks.append(
            {
                "id": f"p{number}-b{index}",
                "type": "paragraph",
                "text": text,
                "region": {"x": 0.1, "y": 0.1 + index * 0.1, "width": 0.8, "height": 0.08},
                "bounding_box": {"x": 0.1, "y": 0.1 + index * 0.1, "width": 0.8, "height": 0.08},
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
                "decision_evidence": ["ocr evidence"],
                "confidence": 0.98,
                "status": "accepted",
            }
        },
        "page_input_fingerprint": fingerprint or f"page-input-{number}",
        "evidence_fingerprint": evidence or f"evidence-{number}",
    }


def test_detects_multilevel_toc_and_maps_body_ranges_deterministically():
    pages = [
        _page(1, "目录", "第一章 战略管理 ........ 5", "1.1 战略的定义 ........ 7"),
        _page(2, "前言", "本书介绍管理问题。"),
        _page(3, "第一章 战略管理", "战略是组织的长期方向。"),
        _page(4, "1.1 战略的定义", "战略回答组织去哪里。"),
        _page(5, "第二章 外部环境", "环境影响组织选择。"),
    ]

    first = detect_chapter_tree(pages, input_fingerprint="book-input")
    second = detect_chapter_tree(list(reversed(pages)), input_fingerprint="book-input")

    assert first == second
    assert [node["number"] for node in first["chapters"]] == ["第一章", "第二章"]
    assert first["chapters"][0]["page_start"] == 3
    assert first["chapters"][0]["page_end"] == 4
    assert first["chapters"][0]["children"][0]["page_start"] == 4
    assert first["chapters"][0]["children"][0]["page_end"] == 4
    assert first["chapters"][0]["children"][0]["number"] == "1.1"
    assert first["chapters"][0]["needs_confirmation"] is False
    assert first["chapters"][0]["source_evidence"]


def test_marks_missing_toc_as_confirmation_required():
    tree = detect_chapter_tree(
        [_page(1, "第一章 战略管理", "正文"), _page(2, "第二章 外部环境", "正文")],
        input_fingerprint="book-input",
    )

    assert tree["warnings"] == ["目录页未识别，章节边界来自正文标题"]
    assert tree["needs_confirmation"] is True
    assert all(chapter["needs_confirmation"] for chapter in tree["chapters"])


def test_supports_english_chapter_numbers_and_toc_page_mapping():
    tree = detect_chapter_tree(
        [
            _page(1, "Contents", "Chapter 1 Foundations ........ 3", "1.1 Scope ........ 4"),
            _page(2, "Chapter 1 Foundations", "The foundation."),
            _page(3, "1.1 Scope", "The scope."),
        ],
        input_fingerprint="book-input",
    )

    assert tree["chapters"][0]["number"] == "Chapter 1"
    assert tree["chapters"][0]["toc_page"] == 3
    assert tree["chapters"][0]["page_start"] == 2
    assert tree["chapters"][0]["children"][0]["number"] == "1.1"
    assert tree["chapters"][0]["children"][0]["toc_page"] == 4


def test_marks_page_conflict_and_duplicate_title_without_silent_choice():
    pages = [
        _page(1, "目录", "第一章 组织行为 ........ 4", "第一章 组织行为 ........ 8"),
        _page(2, "第一章 组织行为", "正文"),
        _page(3, "第一章 组织行为", "另一处正文"),
    ]
    tree = detect_chapter_tree(pages, input_fingerprint="book-input")

    assert tree["needs_confirmation"] is True
    assert "目录页码冲突" in tree["warnings"]
    assert "正文标题重复" in tree["warnings"]
    assert all(chapter["needs_confirmation"] for chapter in tree["chapters"])


def test_confirmation_is_versioned_and_bound_to_current_evidence(tmp_path):
    tree = detect_chapter_tree(
        [_page(1, "目录", "第一章 战略管理 ........ 2"), _page(2, "第一章 战略管理", "正文")],
        input_fingerprint="book-input",
    )
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": tree["chapters"][0],
    }
    target = tmp_path / "confirmation.json"
    persist_chapter_confirmation(target, confirmation)
    assert load_chapter_confirmation(target) == confirmation
    validate_chapter_confirmation(confirmation, tree)

    changed = dict(tree)
    changed["evidence_fingerprint"] = "changed"
    with pytest.raises(ChapterConfirmationError, match="evidence"):
        validate_chapter_confirmation(confirmation, changed)


def test_confirmation_rejects_unknown_fields_and_symlink_target(tmp_path):
    tree = detect_chapter_tree([_page(1, "第一章 标题", "正文")], input_fingerprint="book")
    confirmation = {
        "schema_version": 1,
        "revision": 1,
        "action": "reject",
        "chapter_id": tree["chapters"][0]["id"],
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": None,
        "extra": "must fail",
    }
    with pytest.raises(ChapterConfirmationError):
        validate_chapter_confirmation(confirmation, tree)

    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({key: value for key, value in confirmation.items() if key != "extra"}),
        encoding="utf-8",
    )
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(ChapterConfirmationError, match="target"):
        persist_chapter_confirmation(
            link, {key: value for key, value in confirmation.items() if key != "extra"}
        )
