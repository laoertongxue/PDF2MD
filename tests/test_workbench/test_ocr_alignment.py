import pytest

from parsing_core.workbench.ocr.alignment import (
    AlignmentDecision,
    authorize_baidu_escalation,
    classify_page,
    compare_observations,
    needs_baidu,
    normalize_text,
)
from parsing_core.workbench.ocr.baidu import BaiduEscalationAuthorization


def block(
    text, *, block_type="paragraph", x=0.1, y=0.1, width=0.4, height=0.1, confidence=0.9, **extra
):
    value = {
        "id": extra.pop("id", "b1"),
        "type": block_type,
        "text": text,
        "region": {"x": x, "y": y, "width": width, "height": height},
        "bounding_box": {"x": x, "y": y, "width": width, "height": height},
        "confidence": confidence,
        "reading_order": extra.pop("reading_order", 1),
        "candidates": extra.pop("candidates", []),
        "uncertainty_reason": extra.pop("uncertainty_reason", ""),
        "table": extra.pop("table", None),
        "formula": extra.pop("formula", None),
        "source_region": extra.pop("source_region", "r1"),
    }
    value.update(extra)
    return value


def observation(
    blocks, *, observation_id="obs-1", engine="apple_vision", input_fingerprint="page-sha"
):
    return {
        "id": observation_id,
        "engine": engine,
        "input_fingerprint": input_fingerprint,
        "page": {"number": 1, "width": 1200, "height": 1600},
        "blocks": blocks,
    }


@pytest.mark.parametrize(
    ("apple", "codex", "reason"),
    [
        ("利润为 10%", "利润为10％", None),
        ("學習管理", "学习管理", None),
        ("經濟管理與決策", "经济管理与决策", None),
        ("利润为 10%", "利润为 40%", "numeric_conflict"),
        ("x <= 3", "x >= 3", "formula_operator_conflict"),
    ],
)
def test_compare_observations_classifies_text_differences(apple, codex, reason):
    result = compare_observations(observation([block(apple)]), observation([block(codex)]))

    assert result.status == ("consistent" if reason is None else "conflict")
    assert [item.reason for item in result.conflicts] == ([] if reason is None else [reason])


def test_compare_observations_detects_missing_line_and_preserves_raw_text():
    result = compare_observations(
        observation([block("第一行", id="a"), block("第二行", id="b", y=0.3, reading_order=2)]),
        observation([block("第一行", id="c")]),
    )

    assert result.status == "conflict"
    assert result.conflicts[0].reason == "missing_block"
    assert result.conflicts[0].apple_text == "第二行"


def test_compare_observations_detects_table_shape_and_multicolumn_order():
    left = observation(
        [
            block("左栏", id="left", x=0.1, y=0.1, reading_order=1),
            block("右栏", id="right", x=0.6, y=0.1, reading_order=2),
            block("表", id="table", block_type="table", table={"matrix": [["A", "B"]]}),
        ]
    )
    right = observation(
        [
            block("右栏", id="r-right", x=0.6, y=0.1, reading_order=1),
            block("左栏", id="r-left", x=0.1, y=0.1, reading_order=2),
            block("表", id="r-table", block_type="table", table={"matrix": [["A"]]}),
        ]
    )

    result = compare_observations(left, right)
    reasons = {item.reason for item in result.conflicts}
    assert "reading_order_conflict" in reasons
    assert "table_shape_conflict" in reasons


def test_compare_observations_detects_formula_latex_conflict():
    result = compare_observations(
        observation([block("x", block_type="formula", formula={"latex": "x+1"})]),
        observation(
            [block("x", block_type="formula", formula={"latex": "x-1"})], observation_id="codex"
        ),
    )

    assert {item.reason for item in result.conflicts} == {"formula_conflict"}


def test_compare_observations_detects_table_cell_content_conflict():
    result = compare_observations(
        observation([block("表", block_type="table", table={"matrix": [["收入", "10"]]})]),
        observation(
            [block("表", block_type="table", table={"matrix": [["收入", "40"]]})],
            observation_id="codex",
            engine="codex_vision",
        ),
    )

    assert "table_content_conflict" in {item.reason for item in result.conflicts}


