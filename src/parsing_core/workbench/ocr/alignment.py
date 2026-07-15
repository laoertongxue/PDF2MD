from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opencc import OpenCC

from .baidu import (
    BaiduEscalationAuthorization,
    BaiduEscalationReason,
)
from .models import OcrObservation


class AlignmentDecision(StrEnum):
    CONSISTENT = "consistent"
    CONFLICT = "conflict"
    COMPLEX = "complex"


@dataclass(frozen=True)
class AlignmentConflict:
    reason: str
    region: dict[str, float]
    apple_text: str
    codex_text: str
    apple_block_id: str
    codex_block_id: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class AlignmentResult:
    status: AlignmentDecision
    conflicts: tuple[AlignmentConflict, ...]
    matched_blocks: tuple[tuple[str, str], ...]


_PUNCTUATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "［": "[",
        "］": "]",
        "％": "%",
        "＋": "+",
        "－": "-",
        "＝": "=",
        "＜": "<",
        "＞": ">",
        "／": "/",
    }
)
_NUMBER_RE = re.compile(r"(?:\d+(?:[.,]\d+)*%?|%\d+)")
_FORMULA_OPERATOR_RE = re.compile(r"(?:<=|>=|!=|==|=|<|>|\+|-|\*|/|\^)")

_OPENCC = OpenCC("t2s")


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("OCR block text must be a string")
    value = text.translate(_PUNCTUATION)
    value = _OPENCC.convert(value)
    return " ".join(value.split())


def compare_observations(apple: Any, codex: Any) -> AlignmentResult:
    left = _blocks(apple)
    right = _blocks(codex)
    unmatched_right = set(range(len(right)))
    matched: list[tuple[int, int]] = []
    conflicts: list[AlignmentConflict] = []

    for left_index, left_block in enumerate(left):
        candidate = _best_match(left_block, right, unmatched_right)
        if candidate is None:
            conflicts.append(_conflict("missing_block", left_block, None, apple, codex))
            continue
        unmatched_right.remove(candidate)
        matched.append((left_index, candidate))
        right_block = right[candidate]
        text_reason = _text_conflict_reason(left_block, right_block)
        if text_reason:
            conflicts.append(_conflict(text_reason, left_block, right_block, apple, codex))
        if left_block.get("type") == "formula" and right_block.get("type") == "formula":
            if _formula_value(left_block) != _formula_value(right_block):
                conflicts.append(
                    _conflict("formula_conflict", left_block, right_block, apple, codex)
                )
        if left_block.get("type") == "table" and right_block.get("type") == "table":
            if _table_shape(left_block.get("table")) != _table_shape(right_block.get("table")):
                conflicts.append(
                    _conflict("table_shape_conflict", left_block, right_block, apple, codex)
                )
            elif _table_value(left_block) != _table_value(right_block):
                conflicts.append(
                    _conflict("table_content_conflict", left_block, right_block, apple, codex)
                )
        if left_block.get("type") == "page_number" and right_block.get("type") == "page_number":
            if normalize_text(left_block.get("text", "")) != normalize_text(
                right_block.get("text", "")
            ):
                conflicts.append(
                    _conflict("page_number_conflict", left_block, right_block, apple, codex)
                )
        if left_block.get("type") == "footnote" and right_block.get("type") == "footnote":
            if normalize_text(left_block.get("text", "")) != normalize_text(
                right_block.get("text", "")
            ):
                conflicts.append(
                    _conflict("footnote_conflict", left_block, right_block, apple, codex)
                )

    for right_index in sorted(unmatched_right):
        conflicts.append(_conflict("missing_block", None, right[right_index], apple, codex))

    left_order = [left[i].get("reading_order", i) for i, _ in matched]
    right_order = [right[j].get("reading_order", j) for _, j in matched]
    if sorted(left_order) != sorted(right_order) or any(
        left_order[index] != right_order[index] for index in range(len(matched))
    ):
        first_left, first_right = left[matched[0][0]], right[matched[0][1]]
        conflicts.append(_conflict("reading_order_conflict", first_left, first_right, apple, codex))

    return AlignmentResult(
        status=AlignmentDecision.CONFLICT if conflicts else AlignmentDecision.CONSISTENT,
        conflicts=tuple(conflicts),
        matched_blocks=tuple(
            (left[i].get("id", str(i)), right[j].get("id", str(j))) for i, j in matched
        ),
    )


