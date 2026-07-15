"""Deterministic chapter proposals and evidence-bound confirmation contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ChapterConfirmationError(ValueError):
    pass


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


def detect_chapter_tree(pages: Any, *, input_fingerprint: str) -> dict[str, Any]:
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
    expected = list(range(page_lines[0]["page"], page_lines[-1]["page"] + 1))
    if [item["page"] for item in page_lines] != expected:
        raise ChapterConfirmationError("OCR page sequence is incomplete")

    toc = []
    body = []
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
        key for key, items in _group(body, lambda item: item["key"]).items() if len(items) > 1
    }
    duplicate_titles = {
        key
        for key, items in _group(body, lambda item: item["title"].casefold()).items()
        if len(items) > 1
    }
    if duplicates or duplicate_titles:
        warnings.append("正文标题重复")

    entries = _merge_candidates(toc, body, conflicts)
    if not entries:
        tree = _tree(input_fingerprint, page_lines, [], ["未识别到章节标题"])
        validate_chapter_tree(tree)
        return tree
    entries.sort(
        key=lambda item: (item["page_start"] or 10**9, _number_sort(item["_key"]), item["id"])
    )
    for index, entry in enumerate(entries):
        next_start = next(
            (other["page_start"] for other in entries[index + 1 :] if other["page_start"]), None
        )
        if entry["page_start"] and not entry["page_end"]:
            if next_start is not None and next_start <= entry["page_start"]:
                entry["page_end"] = entry["page_start"]
                entry["warnings"].append("页码边界冲突，需要确认")
                entry["needs_confirmation"] = True
                warnings.append("页码边界冲突")
            else:
                entry["page_end"] = (next_start - 1) if next_start else page_lines[-1]["page"]
        entry["needs_confirmation"] |= (
            bool(warnings)
            or entry["_key"] in duplicates
            or entry["title"].casefold() in duplicate_titles
        )
        if entry["_key"] in duplicates or entry["title"].casefold() in duplicate_titles:
            entry["warnings"].append("正文标题重复，边界需要确认")
    roots = _nest(entries)
    tree = _tree(input_fingerprint, page_lines, roots, warnings)
    validate_chapter_tree(tree)
    return tree


def validate_chapter_tree(value: Any) -> None:
    """Validate the public chapter proposal contract before persistence/use."""
    validator = _validator("chapter-tree.json")
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ChapterConfirmationError("chapter tree schema is invalid")
    for chapter in _flatten(value["chapters"]):
        if (
            chapter["page_start"] is not None
            and chapter["page_end"] is not None
            and chapter["page_end"] < chapter["page_start"]
        ):
            raise ChapterConfirmationError("chapter page boundary is invalid")


def validate_chapter_confirmation(value: Any, tree: dict[str, Any]) -> None:
    """Validate schema and ensure a confirmation still belongs to this proposal."""
    if not isinstance(tree, dict):
        raise ChapterConfirmationError("chapter tree is invalid")
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
    if value["action"] != "reject" and value.get("chapter") is None:
        raise ChapterConfirmationError("edited or confirmed chapter is required")
    if value.get("chapter") is not None and value["chapter"]["id"] != chapter["id"]:
        raise ChapterConfirmationError("chapter confirmation target is inconsistent")


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
    fd, name = tempfile.mkstemp(prefix=".chapter-confirmation.", dir=target.parent)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        os.replace(name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(name).unlink(missing_ok=True)


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


def _extract_page(record: Any) -> dict[str, Any]:
    page = _page_number(record)
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
    return {
        "page": page,
        "blocks": blocks,
        "evidence": record.get("evidence_fingerprint", ""),
        "input": record.get("page_input_fingerprint", ""),
    }


def _body_candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for block in page["blocks"]:
        text = _text(block)
        if _TOC_RE.match(text):
            continue
        match = _NUMBER_RE.match(text)
        if match and len(match.group("title")) >= 2:
            found.append(
                _candidate(page, block, match.group("number"), match.group("title"), "body")
            )
    return found


def _toc_candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    texts = [_text(block) for block in page["blocks"]]
    likely = any(
        "目录" in text or text.lower() in {"contents", "table of contents"} for text in texts
    )
    found = []
    for block, text in zip(page["blocks"], texts, strict=True):
        match = _TOC_RE.match(text)
        if match and (likely or re.search(r"\.{2,}|…+", text)):
            found.append(
                {
                    **_candidate(page, block, match.group("number"), match.group("title"), "toc"),
                    "printed_page": int(match.group("page")),
                }
            )
    return found


def _merge_candidates(toc, body, conflicts):
    body_by_key = _group(body, lambda item: item["key"])
    toc_by_key = _group(toc, lambda item: item["key"])
    all_keys = sorted(set(body_by_key) | set(toc_by_key), key=lambda key: (_number_sort(key), key))
    results = []
    for key in all_keys:
        bodies = body_by_key.get(key, [])
        tocs = toc_by_key.get(key, [])
        chosen = bodies[0] if bodies else tocs[0]
        physical_pages = sorted({item["page"] for item in bodies})
        printed_pages = sorted({item["printed_page"] for item in tocs})
        page_start = physical_pages[0] if physical_pages else None
        warnings = []
        if len(physical_pages) > 1:
            warnings.append("正文标题重复")
        if len(printed_pages) > 1:
            warnings.append("目录页码冲突")
        confidence = 0.95 if bodies and tocs and not warnings else (0.72 if bodies else 0.35)
        evidence_items = _evidence_items(tocs, bodies)
        results.append(
            {
                "id": _stable_id(key, chosen["title"]),
                "number": chosen["number"],
                "title": chosen["title"],
                "level": _level(key),
                "toc_page": printed_pages[0] if printed_pages else None,
                "page_start": page_start,
                "page_end": None,
                "source_evidence": [_evidence(item) for item in evidence_items],
                "confidence": confidence,
                "warnings": warnings,
                "needs_confirmation": bool(warnings) or not bodies or not tocs,
                "children": [],
                "_key": key,
                "_evidence_items": evidence_items,
            }
        )
    return results


def _tree(input_fingerprint, pages, roots, warnings):
    flat = _flatten(roots)
    evidence_fingerprint = _digest(
        [
            {"page": page["page"], "evidence": page["evidence"], "input": page["input"]}
            for page in pages
        ]
    )
    clean_roots = _clean(roots)
    proposal_fingerprint = _digest(clean_roots)
    return {
        "schema_version": 1,
        "input_fingerprint": input_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
        "chapters": clean_roots,
        "warnings": sorted(set(warnings)),
        "needs_confirmation": bool(warnings) or any(item["needs_confirmation"] for item in flat),
    }


def _nest(entries):
    roots = []
    stack = []
    for entry in entries:
        item = dict(entry)
        item.pop("_key", None)
        item.pop("_evidence_items", None)
        while stack and stack[-1]["level"] >= item["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(item)
        else:
            roots.append(item)
        stack.append(item)
    for node in _flatten(roots):
        descendants = _flatten(node["children"])
        if descendants and node["page_start"]:
            ends = [item["page_end"] for item in descendants if item["page_end"]]
            if ends:
                node["page_end"] = max(ends)
    return roots


def _clean(value):
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if not key.startswith("_")}
    return value


def _flatten(items):
    output = []
    for item in items:
        output.append(item)
        output.extend(_flatten(item.get("children", [])))
    return output


def _candidate(page, block, number, title, kind):
    title = _redact(" ".join(title.split()).strip(" .…"))
    key = _normalize_number(number)
    return {
        "page": page["page"],
        "number": number.strip(),
        "title": title,
        "key": key,
        "kind": kind,
        "block_id": str(block.get("id", "")),
        "evidence": page["evidence"],
        "input": page["input"],
    }


def _evidence(item):
    return {
        "kind": item["kind"],
        "page": item["page"],
        "block_id": item["block_id"],
        "evidence_fingerprint": item["evidence"] or "unknown",
        "excerpt": item["title"],
    }


def _evidence_items(tocs, bodies):
    return [*tocs, *bodies]


def _group(items, key):
    grouped = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _toc_conflicts(toc):
    return {
        key
        for key, items in _group(toc, lambda item: item["key"]).items()
        if len({item["printed_page"] for item in items}) > 1
    }


def _text(block):
    value = block.get("text", "") if isinstance(block, dict) else ""
    return value if isinstance(value, str) else ""


def _page_number(record):
    value = record.get("page") if isinstance(record, dict) else None
    if isinstance(value, dict):
        value = value.get("number")
    if not isinstance(value, int) or value < 1:
        raise ChapterConfirmationError("OCR page number is invalid")
    return value


def _normalize_number(value):
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


def _number_sort(value):
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts) if parts else (10**9,)


def _level(number):
    return len(re.findall(r"\d+", number)) or (2 if number.lower().startswith("chapter") else 1)


def _stable_id(number, title):
    return (
        "chapter-"
        + hashlib.sha256(f"{_normalize_number(number)}\0{title}".encode()).hexdigest()[:24]
    )


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _redact(value):
    return _SECRET_RE.sub("[REDACTED]", _PATH_RE.sub("[REDACTED]", value))[:512]


def _find_chapter(items, chapter_id):
    for item in items:
        if item.get("id") == chapter_id:
            return item
        found = _find_chapter(item.get("children", []), chapter_id)
        if found:
            return found
    return None


def _validator(name):
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
