"""Deterministic, previewable Markdown contracts for confirmed OCR chapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import weakref
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .chapters import validate_chapter_confirmation, validate_chapter_tree
from .codex_vision import CodexVisionError, validate_persisted_payload

NOTE_SCHEMA_VERSION = 1
DEFAULT_PROMPT_RULES_VERSION = "mba-intensive-reading-v1"
MERMAID_TYPES = frozenset({"flowchart", "graph", "mindmap"})
SECTION_ORDER = (
    ("source_evidence", "原文证据"),
    ("concepts", "核心概念"),
    ("plain_explain", "通俗、有趣、生活化的解释"),
    ("cases", "教材案例解读"),
    ("problem_solving", "实际例子与问题解决"),
    ("applications", "实际应用"),
)
_FENCE_RE = re.compile(r"^```mermaid\n(.*?)\n^```$", re.MULTILINE | re.DOTALL)
_DANGEROUS_RE = re.compile(
    r"(?is)<\s*/?\s*(?:script|iframe|object|embed|style|link)|javascript\s*:|on[a-z]+\s*=|%%\{"
)
_DIRECTION_RE = re.compile(r"^(?:TD|TB|LR|RL|BT)$")
_FLOW_LINE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]*(?:\[[^\r\n]*\]|\(\([^\r\n]*\)\))?"
    r"\s+(?:-->|---|-.->|==>|-\.->)\s+"
    r"[A-Za-z][A-Za-z0-9_-]*(?:\[[^\r\n]*\]|\(\([^\r\n]*\)\))?"
    r"(?:\s*:\s*[^\r\n]*)?$"
)
_MINDMAP_LINE_RE = re.compile(
    r"^(?: {2,}[A-Za-z0-9_.-]+(?:\(\([^\r\n]*\)\))?|"
    r" {2,}[\u4e00-\u9fff][^\r\n]*)$"
)


class MarkdownNoteError(ValueError):
    pass


class AcceptedIntensiveReadingNote(dict):
    """An in-memory note minted only by the accepted OCR builder."""

    __slots__ = ("__weakref__",)


_ACCEPTED_NOTE_REFS: dict[
    int, tuple[weakref.ReferenceType[AcceptedIntensiveReadingNote], str]
] = {}


def _register_accepted_note(note: AcceptedIntensiveReadingNote) -> None:
    note_id = id(note)

    def remove(_ref, *, note_id=note_id):
        _ACCEPTED_NOTE_REFS.pop(note_id, None)

    _ACCEPTED_NOTE_REFS[note_id] = (
        weakref.ref(note, remove),
        note["metadata"]["note_fingerprint"],
    )


def _require_accepted_note(note: Mapping[str, Any]) -> None:
    if type(note) is not AcceptedIntensiveReadingNote:
        raise MarkdownNoteError("note is not an accepted OCR context")
    registered = _ACCEPTED_NOTE_REFS.get(id(note))
    if registered is None or registered[0]() is not note:
        raise MarkdownNoteError("note is not an accepted OCR context")
    if note.get("metadata", {}).get("note_fingerprint") != registered[1]:
        raise MarkdownNoteError("accepted OCR context was modified")


def build_intensive_reading_note(
    chapter_tree: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    pages: Iterable[Mapping[str, Any]],
    *,
    source_id: str,
    prompt_rules_version: str = DEFAULT_PROMPT_RULES_VERSION,
) -> dict[str, Any]:
    """Build a stable note skeleton from accepted OCR evidence only.

    This function deliberately does not call a model or a network. Later model
    rounds can replace the six section bodies while retaining this contract.
    """
    try:
        validate_chapter_tree(dict(chapter_tree))
        validate_chapter_confirmation(dict(confirmation), dict(chapter_tree))
    except Exception as exc:
        raise MarkdownNoteError("chapter confirmation is invalid") from exc
    if confirmation["action"] != "confirm" or confirmation["chapter"] is None:
        raise MarkdownNoteError("a confirmed chapter is required")
    if not _safe_token(source_id) or not _safe_token(prompt_rules_version):
        raise MarkdownNoteError("source and prompt rule identifiers are invalid")

    chapter = confirmation["chapter"]
    page_records = _accepted_pages(
        pages, chapter, expected_input_fingerprint=chapter_tree["input_fingerprint"]
    )
    if not page_records:
        raise MarkdownNoteError("accepted OCR pages are required")
    source_refs: list[str] = []
    evidence_lines: list[str] = []
    for page, evidence, page_input, blocks in page_records:
        for block in blocks:
            block_id = block["id"]
            citation = f"[src:{source_id}:p{page}:{block_id}]"
            text = _safe_markdown_text(block.get("text", ""))
            if text:
                source_refs.append(citation)
                evidence_lines.append(
                    f"- {citation}（PDF 第 {page} 页；OCR 输入指纹 `{page_input}`；"
                    f"证据指纹 `{evidence}`）：{text}"
                )
    if not evidence_lines:
        raise MarkdownNoteError("accepted OCR contains no text evidence")

    metadata = {
        "input_fingerprint": chapter_tree["input_fingerprint"],
        "chapter_fingerprint": confirmation["chapter_fingerprint"],
        "evidence_fingerprint": chapter_tree["evidence_fingerprint"],
        "prompt_rules_version": prompt_rules_version,
        "source_id": source_id,
        "chapter_id": chapter["id"],
        "chapter_number": chapter["number"],
        "chapter_title": chapter["title"],
        "page_start": chapter["page_start"],
        "page_end": chapter["page_end"],
        "citation_ids": source_refs,
    }
    sections = [
        {
            "key": key,
            "title": title,
            "content": "\n".join(evidence_lines)
            if key == "source_evidence"
            else "待由 DeepSeek 按精读规则生成：概念通俗、有趣、生活化，并结合案例与实际应用。",
            "source_refs": source_refs if key == "source_evidence" else [],
        }
        for key, title in SECTION_ORDER
    ]
    mermaid = [
        {
            "key": "concept_map",
            "title": "知识结构图",
            "type": "flowchart",
            "source": _concept_mermaid(chapter["title"]),
        },
        {
            "key": "application_flow",
            "title": "应用流程图",
            "type": "flowchart",
            "source": _application_mermaid(chapter["title"]),
        },
    ]
    metadata["note_fingerprint"] = _digest(
        {"metadata": metadata, "sections": sections, "mermaid": mermaid}
    )
    note = AcceptedIntensiveReadingNote({
        "schema_version": NOTE_SCHEMA_VERSION,
        "metadata": metadata,
        "sections": sections,
        "mermaid": mermaid,
        "markdown": _render_markdown(chapter, metadata, sections, mermaid),
    })
    validate_intensive_reading_note(note)
    _register_accepted_note(note)
    return note


def validate_intensive_reading_note(value: Any) -> None:
    if not isinstance(value, dict):
        raise MarkdownNoteError("note contract is invalid")
    schema = _load_schema()
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise MarkdownNoteError("note contract schema is invalid")
    metadata = value["metadata"]
    fingerprint_input = {
        "metadata": {k: v for k, v in metadata.items() if k != "note_fingerprint"},
        "sections": value["sections"],
        "mermaid": value["mermaid"],
    }
    if metadata["note_fingerprint"] != _digest(fingerprint_input):
        raise MarkdownNoteError("note fingerprint is invalid")
    for diagram in value["mermaid"]:
        validate_mermaid_block(diagram["source"], expected_type=diagram["type"])
    markdown = value["markdown"]
    if _DANGEROUS_RE.search(markdown) or "<" in markdown:
        raise MarkdownNoteError("markdown contains unsafe markup")
    if markdown.count("```") != 4:
        raise MarkdownNoteError("markdown fence count is invalid")
    matches = _FENCE_RE.findall(markdown)
    if len(matches) != 2 or matches != [item["source"] for item in value["mermaid"]]:
        raise MarkdownNoteError("markdown Mermaid fences are invalid")
    refs = set(metadata["citation_ids"])
    if any(ref not in markdown for ref in refs):
        raise MarkdownNoteError("markdown citation is missing")
    if set(item["key"] for item in value["sections"]) != {item[0] for item in SECTION_ORDER}:
        raise MarkdownNoteError("note sections are incomplete")


def validate_mermaid_block(source: str, *, expected_type: str | None = None) -> str:
    if not isinstance(source, str) or not source.strip() or len(source) > 20_000:
        raise MarkdownNoteError("Mermaid source is invalid")
    source = source.replace("\r\n", "\n").strip()
    if "```" in source or _DANGEROUS_RE.search(source) or "<" in source:
        raise MarkdownNoteError("Mermaid source contains unsafe syntax")
    lines = source.splitlines()
    first = lines[0].split()
    kind = first[0].lower() if first else ""
    if kind not in MERMAID_TYPES or (expected_type is not None and kind != expected_type):
        raise MarkdownNoteError("Mermaid diagram type is not allowed")
    if kind in {"flowchart", "graph"}:
        if len(first) != 2 or not _DIRECTION_RE.fullmatch(first[1]):
            raise MarkdownNoteError("Mermaid direction is invalid")
        if len(lines) < 2 or any(not _FLOW_LINE_RE.fullmatch(line.strip()) for line in lines[1:]):
            raise MarkdownNoteError("Mermaid flow syntax is invalid")
    else:
        if len(lines) < 2 or not lines[1].strip().startswith("root"):
            raise MarkdownNoteError("Mermaid mindmap root is required")
        if any(not _MINDMAP_LINE_RE.fullmatch(line) for line in lines[1:]):
            raise MarkdownNoteError("Mermaid mindmap syntax is invalid")
    if any(line.count('"') % 2 for line in lines):
        raise MarkdownNoteError("Mermaid quotes are not balanced")
    return source


def persist_intensive_reading_note(path: str | Path, note: Mapping[str, Any]) -> None:
    validate_intensive_reading_note(dict(note))
    target = Path(path)
    if not target.is_absolute():
        raise MarkdownNoteError("note target must be absolute")
    if target.suffix.lower() == ".md":
        content = note["markdown"]
    else:
        content = json.dumps(note, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    directory_fd = _open_safe_parent(target.parent)
    temp_name: str | None = None
    backup_name: str | None = None
    temp_fd: int | None = None
    replaced = False
    try:
        target_exists = _validate_target_at(directory_fd, target.name)
        temp_fd, temp_name = _create_temp_at(directory_fd, f".{target.name}.")
        if target_exists:
            backup_name = _link_backup_at(directory_fd, target.name)
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as handle:
            temp_fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        replaced = True
        os.fsync(directory_fd)
    except Exception as exc:
        if replaced:
            try:
                if backup_name is not None:
                    os.replace(
                        backup_name,
                        target.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                else:
                    os.unlink(target.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        if temp_name is not None:
            _unlink_at(directory_fd, temp_name)
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if backup_name is not None:
            _unlink_at(directory_fd, backup_name)
        if isinstance(exc, MarkdownNoteError):
            raise
        raise MarkdownNoteError("note could not be published") from exc
    finally:
        if backup_name is not None and replaced:
            try:
                os.unlink(backup_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError as exc:
                raise MarkdownNoteError("note published but backup cleanup failed") from exc
        os.close(directory_fd)


def _accepted_pages(
    pages: Iterable[Mapping[str, Any]],
    chapter: Mapping[str, Any],
    *,
    expected_input_fingerprint: str,
):
    records = sorted(list(pages), key=lambda item: _page_number(item))
    start, end = chapter["page_start"], chapter["page_end"]
    if start is None or end is None or end < start:
        raise MarkdownNoteError("chapter page boundary is invalid")
    selected = [item for item in records if start <= _page_number(item) <= end]
    if [item["page"] for item in selected] != list(range(start, end + 1)):
        raise MarkdownNoteError("chapter OCR page sequence is incomplete")
    result = []
    for record in selected:
        decision = record.get("decision")
        payload = decision.get("payload") if isinstance(decision, dict) else None
        if not isinstance(payload, dict) or payload.get("status") != "accepted":
            raise MarkdownNoteError("chapter requires accepted OCR decisions")
        page_info = payload.get("page")
        try:
            validate_persisted_payload(
                payload,
                kind="adjudication",
                page=record["page"],
                width=page_info["width"],
                height=page_info["height"],
            )
        except (CodexVisionError, KeyError, TypeError) as exc:
            raise MarkdownNoteError("chapter OCR evidence is invalid") from exc
        blocks = sorted(
            payload["final_blocks"], key=lambda item: (item["reading_order"], item["id"])
        )
        page_input = record.get("page_input_fingerprint")
        evidence = record.get("evidence_fingerprint")
        if not isinstance(evidence, str) or not evidence:
            raise MarkdownNoteError("chapter OCR fingerprints are missing")
        if not isinstance(page_input, str) or not page_input:
            raise MarkdownNoteError("chapter OCR fingerprints are missing")
        if page_input != expected_input_fingerprint:
            raise MarkdownNoteError("chapter OCR input fingerprint is inconsistent")
        result.append((record["page"], evidence, page_input, blocks))
    return result


def _render_markdown(chapter, metadata, sections, mermaid):
    lines = [
        f"# {_safe_markdown_text(chapter['number'])} {_safe_markdown_text(chapter['title'])}",
        "",
        f"> 来源：PDF 第 {metadata['page_start']}–{metadata['page_end']} 页",
        f"> 输入指纹：`{metadata['input_fingerprint']}`",
        f"> 章节指纹：`{metadata['chapter_fingerprint']}`",
        f"> OCR 证据指纹：`{metadata['evidence_fingerprint']}`",
        f"> 精读规则版本：`{metadata['prompt_rules_version']}`",
        "",
    ]
    for section in sections:
        lines.extend([f"## {section['title']}", "", section["content"], ""])
        if section["key"] == "source_evidence":
            lines.extend(["来源引用必须保持在本章正文和后续模型输出中。", ""])
    lines.extend([f"## {mermaid[0]['title']}", "", "```mermaid", mermaid[0]["source"], "```", ""])
    lines.extend([f"## {mermaid[1]['title']}", "", "```mermaid", mermaid[1]["source"], "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _concept_mermaid(title: str) -> str:
    label = _mermaid_label(title)
    return f"flowchart TD\n  A[\"{label}\"] --> B[\"核心概念\"]\n  B --> C[\"案例与应用\"]"


def _application_mermaid(title: str) -> str:
    label = _mermaid_label(title)
    return (
        f"flowchart LR\n  A[\"识别 {label}\"] --> B[\"分析问题\"]\n"
        "  B --> C[\"选择行动\"]\n  C --> D[\"复盘结果\"]"
    )


def _mermaid_label(value: str) -> str:
    value = _safe_markdown_text(value).replace('"', "#quot;")
    if not value or len(value) > 120:
        raise MarkdownNoteError("Mermaid node label is invalid")
    return value


def _safe_markdown_text(value: Any) -> str:
    if not isinstance(value, str):
        raise MarkdownNoteError("OCR text is invalid")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    value = value.replace("```", "` ` `").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("\r", " ").replace("\n", " ")
    return value[:8192]


def _safe_token(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value))


def _page_number(record: Mapping[str, Any]) -> int:
    value = record.get("page")
    if isinstance(value, dict):
        value = value.get("number")
    if not isinstance(value, int) or value < 1:
        raise MarkdownNoteError("OCR page number is invalid")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_schema() -> dict[str, Any]:
    path = Path(__file__).with_name("schemas") / "intensive-reading-note.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarkdownNoteError("note schema cannot be loaded") from exc


def _open_safe_parent(parent: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(parent.anchor, flags)
    try:
        for part in parent.parts[1:]:
            if not part:
                continue
            try:
                child_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise MarkdownNoteError("note parent is not safe")
        return fd
    except Exception:
        os.close(fd)
        raise MarkdownNoteError("note parent is not safe") from None


def _validate_target_at(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MarkdownNoteError("note target is not safe")
    return True


def _create_temp_at(directory_fd: int, prefix: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(10):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            continue
    raise MarkdownNoteError("note temporary file could not be created")


def _link_backup_at(directory_fd: int, target_name: str) -> str:
    for _ in range(10):
        name = f".{target_name}.backup.{secrets.token_hex(16)}"
        try:
            os.link(
                target_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            return name
        except FileExistsError:
            continue
    raise MarkdownNoteError("note backup could not be created")


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