def classify_page(apple: Any, codex: Any) -> AlignmentDecision:
    result = compare_observations(apple, codex)
    blocks = _blocks(apple) + _blocks(codex)
    if any(block.get("type") in {"table", "formula", "image", "list"} for block in blocks):
        return AlignmentDecision.COMPLEX
    if _uncertain_items(apple) or _uncertain_items(codex):
        return AlignmentDecision.COMPLEX
    return result.status


def needs_baidu(
    page_hash: str, page: int, status: str | AlignmentDecision, *, sample_rate: float = 0.05
) -> bool:
    if not isinstance(page_hash, str) or not page_hash:
        raise ValueError("page hash is required")
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be positive")
    if not math.isfinite(sample_rate) or not 0 <= sample_rate <= 1:
        raise ValueError("sample rate must be between 0 and 1")
    status_value = status.value if isinstance(status, AlignmentDecision) else str(status)
    if status_value in {AlignmentDecision.CONFLICT.value, AlignmentDecision.COMPLEX.value}:
        return True
    bucket = int.from_bytes(hashlib.sha256(f"{page_hash}:{page}".encode()).digest()[:8], "big")
    return bucket % 10_000 < int(sample_rate * 10_000)


def authorize_baidu_escalation(
    page_hash: str,
    page: int,
    status: str | AlignmentDecision,
    *,
    input_fingerprint: str,
    sample_rate: float = 0.05,
) -> BaiduEscalationAuthorization | None:
    status_value = status.value if isinstance(status, AlignmentDecision) else str(status)
    if status_value == AlignmentDecision.CONFLICT.value:
        return BaiduEscalationAuthorization._from_alignment(
            BaiduEscalationReason.CONFLICT,
            page_hash=page_hash,
            input_fingerprint=input_fingerprint,
            alignment_status=status_value,
            page=page,
        )
    if status_value == AlignmentDecision.COMPLEX.value:
        return BaiduEscalationAuthorization._from_alignment(
            BaiduEscalationReason.COMPLEX,
            page_hash=page_hash,
            input_fingerprint=input_fingerprint,
            alignment_status=status_value,
            page=page,
        )
    if needs_baidu(page_hash, page, status_value, sample_rate=sample_rate):
        return BaiduEscalationAuthorization._from_alignment(
            BaiduEscalationReason.SAMPLE,
            page_hash=page_hash,
            input_fingerprint=input_fingerprint,
            alignment_status=status_value,
            page=page,
        )
    return None


def _blocks(observation: Any) -> list[dict[str, Any]]:
    if isinstance(observation, OcrObservation):
        payload = json.loads(observation.payload_json)
    elif isinstance(observation, str):
        payload = json.loads(observation)
    else:
        payload = observation
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise ValueError("OCR observation has invalid blocks")
    return [item for item in payload["blocks"] if isinstance(item, dict)]


def _uncertain_items(observation: Any) -> list[Any]:
    if isinstance(observation, OcrObservation):
        payload = json.loads(observation.payload_json)
    elif isinstance(observation, str):
        payload = json.loads(observation)
    else:
        payload = observation
    return payload.get("uncertain_items", []) if isinstance(payload, dict) else []


def _best_match(
    block: dict[str, Any], candidates: list[dict[str, Any]], available: set[int]
) -> int | None:
    bbox = _bbox(block)
    scored = []
    for index in available:
        score = _iou(bbox, _bbox(candidates[index]))
        if score >= 0.20:
            scored.append((score, -index, index))
    if not scored:
        return None
    return max(scored)[2]


