"""Application-facing bridge for the unattended OCR and intensive-reading flow."""

from __future__ import annotations

import json
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
from .orchestrator import BatchStatus, OcrOrchestrator


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
    published = (
        status is WorkflowStatus.COMPLETED and paths.final.is_file() and paths.note.is_file()
    )
    return {
        "status": status.value,
        "source_path": str(Path(source_path).expanduser()),
        "state_path": str(paths.state),
        "error": error,
        "publishable": published,
        "markdown_path": str(paths.note) if published else None,
        "chapter_tree_path": str(paths.chapter_tree) if paths.chapter_tree.is_file() else None,
    }


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