def test_compare_observations_detects_structured_table_cells_conflict():
    result = compare_observations(
        observation([block("表", block_type="table", table={"cells": [{"row": 1, "text": "10"}]})]),
        observation(
            [block("表", block_type="table", table={"cells": [{"row": 1, "text": "40"}]})],
            observation_id="codex",
        ),
    )

    assert "table_content_conflict" in {item.reason for item in result.conflicts}


def test_conflict_contains_final_adjudication_evidence_package():
    result = compare_observations(
        observation([block("利润为 10%", confidence=0.7)], input_fingerprint="apple-page"),
        observation(
            [block("利润为 40%", confidence=0.8)],
            observation_id="codex-2",
            engine="codex_vision",
            input_fingerprint="codex-page",
        ),
    )

    conflict = result.conflicts[0]
    evidence = conflict.evidence
    assert evidence["candidate_text"] == {"apple": "利润为 10%", "codex": "利润为 40%"}
    assert evidence["candidate_structures"]["apple"]["type"] == "paragraph"
    assert evidence["confidence"] == {"apple": 0.7, "codex": 0.8}
    assert evidence["observation_ids"] == {"apple": "obs-1", "codex": "codex-2"}
    assert evidence["input_fingerprints"] == {"apple": "apple-page", "codex": "codex-page"}
    assert evidence["baidu_observation_refs"] == []


def test_matching_different_engine_ids_does_not_create_order_conflict():
    result = compare_observations(
        observation([block("左", id="apple-1", reading_order=1)]),
        observation([block("左", id="codex-9", reading_order=1)]),
    )

    assert result.status == AlignmentDecision.CONSISTENT


def test_page_classification_marks_complex_pages_for_upgrade():
    result = classify_page(
        observation([block("表", block_type="table", table={"matrix": [["A"]]})]),
        observation([block("表", block_type="table", table={"matrix": [["A"]]})]),
    )

    assert result == AlignmentDecision.COMPLEX


def test_baidu_sampling_is_stable_and_exactly_hash_based():
    selected = [
        page
        for page in range(1, 101)
        if needs_baidu("book-sha", page, "consistent", sample_rate=0.05)
    ]
    repeated = [
        page
        for page in range(1, 101)
        if needs_baidu("book-sha", page, "consistent", sample_rate=0.05)
    ]

    assert selected == repeated
    assert all(
        needs_baidu("book-sha", page, "consistent", sample_rate=0.05) is False or page in selected
        for page in range(1, 101)
    )


def test_upgrade_is_required_for_conflict_or_complex_but_not_consistent_unsampled():
    assert needs_baidu("book-sha", 1, "conflict", sample_rate=0) is True
    assert needs_baidu("book-sha", 1, "complex", sample_rate=0) is True
    assert needs_baidu("book-sha", 1, "consistent", sample_rate=0) is False


def test_baidu_authorization_is_typed_and_only_issued_for_upgrade_pages():
    authorization = authorize_baidu_escalation(
        "book-sha", 1, "conflict", input_fingerprint="input-sha", sample_rate=0
    )
    assert isinstance(authorization, BaiduEscalationAuthorization)
    assert isinstance(
        authorize_baidu_escalation(
            "book-sha", 1, "complex", input_fingerprint="input-sha", sample_rate=0
        ),
        BaiduEscalationAuthorization,
    )
    assert isinstance(
        authorize_baidu_escalation(
            "book-sha", 1, "consistent", input_fingerprint="input-sha", sample_rate=1
        ),
        BaiduEscalationAuthorization,
    )
    assert (
        authorize_baidu_escalation(
            "book-sha", 1, "consistent", input_fingerprint="input-sha", sample_rate=0
        )
        is None
    )


def test_normalization_does_not_hide_numbers_or_formula_operators():
    assert normalize_text("总成本：１０，０００ 元") == "总成本:１０,０００ 元"
    assert normalize_text("x <= 3") != normalize_text("x >= 3")