def _text_conflict_reason(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    left_text = normalize_text(left.get("text", "")).replace(" ", "")
    right_text = normalize_text(right.get("text", "")).replace(" ", "")
    if left_text == right_text:
        return None
    if _NUMBER_RE.findall(left_text) != _NUMBER_RE.findall(right_text):
        return "numeric_conflict"
    if _FORMULA_OPERATOR_RE.findall(left_text) != _FORMULA_OPERATOR_RE.findall(right_text):
        return "formula_operator_conflict"
    return "text_conflict"


def _conflict(
    reason: str,
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    apple: Any,
    codex: Any,
) -> AlignmentConflict:
    source = left or right or {}
    return AlignmentConflict(
        reason=reason,
        region=_bbox(source),
        apple_text=(left or {}).get("text", ""),
        codex_text=(right or {}).get("text", ""),
        apple_block_id=(left or {}).get("id", ""),
        codex_block_id=(right or {}).get("id", ""),
        evidence=_evidence(apple, codex, left, right),
    )


def _metadata(observation: Any) -> dict[str, Any]:
    if isinstance(observation, OcrObservation):
        return {
            "id": observation.id,
            "engine": observation.engine,
            "input_fingerprint": observation.input_hash,
        }
    if isinstance(observation, str):
        try:
            observation = json.loads(observation)
        except json.JSONDecodeError:
            return {}
    return observation if isinstance(observation, dict) else {}


def _evidence(
    apple: Any,
    codex: Any,
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    apple_meta, codex_meta = _metadata(apple), _metadata(codex)
    return {
        "candidate_text": {
            "apple": (left or {}).get("text", ""),
            "codex": (right or {}).get("text", ""),
        },
        "candidate_structures": {"apple": left or {}, "codex": right or {}},
        "confidence": {
            "apple": (left or {}).get("confidence"),
            "codex": (right or {}).get("confidence"),
        },
        "observation_ids": {"apple": apple_meta.get("id", ""), "codex": codex_meta.get("id", "")},
        "input_fingerprints": {
            "apple": apple_meta.get("input_fingerprint", ""),
            "codex": codex_meta.get("input_fingerprint", ""),
        },
        "page_fingerprints": {
            "apple": apple_meta.get("page_fingerprint") or apple_meta.get("input_fingerprint", ""),
            "codex": codex_meta.get("page_fingerprint") or codex_meta.get("input_fingerprint", ""),
        },
        "baidu_observation_refs": list(
            dict.fromkeys(
                (apple_meta.get("baidu_observation_refs") or [])
                + (codex_meta.get("baidu_observation_refs") or [])
            )
        ),
    }


def _bbox(block: dict[str, Any]) -> dict[str, float]:
    value = block.get("bounding_box") or block.get("region") or {}
    return {key: float(value.get(key, 0)) for key in ("x", "y", "width", "height")}


def _iou(left: dict[str, float], right: dict[str, float]) -> float:
    x1, y1 = max(left["x"], right["x"]), max(left["y"], right["y"])
    x2 = min(left["x"] + left["width"], right["x"] + right["width"])
    y2 = min(left["y"] + left["height"], right["y"] + right["height"])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union else 0.0


def _table_shape(table: Any) -> tuple[int, int] | None:
    if not isinstance(table, dict) or not isinstance(table.get("matrix"), list):
        return None
    rows = table["matrix"]
    return len(rows), max((len(row) for row in rows if isinstance(row, list)), default=0)


def _formula_value(block: dict[str, Any]) -> str:
    formula = block.get("formula")
    if not isinstance(formula, dict):
        return ""
    return normalize_text(str(formula.get("latex", ""))).replace(" ", "")


def _table_value(block: dict[str, Any]) -> str:
    table = block.get("table")
    if not isinstance(table, dict):
        return ""
    value = {key: table.get(key) for key in ("matrix", "cells") if key in table}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
