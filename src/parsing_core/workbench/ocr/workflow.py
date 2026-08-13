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
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
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


def restored_workflow_status(
    payload: dict[str, object],
) -> tuple[WorkflowStatus, str | None]:
    raw = payload.get("status")
    try:
        status = WorkflowStatus(str(raw))
    except ValueError:
        return WorkflowStatus.BLOCKED, "ocr_state_invalid"
    error = payload.get("error")
    safe_error = error if isinstance(error, str) and 0 < len(error) <= 240 else None
    return status, safe_error


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
    completed_final = None
    if status is WorkflowStatus.COMPLETED:
        try:
            completed_final = _read_completed_ocr_final(paths.final, source_path)
        except (OSError, ValueError):
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
    return _status_payload_from_snapshot(
        status=status,
        source_path=source_path,
        paths=paths,
        error=error,
        completed_final=completed_final,
    )


def _status_payload_from_snapshot(
    *,
    status: WorkflowStatus,
    source_path: str | Path,
    paths: WorkflowPaths,
    error: str | None,
    completed_final: dict[str, Any] | None,
) -> dict[str, Any]:
    published = False
    if status is WorkflowStatus.COMPLETED:
        if completed_final is None:
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
        else:
            published, error = _publication_status(completed_final, paths.note)
    return {
        "status": status.value,
        "source_path": str(Path(source_path).expanduser()),
        "state_path": str(paths.state),
        "error": error,
        "publishable": published,
        "markdown_path": str(paths.note) if published else None,
        "chapter_tree_path": str(paths.chapter_tree) if paths.chapter_tree.is_file() else None,
    }


def _read_completed_ocr_final(final_path: Path, source_path: str | Path) -> dict[str, Any]:
    final = _read_regular_json(final_path)
    if not _completed_ocr_final_is_valid(final, source_path):
        raise ValueError("OCR final evidence is invalid")
    return final


