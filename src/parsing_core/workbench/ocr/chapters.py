"""Deterministic chapter proposals and evidence-bound confirmation contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator

from .atomic_io import atomic_replace_bytes


class ChapterConfirmationError(ValueError):
    pass


@dataclass(frozen=True)
class _PageBlock:
    id: object
    text: object


@dataclass(frozen=True)
class _PageEvidence:
    page: int
    blocks: tuple[_PageBlock, ...]
    evidence: str
    input_fingerprint: str


@dataclass(frozen=True)
class _Candidate:
    page: int
    number: str
    title: str
    key: str
    kind: Literal["toc", "body"]
    block_id: str
    evidence: str
    printed_page: int | None = None


class _ChapterEvidence(TypedDict):
    kind: Literal["toc", "body"]
    page: int
    block_id: str
    evidence_fingerprint: str
    excerpt: str


class _Chapter(TypedDict):
    id: str
    number: str
    title: str
    level: int
    toc_page: int | None
    page_start: int | None
    page_end: int | None
    source_evidence: list[_ChapterEvidence]
    confidence: float
    warnings: list[str]
    needs_confirmation: bool
    children: list[_Chapter]


@dataclass
class _ChapterEntry:
    id: str
    number: str
    title: str
    level: int
    toc_page: int | None
    page_start: int | None
    page_end: int | None
    source_evidence: list[_ChapterEvidence]
    confidence: float
    warnings: list[str]
    needs_confirmation: bool
    key: str


class _ChapterTree(TypedDict):
    schema_version: int
    input_fingerprint: str
    evidence_fingerprint: str
    proposal_fingerprint: str
    chapters: list[_Chapter]
    warnings: list[str]
    needs_confirmation: bool


_CHINESE_NUMBER = r"[一二三四五六七八九十百千万零〇两\d]+"
_NUMBER_RE = re.compile(
    rf"^(?P<number>(?:第\s*{_CHINESE_NUMBER}\s*章|chapter\s+[A-Za-z0-9.-]+|\d+(?:\.\d+)*))\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_TOC_RE = re.compile(
    rf"^(?P<number>(?:第\s*{_CHINESE_NUMBER}\s*章|chapter\s+[A-Za-z0-9.-]+|\d+(?:\.\d+)*))\s+(?P<title>.+?)\s*(?:\.{{2,}}|…+|\s{{2,}})\s*(?P<page>\d{{1,4}})\s*$",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:/Users/[^\s]+|/private/[^\s]+|[A-Za-z]:\\[^\s]+)")
_SECRET_RE = re.compile(r"\b(?:sk|key|token)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)


def _write_file(fd: int, data: memoryview) -> int:
    return os.write(fd, data)


def _sync_file(fd: int) -> None:
    os.fsync(fd)


def _close_file(fd: int) -> None:
    os.close(fd)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _replace_file(source: Path, target: Path, directory_fd: int) -> None:
    if source.parent != target.parent:
        raise ValueError("atomic source and target must share a directory")
    os.replace(
        source.name,
        target.name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _sync_directory(fd: int) -> None:
    os.fsync(fd)


def _unlink_temporary(path: Path) -> None:
    path.unlink(missing_ok=True)


def detect_chapter_tree(pages: Iterable[object], *, input_fingerprint: str) -> dict[str, Any]:
    """Build a stable proposal from completed Task 6 page decisions.

    ``pages`` may be any iterable of persisted page records. Every record must
    contain an accepted final adjudication; no incomplete OCR evidence is read.
    """
    if not isinstance(input_fingerprint, str) or not input_fingerprint:
        raise ChapterConfirmationError("input fingerprint is required")
    records = sorted(list(pages), key=lambda item: _page_number(item))
    if not records:
        raise ChapterConfirmationError("OCR pages are required")
    page_lines = [_extract_page(record) for record in records]
    expected = list(range(page_lines[0].page, page_lines[-1].page + 1))
    if [item.page for item in page_lines] != expected:
        raise ChapterConfirmationError("OCR page sequence is incomplete")

    toc: list[_Candidate] = []
    body: list[_Candidate] = []
    for page in page_lines:
        toc.extend(_toc_candidates(page))
        body.extend(_body_candidates(page))
    warnings: list[str] = []
    if not toc:
        warnings.append("目录页未识别，章节边界来自正文标题")
    conflicts = _toc_conflicts(toc)
    if conflicts:
        warnings.append("目录页码冲突")
    duplicates = {
        key for key, items in _group(body, lambda item: item.key).items() if len(items) > 1
    }
    duplicate_titles = {
        key
        for key, items in _group(body, lambda item: item.title.casefold()).items()
        if len(items) > 1
    }
    if duplicates or duplicate_titles:
        warnings.append("正文标题重复")

    entries = _merge_candidates(toc, body)
    if not entries:
        tree = _tree(input_fingerprint, page_lines, [], ["未识别到章节标题"])
        validate_chapter_tree(tree)
        return dict(tree)
    entries.sort(key=lambda item: (item.page_start or 10**9, _number_sort(item.key), item.id))
    for index, entry in enumerate(entries):
        next_start = next(
            (other.page_start for other in entries[index + 1 :] if other.page_start), None
        )
        if entry.page_start and not entry.page_end:
            if next_start is not None and next_start <= entry.page_start:
                entry.page_end = entry.page_start
                entry.warnings.append("页码边界冲突，需要确认")
                entry.needs_confirmation = True
                warnings.append("页码边界冲突")
            else:
                entry.page_end = (next_start - 1) if next_start else page_lines[-1].page
        entry.needs_confirmation |= (
            bool(warnings) or entry.key in duplicates or entry.title.casefold() in duplicate_titles
        )
        if entry.key in duplicates or entry.title.casefold() in duplicate_titles:
            entry.warnings.append("正文标题重复，边界需要确认")
    roots = _nest(entries)
    tree = _tree(input_fingerprint, page_lines, roots, warnings)
    validate_chapter_tree(tree)
    return dict(tree)


def validate_chapter_tree(value: Any) -> None:
    """Validate the public chapter proposal contract before persistence/use."""
    if not isinstance(value, dict):
        raise ChapterConfirmationError("chapter tree schema is invalid")
    validator = _validator("chapter-tree.json")
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ChapterConfirmationError("chapter tree schema is invalid")
    for chapter in _flatten_validated_chapters(value.get("chapters")):
        page_start = chapter.get("page_start")
        page_end = chapter.get("page_end")
        if isinstance(page_start, int) and isinstance(page_end, int) and page_end < page_start:
            raise ChapterConfirmationError("chapter page boundary is invalid")


def validate_chapter_confirmation(value: Any, tree: dict[str, Any]) -> None:
    """Validate schema and ensure a confirmation still belongs to this proposal."""
    if not isinstance(tree, dict):
        raise ChapterConfirmationError("chapter tree is invalid")
    validate_chapter_tree(tree)
    validator = _validator("chapter-confirmation.json")
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ChapterConfirmationError("chapter confirmation schema is invalid")
    for field in ("input_fingerprint", "proposal_fingerprint", "evidence_fingerprint"):
        if value[field] != tree.get(field):
            raise ChapterConfirmationError(f"chapter confirmation {field} does not match evidence")
    chapter = _find_chapter(tree.get("chapters", []), value["chapter_id"])
    if chapter is None:
        raise ChapterConfirmationError("chapter confirmation target is missing")
    if value["action"] == "reject":
        if value["chapter"] is not None or value["chapter_fingerprint"] is not None:
            raise ChapterConfirmationError("rejected chapter must not contain edited content")
        return

    confirmed = value["chapter"]
    if confirmed is None or confirmed["id"] != chapter["id"]:
        raise ChapterConfirmationError("chapter confirmation target is inconsistent")
    if value["chapter_fingerprint"] != _chapter_fingerprint(confirmed):
        raise ChapterConfirmationError("chapter content fingerprint is invalid")
    if value["action"] == "confirm":
        if confirmed != chapter:
            raise ChapterConfirmationError("confirmed chapter does not match proposal")
        return

    _validate_edited_chapter(confirmed, chapter)


def persist_chapter_confirmation(path: str | Path, value: dict[str, Any]) -> None:
    """Atomically write a validated JSON contract without following a target symlink."""
    _validate_confirmation_schema(value)
    target = Path(path)
    try:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ChapterConfirmationError("confirmation target is not safe")
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fd, name = tempfile.mkstemp(prefix=".chapter-confirmation.", dir=target.parent)
    atomic_replace_bytes(
        fd=fd,
        temporary=Path(name),
        target=target,
        data=encoded,
        writer=_write_file,
        sync_file=_sync_file,
        close_file=_close_file,
        open_directory=_open_directory,
        replace_file=_replace_file,
        sync_directory=_sync_directory,
        unlink_temporary=_unlink_temporary,
    )


def load_chapter_confirmation(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        info = target.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1024 * 1024
        ):
            raise ChapterConfirmationError("confirmation target is not safe")
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            if actual.st_nlink != 1 or not stat.S_ISREG(actual.st_mode):
                raise ChapterConfirmationError("confirmation target is not safe")
            data = os.read(fd, 1024 * 1024 + 1)
        finally:
            os.close(fd)
        if len(data) > 1024 * 1024:
            raise ChapterConfirmationError("confirmation is too large")
        value = json.loads(data.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChapterConfirmationError("confirmation cannot be read") from exc
    if not isinstance(value, dict):
        raise ChapterConfirmationError("confirmation schema is invalid")
    _validate_confirmation_schema(value)
    return value


def _extract_page(record: object) -> _PageEvidence:
    page = _page_number(record)
    if not isinstance(record, dict):
        raise ChapterConfirmationError("OCR page number is invalid")
    decision = record.get("decision") if isinstance(record, dict) else None
    payload = decision.get("payload") if isinstance(decision, dict) else None
    if not isinstance(payload, dict) or payload.get("status") != "accepted":
        raise ChapterConfirmationError("chapter detection requires accepted OCR decisions")
    page_info = payload.get("page")
    if not isinstance(page_info, dict) or page_info.get("number") != page:
        raise ChapterConfirmationError("OCR page evidence is inconsistent")
    blocks = payload.get("final_blocks")
    if (
        not isinstance(blocks, list)
        or not record.get("evidence_fingerprint")
        or not record.get("page_input_fingerprint")
    ):
        raise ChapterConfirmationError("OCR final blocks are missing")
    evidence = record.get("evidence_fingerprint")
    page_input = record.get("page_input_fingerprint")
    if not isinstance(evidence, str) or not isinstance(page_input, str):
        raise ChapterConfirmationError("OCR final blocks are missing")
    page_blocks = tuple(
        _PageBlock(
            id=block.get("id", "") if isinstance(block, dict) else "",
            text=block.get("text", "") if isinstance(block, dict) else "",
        )
        for block in blocks
    )
    return _PageEvidence(page, page_blocks, evidence, page_input)


def _body_candidates(page: _PageEvidence) -> list[_Candidate]:
    found: list[_Candidate] = []
    for block in page.blocks:
        text = _text(block)
        if _TOC_RE.match(text):
            continue
        match = _NUMBER_RE.match(text)
        if match and len(match.group("title")) >= 2:
            found.append(
                _candidate(page, block, match.group("number"), match.group("title"), "body")
            )
    return found


def _toc_candidates(page: _PageEvidence) -> list[_Candidate]:
    texts = [_text(block) for block in page.blocks]
    likely = any(
        "目录" in text or text.lower() in {"contents", "table of contents"} for text in texts
    )
    found: list[_Candidate] = []
    for block, text in zip(page.blocks, texts, strict=True):
        match = _TOC_RE.match(text)
        if match and (likely or re.search(r"\.{2,}|…+", text)):
            found.append(
                _candidate(
                    page,
                    block,
                    match.group("number"),
                    match.group("title"),
                    "toc",
                    printed_page=int(match.group("page")),
                )
            )
    return found


def _merge_candidates(toc: Sequence[_Candidate], body: Sequence[_Candidate]) -> list[_ChapterEntry]:
    body_by_key = _group(body, lambda item: item.key)
    toc_by_key = _group(toc, lambda item: item.key)
    all_keys = sorted(set(body_by_key) | set(toc_by_key), key=lambda key: (_number_sort(key), key))
    results: list[_ChapterEntry] = []
    for key in all_keys:
        bodies = body_by_key.get(key, [])
        tocs = toc_by_key.get(key, [])
        chosen = bodies[0] if bodies else tocs[0]
        physical_pages = sorted({item.page for item in bodies})
        printed_pages = sorted(item.printed_page for item in tocs if item.printed_page is not None)
        page_start = physical_pages[0] if physical_pages else None
        warnings = []
        if len(physical_pages) > 1:
            warnings.append("正文标题重复")
        if len(printed_pages) > 1:
            warnings.append("目录页码冲突")
        confidence = 0.95 if bodies and tocs and not warnings else (0.72 if bodies else 0.35)
        evidence_items = _evidence_items(tocs, bodies)
        results.append(
            _ChapterEntry(
                id=_stable_id(key, chosen.title),
                number=chosen.number,
                title=chosen.title,
                level=_level(key),
                toc_page=printed_pages[0] if printed_pages else None,
                page_start=page_start,
                page_end=None,
                source_evidence=[_evidence(item) for item in evidence_items],
                confidence=confidence,
                warnings=warnings,
                needs_confirmation=bool(warnings) or not bodies or not tocs,
                key=key,
            )
        )
    return results


def _tree(
    input_fingerprint: str,
    pages: Sequence[_PageEvidence],
    roots: list[_Chapter],
    warnings: Sequence[str],
) -> _ChapterTree:
    flat = _flatten_chapters(roots)
    evidence_fingerprint = _digest(
        [
            {
                "page": page.page,
                "evidence": page.evidence,
                "input": page.input_fingerprint,
            }
            for page in pages
        ]
    )
    proposal_fingerprint = _digest(roots)
    return {
        "schema_version": 1,
        "input_fingerprint": input_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
        "chapters": roots,
        "warnings": sorted(set(warnings)),
        "needs_confirmation": bool(warnings) or any(item["needs_confirmation"] for item in flat),
    }


def _nest(entries: Sequence[_ChapterEntry]) -> list[_Chapter]:
    roots: list[_Chapter] = []
    stack: list[_Chapter] = []
    for entry in entries:
        item: _Chapter = {
            "id": entry.id,
            "number": entry.number,
            "title": entry.title,
            "level": entry.level,
            "toc_page": entry.toc_page,
            "page_start": entry.page_start,
            "page_end": entry.page_end,
            "source_evidence": list(entry.source_evidence),
            "confidence": entry.confidence,
            "warnings": list(entry.warnings),
            "needs_confirmation": entry.needs_confirmation,
            "children": [],
        }
        while stack and stack[-1]["level"] >= item["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(item)
        else:
            roots.append(item)
        stack.append(item)
    for node in _flatten_chapters(roots):
        descendants = _flatten_chapters(node["children"])
        if descendants and node["page_start"]:
            ends = [item["page_end"] for item in descendants if item["page_end"]]
            if ends:
                node["page_end"] = max(ends)
    return roots


def _flatten_chapters(items: Iterable[_Chapter]) -> list[_Chapter]:
    output: list[_Chapter] = []
    for item in items:
        output.append(item)
        output.extend(_flatten_chapters(item["children"]))
    return output


def _candidate(
    page: _PageEvidence,
    block: _PageBlock,
    number: str,
    title: str,
    kind: Literal["toc", "body"],
    *,
    printed_page: int | None = None,
) -> _Candidate:
    title = _redact(" ".join(title.split()).strip(" .…"))
    key = _normalize_number(number)
    return _Candidate(
        page=page.page,
        number=number.strip(),
        title=title,
        key=key,
        kind=kind,
        block_id=str(block.id),
        evidence=page.evidence,
        printed_page=printed_page,
    )


def _evidence(item: _Candidate) -> _ChapterEvidence:
    return {
        "kind": item.kind,
        "page": item.page,
        "block_id": item.block_id,
        "evidence_fingerprint": item.evidence or "unknown",
        "excerpt": item.title,
    }


def _evidence_items(tocs: Sequence[_Candidate], bodies: Sequence[_Candidate]) -> list[_Candidate]:
    return [*tocs, *bodies]


def _group[Item, Key: Hashable](
    items: Iterable[Item], key: Callable[[Item], Key]
) -> dict[Key, list[Item]]:
    grouped: dict[Key, list[Item]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _toc_conflicts(toc: Sequence[_Candidate]) -> set[str]:
    return {
        key
        for key, items in _group(toc, lambda item: item.key).items()
        if len({item.printed_page for item in items}) > 1
    }


def _text(block: _PageBlock) -> str:
    value = block.text
    return value if isinstance(value, str) else ""


def _page_number(record: object) -> int:
    value = record.get("page") if isinstance(record, Mapping) else None
    if isinstance(value, dict):
        value = value.get("number")
    if not isinstance(value, int) or value < 1:
        raise ChapterConfirmationError("OCR page number is invalid")
    return value


def _normalize_number(value: str) -> str:
    value = value.lower().replace(" ", "")
    if value.startswith("第") and value.endswith("章"):
        raw = value[1:-1]
        digits = {
            "零": 0,
            "〇": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        if raw in digits:
            return str(digits[raw])
        if raw.startswith("十"):
            return str(10 + digits.get(raw[1:], 0))
        if raw.endswith("十"):
            return str(digits.get(raw[0], 0) * 10)
        if "十" in raw:
            left, right = raw.split("十", 1)
            return str(digits.get(left, 0) * 10 + digits.get(right, 0))
    if value.startswith("chapter"):
        value = value.replace("chapter", "", 1)
    return value


def _number_sort(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts) if parts else (10**9,)


def _level(number: str) -> int:
    return len(re.findall(r"\d+", number)) or (2 if number.lower().startswith("chapter") else 1)


def _stable_id(number: str, title: str) -> str:
    return (
        "chapter-"
        + hashlib.sha256(f"{_normalize_number(number)}\0{title}".encode()).hexdigest()[:24]
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _redact(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", _PATH_RE.sub("[REDACTED]", value))[:512]


def _find_chapter(items: object, chapter_id: object) -> dict[str, object] | None:
    if not isinstance(items, list):
        return None
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item = {key: value for key, value in raw_item.items() if isinstance(key, str)}
        if item.get("id") == chapter_id:
            return item
        found = _find_chapter(item.get("children", []), chapter_id)
        if found:
            return found
    return None


def _flatten_validated_chapters(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        raise ChapterConfirmationError("chapter tree schema is invalid")
    output: list[dict[str, object]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict) or any(not isinstance(key, str) for key in raw_item):
            raise ChapterConfirmationError("chapter tree schema is invalid")
        item = {key: value for key, value in raw_item.items() if isinstance(key, str)}
        output.append(item)
        output.extend(_flatten_validated_chapters(item.get("children")))
    return output


_EDITABLE_CHAPTER_FIELDS = frozenset(
    {"title", "number", "page_start", "page_end", "warnings", "needs_confirmation"}
)
_IMMUTABLE_CHAPTER_FIELDS = frozenset(
    {"id", "level", "toc_page", "source_evidence", "confidence", "children"}
)


def _chapter_fingerprint(chapter: Mapping[str, object]) -> str:
    return _digest(chapter)


def _validate_edited_chapter(edited: Mapping[str, object], proposal: Mapping[str, object]) -> None:
    """Allow only explicit metadata edits; OCR provenance and tree shape stay fixed."""
    if set(edited) != set(proposal):
        raise ChapterConfirmationError("edited chapter fields are invalid")
    for field in _IMMUTABLE_CHAPTER_FIELDS:
        if edited[field] != proposal[field]:
            raise ChapterConfirmationError(f"chapter {field} cannot be edited")
    if not set(edited).issuperset(_EDITABLE_CHAPTER_FIELDS):
        raise ChapterConfirmationError("edited chapter fields are incomplete")
    page_start = edited["page_start"]
    page_end = edited["page_end"]
    if isinstance(page_start, int) and isinstance(page_end, int) and page_end < page_start:
        raise ChapterConfirmationError("edited chapter page boundary is invalid")


def _validator(name: str) -> Draft202012Validator:
    base = Path(__file__).with_name("schemas")
    schema = json.loads((base / name).read_text(encoding="utf-8"))
    if name == "chapter-confirmation.json":
        tree_schema = json.loads((base / "chapter-tree.json").read_text(encoding="utf-8"))
        schema["$defs"] = tree_schema["$defs"]
        schema["properties"]["chapter"]["anyOf"][0]["$ref"] = "#/$defs/chapter"
    return Draft202012Validator(schema)


def _validate_confirmation_schema(value: Any) -> None:
    errors = list(_validator("chapter-confirmation.json").iter_errors(value))
    if errors:
        raise ChapterConfirmationError("chapter confirmation schema is invalid")
