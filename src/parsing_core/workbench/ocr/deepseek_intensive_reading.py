"""Unattended DeepSeek generation for an accepted intensive-reading note."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from parsing_core.workbench.deepseek import MODEL_NAME, DeepSeekError

from .markdown_notes import (
    DEFAULT_PROMPT_RULES_VERSION,
    SECTION_ORDER,
    _digest,
    _render_markdown,
    _require_accepted_note,
    persist_intensive_reading_note,
    validate_intensive_reading_note,
    validate_mermaid_block,
)

RULESET = """你正在生成 MBA 教材章节的精读笔记。
必须遵守：
1. 概念必须准确，并提供“概念通俗、有趣、生活化”的解释，不能只换同义词。
2. 必须解读教材中的案例；不得凭空声称教材没有提供的事实。
3. 必须给出一个实际例子，逐步说明如何解决问题。
4. 必须说明实际应用、适用边界、风险和下一步行动建议。
5. 原文证据章节必须逐字保留，其他栏目必须引用提供的 [src:...] 证据标记。
6. 必须输出两个可直接预览的 Mermaid 图：知识结构图和应用流程图。
7. 只返回 JSON，不要 Markdown 围栏、解释文字或额外字段；不要改写、补造或删除证据。
"""
MAX_PROMPT_BYTES = 1_500_000


class DeepSeekGenerationError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.status = status


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def build_generation_prompt(note: Mapping[str, Any]) -> str:
    metadata = note.get("metadata")
    sections = note.get("sections")
    mermaid = note.get("mermaid")
    if not isinstance(metadata, Mapping):
        raise DeepSeekGenerationError("intensive-reading input is invalid")
    if not isinstance(sections, list) or not isinstance(mermaid, list):
        raise DeepSeekGenerationError("intensive-reading input is invalid")
    source = {
        "chapter": {
            "chapter_id": metadata.get("chapter_id"),
            "page_start": metadata.get("page_start"),
            "page_end": metadata.get("page_end"),
        },
        "metadata": {key: value for key, value in metadata.items() if key != "note_fingerprint"},
        "sections": sections,
        "mermaid": mermaid,
    }
    prompt = (
        RULESET
        + "\n输出结构必须保持以下 section 顺序、key/title 和 Mermaid key/type 不变。"
        + "\n精读栏目必须严格按此顺序输出："
        + "、".join(key for key, _title in SECTION_ORDER)
        + "\n输入证据如下：\n"
        + json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise DeepSeekGenerationError("intensive-reading prompt exceeds limit")
    return prompt


class DeepSeekIntensiveReadingGenerator:
    def __init__(self, client: Any, *, timeout: int = 180, retries: int = 2):
        self.client = client
        self.timeout = timeout
        self.retries = retries

    def generate(
        self,
        note: Mapping[str, Any],
        *,
        output_path: str | Path | None = None,
        cancel_event: threading.Event | None = None,
        prompt_rules_version: str = DEFAULT_PROMPT_RULES_VERSION,
    ) -> dict[str, Any]:
        if getattr(self.client, "model", None) != MODEL_NAME:
            raise DeepSeekGenerationError(f"only {MODEL_NAME} is supported")
        try:
            _require_accepted_note(note)
            validate_intensive_reading_note(note)
        except Exception as exc:
            raise DeepSeekGenerationError(
                "intensive-reading input is not an accepted OCR note"
            ) from exc
        if cancel_event is not None and cancel_event.is_set():
            raise DeepSeekGenerationError(
                "intensive-reading generation cancelled",
                status="cancelled",
            )
        try:
            base = json.loads(json.dumps(note, ensure_ascii=False))
            prompt = build_generation_prompt(base)
            raw = self.client.complete(
                prompt,
                timeout=self.timeout,
                max_tokens=8192,
                cancel_event=cancel_event,
                retries=self.retries,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise DeepSeekGenerationError(
                    "intensive-reading generation cancelled",
                    status="cancelled",
                )
            generated = json.loads(raw)
            result = _finalize_generated_note(base, generated, prompt, prompt_rules_version)
            if output_path is not None:
                persist_intensive_reading_note(output_path, result)
            return result
        except DeepSeekGenerationError:
            raise
        except DeepSeekError as exc:
            if "cancelled" in str(exc):
                raise DeepSeekGenerationError(
                    "intensive-reading generation cancelled", status="cancelled"
                ) from exc
            raise DeepSeekGenerationError("intensive-reading generation failed") from exc
        except Exception as exc:
            raise DeepSeekGenerationError("intensive-reading generation failed") from exc


def _finalize_generated_note(
    base: Mapping[str, Any], generated: Any, prompt: str, prompt_rules_version: str
) -> dict[str, Any]:
    if not isinstance(generated, dict):
        raise DeepSeekGenerationError("generated note is not an object")
    if set(generated) != {"schema_version", "metadata", "sections", "mermaid"}:
        raise DeepSeekGenerationError("generated note has unexpected fields")
    if generated.get("schema_version") != 1:
        raise DeepSeekGenerationError("generated note schema version is invalid")
    base_metadata = base.get("metadata")
    metadata = generated.get("metadata")
    if not isinstance(base_metadata, Mapping) or not isinstance(metadata, dict):
        raise DeepSeekGenerationError("generated note metadata is invalid")
    allowed_metadata = {
        "input_fingerprint",
        "chapter_fingerprint",
        "evidence_fingerprint",
        "prompt_rules_version",
        "source_id",
        "chapter_id",
        "chapter_number",
        "chapter_title",
        "page_start",
        "page_end",
        "citation_ids",
        "model",
        "prompt_fingerprint",
    }
    if set(metadata) - allowed_metadata:
        raise DeepSeekGenerationError("generated note metadata has unexpected fields")
    for key, value in base_metadata.items():
        if key != "note_fingerprint" and metadata.get(key) != value:
            raise DeepSeekGenerationError("generated note metadata is not bound to input")
    if metadata.get("model") != MODEL_NAME or metadata.get(
        "prompt_fingerprint"
    ) != prompt_fingerprint(prompt):
        raise DeepSeekGenerationError("generated note model or prompt fingerprint is invalid")
    for key in (
        "input_fingerprint",
        "chapter_fingerprint",
        "evidence_fingerprint",
        "source_id",
        "chapter_id",
    ):
        if metadata.get(key) != base_metadata.get(key):
            raise DeepSeekGenerationError("generated note evidence fingerprint is invalid")
    if metadata.get("prompt_rules_version") != prompt_rules_version:
        raise DeepSeekGenerationError("generated note ruleset is invalid")
    sections = generated.get("sections")
    base_sections = base.get("sections")
    if (
        not isinstance(sections, list)
        or not isinstance(base_sections, list)
        or len(sections) != len(SECTION_ORDER)
    ):
        raise DeepSeekGenerationError("generated note sections are incomplete")
    expected_by_key = {item[0]: item for item in SECTION_ORDER}
    expected_section_keys = [item[0] for item in SECTION_ORDER]
    actual_section_keys = [
        section.get("key") if isinstance(section, Mapping) else None
        for section in sections
    ]
    if actual_section_keys != expected_section_keys:
        raise DeepSeekGenerationError("generated note sections are out of order")
    base_source = next(
        (item for item in base_sections if item.get("key") == "source_evidence"), None
    )
    source = next((item for item in sections if item.get("key") == "source_evidence"), None)
    if (
        not isinstance(base_source, Mapping)
        or not isinstance(source, dict)
        or source.get("content") != base_source.get("content")
        or source.get("source_refs") != base_source.get("source_refs")
    ):
        raise DeepSeekGenerationError("original evidence was rewritten")
    citation_ids = base_metadata.get("citation_ids")
    if not isinstance(citation_ids, list) or not citation_ids:
        raise DeepSeekGenerationError("source evidence citations are missing")
    for section in sections:
        if not isinstance(section, dict):
            raise DeepSeekGenerationError("generated note section is invalid")
        if set(section) != {"key", "title", "content", "source_refs"}:
            raise DeepSeekGenerationError("generated note section is invalid")
        if section.get("key") not in expected_by_key:
            raise DeepSeekGenerationError("generated note section is invalid")
        key = section["key"]
        if (
            section.get("title") != expected_by_key[key][1]
            or not isinstance(section.get("content"), str)
            or not section["content"].strip()
        ):
            raise DeepSeekGenerationError("generated note section is incomplete")
        refs = section.get("source_refs")
        if not isinstance(refs, list) or not refs or any(ref not in citation_ids for ref in refs):
            raise DeepSeekGenerationError("generated note section lacks evidence citations")
    mermaid = generated.get("mermaid")
    base_mermaid = base.get("mermaid")
    if (
        not isinstance(mermaid, list)
        or not isinstance(base_mermaid, list)
        or len(mermaid) != len(base_mermaid)
        or len(mermaid) != 2
    ):
        raise DeepSeekGenerationError("generated Mermaid diagrams are incomplete")
    for diagram, expected in zip(mermaid, base_mermaid, strict=True):
        if not isinstance(diagram, dict):
            raise DeepSeekGenerationError("generated Mermaid diagram is invalid")
        if set(diagram) != {"key", "title", "type", "source"}:
            raise DeepSeekGenerationError("generated Mermaid diagram is invalid")
        if any(diagram.get(field) != expected.get(field) for field in ("key", "title", "type")):
            raise DeepSeekGenerationError("generated Mermaid contract was changed")
        try:
            validate_mermaid_block(diagram["source"], expected_type=diagram["type"])
        except Exception as exc:
            raise DeepSeekGenerationError("generated Mermaid diagram is invalid") from exc
    metadata = dict(metadata)
    metadata.pop("note_fingerprint", None)
    metadata["page_start"] = base_metadata.get("page_start")
    metadata["page_end"] = base_metadata.get("page_end")
    metadata["citation_ids"] = list(citation_ids)
    metadata["note_fingerprint"] = _digest(
        {"metadata": metadata, "sections": sections, "mermaid": mermaid}
    )
    chapter = {
        "number": metadata.get("chapter_number", ""),
        "title": metadata.get("chapter_title", metadata["chapter_id"]),
    }
    result = {
        "schema_version": 1,
        "metadata": metadata,
        "sections": sections,
        "mermaid": mermaid,
        "markdown": _render_markdown(chapter, metadata, sections, mermaid),
    }
    try:
        validate_intensive_reading_note(result)
    except Exception as exc:
        raise DeepSeekGenerationError("generated note failed publication validation") from exc
    return result
