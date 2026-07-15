"""Application-facing bridge for the unattended OCR and intensive-reading flow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .chapters import (
    _chapter_fingerprint,
    detect_chapter_tree,
    persist_chapter_confirmation,
    validate_chapter_tree,
)
from .markdown_notes import validate_mermaid_block
from .orchestrator import BatchStatus, OcrOrchestrator, _snapshot_pdf

_MARKDOWN_FENCE_RE = re.compile(r"```mermaid\n([\s\S]*?)\n```", re.MULTILINE)
_SECTION_HEADINGS = (
    "原文证据",
    "核心概念",
    "通俗、有趣、生活化的解释",
    "教材案例解读",
    "实际例子与问题解决",
    "实际应用",
    "知识结构图",
    "应用流程图",
)


class WorkflowStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowBlockedError(RuntimeError):
    """A required local or remote engine is not configured."""


@dataclass(frozen=True)
class WorkflowPaths:
    root: Path
    state: Path
    final: Path
    chapter_tree: Path
    confirmation: Path
    note: Path


def workflow_paths(root: str | Path) -> WorkflowPaths:
    root_path = Path(root).expanduser().resolve()
    return WorkflowPaths(
        root=root_path,
        state=root_path / "batch-state.json",
        final=root_path / "batch-final.json",
        chapter_tree=root_path / "chapter-tree.json",
        confirmation=root_path / "chapter-confirmation.json",
        note=root_path / "intensive-reading.md",
    )


def status_payload(
    *,
    status: WorkflowStatus,
    source_path: str | Path,
    state_root: str | Path,
    error: str | None = None,
) -> dict[str, Any]:
    paths = workflow_paths(state_root)
    published = status is WorkflowStatus.COMPLETED and _final_publication_is_valid(
        paths.final, paths.note, source_path
    )
    public_status = (
        WorkflowStatus.BLOCKED
        if status is WorkflowStatus.COMPLETED and not published
        else status
    )
    return {
        "status": public_status.value,
        "source_path": str(Path(source_path).expanduser()),
        "state_path": str(paths.state),
        "error": error,
        "publishable": published,
        "markdown_path": str(paths.note) if published else None,
        "chapter_tree_path": str(paths.chapter_tree) if paths.chapter_tree.is_file() else None,
    }


def _final_publication_is_valid(
    final_path: Path, note_path: Path, source_path: str | Path
) -> bool:
    try:
        final = _read_regular_json(final_path)
        if final.get("status") != BatchStatus.COMPLETED.value:
            return False
        snapshot = final.get("pdf_snapshot")
        current_snapshot = _snapshot_pdf(source_path)
        if not isinstance(snapshot, dict) or snapshot != current_snapshot:
            return False
        input_fingerprint = final.get("input_fingerprint")
        pages = final.get("pages")
        if not isinstance(input_fingerprint, str) or not input_fingerprint:
            return False
        if not isinstance(pages, dict) or not pages:
            return False
        page_numbers = sorted(int(key) for key in pages)
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            return False
        validator = OcrOrchestrator(
            vision=None, codex=None, baidu=None, state_root=final_path.parent
        )
        for page in page_numbers:
            record = pages[str(page)]
            if not isinstance(record, dict) or record.get("status") != "completed":
                return False
            alignment = record.get("alignment")
            if not isinstance(alignment, dict):
                return False
            sample_rate = 0.05 if alignment.get("baidu_required") else 0.0
            if not validator._completed_evidence_is_valid(
                final, record, page, sample_rate
            ):
                return False
        return _markdown_publication_is_valid(final, note_path, input_fingerprint)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _read_regular_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
        raise ValueError("final artifact is not a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("final artifact is invalid")
    return value


def _markdown_publication_is_valid(
    final: dict[str, Any], path: Path, input_fingerprint: str
) -> bool:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
        return False
    markdown = path.read_text(encoding="utf-8")
    if not markdown.endswith("\n") or "待由 DeepSeek" in markdown:
        return False
    if final.get("markdown_sha256") != hashlib.sha256(markdown.encode("utf-8")).hexdigest():
        return False
    if final.get("model") != "deepseek-v4-pro" or final.get(
        "ruleset"
    ) != "mba-intensive-reading-v1":
        return False
    if final.get("note_input_fingerprint") != input_fingerprint:
        return False
    chapter_fingerprint = final.get("chapter_fingerprint")
    evidence_fingerprint = final.get("note_evidence_fingerprint")
    prompt_fingerprint = final.get("prompt_fingerprint")
    if not all(
        isinstance(value, str) and value
        for value in (chapter_fingerprint, evidence_fingerprint, prompt_fingerprint)
    ):
        return False
    if f"> 章节指纹：`{chapter_fingerprint}`" not in markdown:
        return False
    if f"> OCR 证据指纹：`{evidence_fingerprint}`" not in markdown:
        return False
    if f"> Prompt 指纹：`{prompt_fingerprint}`" not in markdown:
        return False
    if f"> 输入指纹：`{input_fingerprint}`" not in markdown:
        return False
    if "> 精读规则版本：`mba-intensive-reading-v1`" not in markdown:
        return False
    if "> 模型：`deepseek-v4-pro`" not in markdown:
        return False
    if not all(f"## {heading}" in markdown for heading in _SECTION_HEADINGS):
        return False
    diagrams = _MARKDOWN_FENCE_RE.findall(markdown)
    if len(diagrams) != 2:
        return False
    try:
        validate_mermaid_block(diagrams[0], expected_type="flowchart")
        validate_mermaid_block(diagrams[1], expected_type="flowchart")
    except Exception:
        return False
    return "[src:" in markdown


def bind_published_note(
    final_path: str | Path, note_path: str | Path, metadata: dict[str, Any]
) -> None:
    """Bind the generated note artifact to the completed OCR evidence."""
    final = _read_regular_json(Path(final_path))
    note = Path(note_path)
    info = note.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("published note is invalid")
    content = note.read_bytes()
    if not isinstance(metadata, dict) or metadata.get("model") != "deepseek-v4-pro":
        raise ValueError("published model is invalid")
    if metadata.get("prompt_rules_version") != "mba-intensive-reading-v1":
        raise ValueError("published ruleset is invalid")
    final.update(
        {
            "markdown_sha256": hashlib.sha256(content).hexdigest(),
            "model": metadata["model"],
            "ruleset": metadata["prompt_rules_version"],
            "prompt_fingerprint": metadata.get("prompt_fingerprint", ""),
            "chapter_fingerprint": metadata.get("chapter_fingerprint", ""),
            "note_input_fingerprint": metadata.get("input_fingerprint", ""),
            "note_evidence_fingerprint": metadata.get("evidence_fingerprint", ""),
        }
    )
    encoded = json.dumps(
        final, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    fd, temp_name = tempfile.mkstemp(
        prefix=".batch-final-note.", dir=Path(final_path).parent
    )
    try:
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        os.replace(temp_name, final_path)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)


def build_confirmation(tree: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    validate_chapter_tree(tree)
    chapter = _find_chapter(tree["chapters"], chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")
    return {
        "schema_version": 1,
        "revision": 1,
        "action": "confirm",
        "chapter_id": chapter_id,
        "input_fingerprint": tree["input_fingerprint"],
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "evidence_fingerprint": tree["evidence_fingerprint"],
        "chapter": chapter,
        "chapter_fingerprint": _chapter_fingerprint(chapter),
    }


def _find_chapter(chapters: list[dict[str, Any]], chapter_id: str) -> dict[str, Any] | None:
    for chapter in chapters:
        if chapter.get("id") == chapter_id:
            return chapter
        child = _find_chapter(chapter.get("children", []), chapter_id)
        if child is not None:
            return child
    return None


def count_pdf_pages(pdf_path: str | Path) -> int:
    try:
        reader = PdfReader(str(pdf_path), strict=True)
        pages = len(reader.pages)
    except Exception as exc:
        raise ValueError("教材 PDF 无法读取") from exc
    if pages < 1 or pages > 10_000:
        raise ValueError("教材 PDF 页数无效")
    return pages


class OcrWorkflow:
    """Durable application workflow around the already-gated OCR orchestrator."""

    def __init__(
        self,
        *,
        source_path: str | Path,
        state_root: str | Path,
        orchestrator_factory: Callable[[Callable[[], bool]], OcrOrchestrator],
    ):
        self.source_path = Path(source_path).expanduser().resolve()
        self.paths = workflow_paths(state_root)
        self._cancel = threading.Event()
        self._factory = orchestrator_factory
        self._thread: threading.Thread | None = None
        self._status = WorkflowStatus.IDLE
        self._error: str | None = None
        self._lock = threading.Lock()

    def start(self, *, dpi: int = 300, languages: tuple[str, ...] = ("zh-Hans", "en-US")) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("OCR 任务正在运行")
            self._cancel.clear()
            self._error = None
            self._status = WorkflowStatus.RUNNING
            self._thread = threading.Thread(
                target=self._run, args=(dpi, languages), daemon=True, name="pdf2md-ocr"
            )
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = self._status
            error = self._error
        if status is WorkflowStatus.IDLE:
            persisted = self._persisted_status()
            if persisted is not None:
                status, error = persisted
        return status_payload(
            status=status,
            source_path=self.source_path,
            state_root=self.paths.root,
            error=error,
        )

    def _run(self, dpi: int, languages: tuple[str, ...]) -> None:
        try:
            pages = list(range(1, count_pdf_pages(self.source_path) + 1))
            orchestrator = self._factory(self._cancel.is_set)
            result = orchestrator.run_batch(
                self.source_path,
                pages=pages,
                dpi=dpi,
                languages=languages,
            )
            with self._lock:
                self._status = _workflow_status(result.status)
                self._error = result.error
        except WorkflowBlockedError as exc:
            with self._lock:
                self._status = WorkflowStatus.BLOCKED
                self._error = _safe_error(exc)
        except Exception as exc:
            with self._lock:
                self._status = (
                    WorkflowStatus.CANCELLED
                    if self._cancel.is_set()
                    else WorkflowStatus.FAILED
                )
                self._error = _safe_error(exc)

    def detect_chapters(self) -> dict[str, Any]:
        with self._lock:
            current = self._status
        if (
            current is WorkflowStatus.IDLE
            and self._persisted_status() == (WorkflowStatus.COMPLETED, None)
        ):
            current = WorkflowStatus.COMPLETED
        if current is not WorkflowStatus.COMPLETED:
            raise ValueError("OCR 尚未完成，不能识别章节")
        final = _load_json(self.paths.final)
        fingerprint = _input_fingerprint(final)
        pages = []
        for key in sorted(final["pages"], key=int):
            record = dict(final["pages"][key])
            record["page_input_fingerprint"] = fingerprint
            pages.append(record)
        tree = detect_chapter_tree(pages, input_fingerprint=fingerprint)
        _atomic_json(self.paths.chapter_tree, tree)
        return tree

    def _persisted_status(self) -> tuple[WorkflowStatus, str | None] | None:
        for path in (self.paths.final, self.paths.state):
            if not path.is_file():
                continue
            try:
                value = _load_json(path)
            except ValueError:
                return WorkflowStatus.BLOCKED, "OCR 状态文件无法验证，请重试"
            raw = value.get("status")
            if raw == WorkflowStatus.COMPLETED.value and path == self.paths.final:
                return WorkflowStatus.COMPLETED, None
            if (
                raw in {item.value for item in WorkflowStatus}
                and raw != WorkflowStatus.RUNNING.value
            ):
                return WorkflowStatus(raw), value.get("error")
            if raw == WorkflowStatus.RUNNING.value:
                return WorkflowStatus.BLOCKED, "上次 OCR 未完成，请重试"
        return None

    def confirm_chapter(self, chapter_id: str) -> dict[str, Any]:
        tree = _load_json(self.paths.chapter_tree)
        confirmation = build_confirmation(tree, chapter_id)
        persist_chapter_confirmation(self.paths.confirmation, confirmation)
        return confirmation


def _workflow_status(status: BatchStatus) -> WorkflowStatus:
    return WorkflowStatus(status.value)


def _input_fingerprint(final: dict[str, Any]) -> str:
    value = final.get("input_fingerprint")
    if not isinstance(value, str) or not value:
        raise ValueError("OCR 输入指纹缺失")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OCR 证据文件无法读取") from exc
    if not isinstance(value, dict):
        raise ValueError("OCR 证据文件格式无效")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_error(exc: Exception) -> str:
    return str(exc) if str(exc) and len(str(exc)) <= 240 else "OCR 任务失败，请查看日志后重试"