def _completed_ocr_final_is_valid(final: dict[str, Any], source_path: str | Path) -> bool:
    try:
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
            vision=None, codex=None, baidu=None, state_root=Path(source_path).parent
        )
        for page in page_numbers:
            record = pages[str(page)]
            if not isinstance(record, dict) or record.get("status") != "completed":
                return False
            alignment = record.get("alignment")
            if not isinstance(alignment, dict):
                return False
            sample_rate = 0.05 if alignment.get("baidu_required") else 0.0
            if not validator._completed_evidence_is_valid(final, record, page, sample_rate):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _publication_status(final: dict[str, Any], note_path: Path) -> tuple[bool, str | None]:
    try:
        markdown = _read_regular_bytes(note_path).decode("utf-8")
    except FileNotFoundError:
        return False, None
    except (OSError, UnicodeError, ValueError):
        return False, "ocr_publication_invalid"
    input_fingerprint = final.get("input_fingerprint")
    if not isinstance(input_fingerprint, str) or not _markdown_publication_is_valid(
        final, markdown, input_fingerprint
    ):
        return False, "ocr_publication_invalid"
    return True, None


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    before_path = None
    if no_follow:
        flags |= no_follow
    else:
        try:
            before_path = path.lstat()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("artifact cannot be inspected") from exc
        if not stat.S_ISREG(before_path.st_mode):
            raise ValueError("artifact is not a regular file")
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("artifact cannot be opened safely") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_ARTIFACT_BYTES
        ):
            raise ValueError("artifact is not a safe regular file")
        if before_path is not None:
            after_path = path.lstat()
            if (
                not stat.S_ISREG(after_path.st_mode)
                or (before_path.st_dev, before_path.st_ino) != (before.st_dev, before.st_ino)
                or (after_path.st_dev, after_path.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ValueError("artifact changed while opening")
        chunks = []
        total = 0
        while True:
            chunk = os.read(
                fd,
                min(_READ_CHUNK_BYTES, _MAX_ARTIFACT_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_ARTIFACT_BYTES:
                raise ValueError("artifact is too large")
        after = os.fstat(fd)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError("artifact changed while reading")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise ValueError("artifact size changed while reading")
        return data
    except OSError as exc:
        raise ValueError("artifact cannot be read safely") from exc
    finally:
        os.close(fd)


def _read_regular_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("artifact JSON is invalid")
    return value


def _markdown_publication_is_valid(
    final: dict[str, Any], markdown: str, input_fingerprint: str
) -> bool:
    if not markdown.endswith("\n") or "待由 DeepSeek" in markdown:
        return False
    if final.get("markdown_sha256") != hashlib.sha256(markdown.encode("utf-8")).hexdigest():
        return False
    if (
        final.get("model") != "deepseek-v4-pro"
        or final.get("ruleset") != "mba-intensive-reading-v1"
    ):
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
    final_path: str | Path,
    note_path: str | Path,
    metadata: dict[str, Any],
    *,
    expected_final: dict[str, Any] | None = None,
) -> None:
    """Bind the generated note artifact to the completed OCR evidence."""
    final = _read_regular_json(Path(final_path))
    if expected_final is not None and final != expected_final:
        raise ValueError("OCR final changed during note generation")
    content = _read_regular_bytes(Path(note_path))
    if not isinstance(metadata, dict) or metadata.get("model") != "deepseek-v4-pro":
        raise ValueError("published model is invalid")
    if metadata.get("prompt_rules_version") != "mba-intensive-reading-v1":
        raise ValueError("published ruleset is invalid")
    if metadata.get("input_fingerprint") != final.get("input_fingerprint"):
        raise ValueError("published input fingerprint is invalid")
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
    encoded = json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fd, temp_name = tempfile.mkstemp(prefix=".batch-final-note.", dir=Path(final_path).parent)
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
        status, error, completed_final = self._effective_status()
        return _status_payload_from_snapshot(
            status=status,
            source_path=self.source_path,
            paths=self.paths,
            error=error,
            completed_final=completed_final,
        )

    def _effective_status(
        self,
    ) -> tuple[WorkflowStatus, str | None, dict[str, Any] | None]:
        with self._lock:
            status = self._status
            error = self._error
        if status is WorkflowStatus.IDLE:
            return self._persisted_status()
        if status is WorkflowStatus.COMPLETED:
            try:
                final = _read_completed_ocr_final(self.paths.final, self.source_path)
            except (OSError, ValueError):
                return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
            return WorkflowStatus.COMPLETED, None, final
        return status, error, None

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
                    WorkflowStatus.CANCELLED if self._cancel.is_set() else WorkflowStatus.FAILED
                )
                self._error = _safe_error(exc)

    def detect_chapters(self) -> dict[str, Any]:
        final, pages = self.completed_evidence()
        fingerprint = _input_fingerprint(final)
        tree = detect_chapter_tree(pages, input_fingerprint=fingerprint)
        _atomic_json(self.paths.chapter_tree, tree)
        return tree

    def completed_evidence(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        status, _error, final = self._effective_status()
        if status is not WorkflowStatus.COMPLETED or final is None:
            raise ValueError("OCR 尚未完成，不能读取证据")
        return final, _normalized_completed_pages(final)

    def _persisted_status(
        self,
    ) -> tuple[WorkflowStatus, str | None, dict[str, Any] | None]:
        try:
            final = _read_completed_ocr_final(self.paths.final, self.source_path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
        else:
            return WorkflowStatus.COMPLETED, None, final

        try:
            value = _read_regular_json(self.paths.state)
        except FileNotFoundError:
            return WorkflowStatus.IDLE, None, None
        except (OSError, ValueError):
            return WorkflowStatus.BLOCKED, "ocr_state_invalid", None
        status, error = restored_workflow_status(value)
        if status is WorkflowStatus.RUNNING:
            return WorkflowStatus.BLOCKED, "ocr_state_interrupted", None
        if status is WorkflowStatus.COMPLETED:
            return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
        return status, error, None

    def confirm_chapter(self, chapter_id: str) -> dict[str, Any]:
        tree = _load_json(self.paths.chapter_tree)
        confirmation = build_confirmation(tree, chapter_id)
        persist_chapter_confirmation(self.paths.confirmation, confirmation)
        return confirmation


def _workflow_status(status: BatchStatus) -> WorkflowStatus:
    return WorkflowStatus(status.value)


def _normalized_completed_pages(final: dict[str, Any]) -> list[dict[str, Any]]:
    fingerprint = _input_fingerprint(final)
    raw_pages = final.get("pages")
    if not isinstance(raw_pages, dict):
        raise ValueError("OCR 页证据缺失")
    pages = []
    for key in sorted(raw_pages, key=int):
        raw_record = raw_pages[key]
        if not isinstance(raw_record, dict):
            raise ValueError("OCR 页证据无效")
        record = dict(raw_record)
        record["page"] = int(key)
        record["page_input_fingerprint"] = fingerprint
        pages.append(record)
    return pages


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
