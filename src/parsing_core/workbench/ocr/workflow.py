"""Application-facing bridge for the unattended OCR and intensive-reading flow."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .chapters import (
    _chapter_fingerprint,
    detect_chapter_tree,
    load_chapter_confirmation,
    persist_chapter_confirmation,
    validate_chapter_confirmation,
    validate_chapter_tree,
)
from .markdown_notes import validate_mermaid_block
from .orchestrator import BatchStatus, OcrOrchestrator, _snapshot_pdf

_MARKDOWN_FENCE_RE = re.compile(r"```mermaid\n([\s\S]*?)\n```", re.MULTILINE)
_PUBLISHED_NOTE_RE = re.compile(r"^intensive-reading\.([0-9a-f]{64})\.md$")
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PUBLICATION_MANIFEST_FIELDS = {
    "schema_version",
    "final_snapshot_sha256",
    "artifact_basename",
    "artifact_sha256",
    "proposal_fingerprint",
    "metadata",
}
_PUBLICATION_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_PUBLICATION_THREAD_LOCKS_GUARD = threading.Lock()
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
    publication: Path
    publication_lock: Path
    chapter_tree: Path
    confirmation: Path
    note: Path


def workflow_paths(root: str | Path) -> WorkflowPaths:
    root_path = Path(root).expanduser().resolve()
    return WorkflowPaths(
        root=root_path,
        state=root_path / "batch-state.json",
        final=root_path / "batch-final.json",
        publication=root_path / "note-publication.json",
        publication_lock=root_path / ".note-publication.lock",
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
    published_path: Path | None = None
    if status is WorkflowStatus.COMPLETED:
        if completed_final is None:
            status = WorkflowStatus.BLOCKED
            error = "ocr_evidence_invalid"
        else:
            published, error, published_path = _publication_status(completed_final, paths)
    return {
        "status": status.value,
        "source_path": str(Path(source_path).expanduser()),
        "state_path": str(paths.state),
        "error": error,
        "publishable": published,
        "markdown_path": str(published_path) if published_path is not None else None,
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


def _publication_status(
    final: dict[str, Any], paths: WorkflowPaths
) -> tuple[bool, str | None, Path | None]:
    try:
        publication = _read_regular_json(paths.publication)
    except FileNotFoundError:
        return _legacy_publication_status(final, paths)
    except (OSError, ValueError):
        return False, "ocr_publication_invalid", None
    try:
        if (
            set(publication) != _PUBLICATION_MANIFEST_FIELDS
            or publication.get("schema_version") != 1
        ):
            raise ValueError("publication manifest is invalid")
        artifact_basename = publication.get("artifact_basename")
        artifact_sha256 = publication.get("artifact_sha256")
        metadata = publication.get("metadata")
        if not isinstance(artifact_basename, str):
            raise ValueError("publication manifest is invalid")
        match = _PUBLISHED_NOTE_RE.fullmatch(artifact_basename)
        if (
            match is None
            or not isinstance(artifact_sha256, str)
            or match.group(1) != artifact_sha256
            or not isinstance(metadata, dict)
        ):
            raise ValueError("publication manifest is invalid")
        input_fingerprint = _input_fingerprint(final)
        expected_tree = detect_chapter_tree(
            _normalized_completed_pages(final), input_fingerprint=input_fingerprint
        )
        if publication.get("proposal_fingerprint") != expected_tree["proposal_fingerprint"]:
            raise ValueError("publication proposal is invalid")
        chapter_id = metadata.get("chapter_id")
        chapter = (
            _find_chapter(expected_tree["chapters"], chapter_id)
            if isinstance(chapter_id, str)
            else None
        )
        if chapter is None or metadata.get("chapter_fingerprint") != _chapter_fingerprint(chapter):
            raise ValueError("publication chapter is invalid")
        artifact_path = paths.root / artifact_basename
        content = _read_regular_bytes(artifact_path)
        markdown = content.decode("utf-8")
        current_final = _read_regular_json(paths.final)
        if (
            publication.get("final_snapshot_sha256") != _json_fingerprint(final)
            or _json_fingerprint(current_final) != _json_fingerprint(final)
            or hashlib.sha256(content).hexdigest() != artifact_sha256
            or metadata.get("evidence_fingerprint") != expected_tree["evidence_fingerprint"]
            or not _markdown_publication_is_valid(
                metadata,
                markdown,
                input_fingerprint,
                expected_sha256=artifact_sha256,
            )
        ):
            raise ValueError("publication binding is invalid")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False, "ocr_publication_invalid", None
    return True, None, artifact_path


def _legacy_publication_status(
    final: dict[str, Any], paths: WorkflowPaths
) -> tuple[bool, str | None, Path | None]:
    legacy_fields = {
        "markdown_sha256",
        "model",
        "ruleset",
        "prompt_fingerprint",
        "chapter_fingerprint",
        "note_input_fingerprint",
        "note_evidence_fingerprint",
    }
    has_legacy_metadata = any(field in final for field in legacy_fields)
    try:
        markdown = _read_regular_bytes(paths.note).decode("utf-8")
        current_final = _read_regular_json(paths.final)
    except FileNotFoundError:
        if has_legacy_metadata:
            return False, "ocr_publication_invalid", None
        return False, None, None
    except (OSError, UnicodeError, ValueError):
        return False, "ocr_publication_invalid", None
    if (
        not has_legacy_metadata
        or _json_fingerprint(current_final) != _json_fingerprint(final)
        or not _legacy_markdown_publication_is_valid(final, markdown, _input_fingerprint(final))
    ):
        return False, "ocr_publication_invalid", None
    return True, None, paths.note


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


def _write_all(fd: int, data: bytes) -> None:
    written = 0
    while written < len(data):
        count = os.write(fd, data[written:])
        if count <= 0:
            raise OSError("artifact write made no progress")
        written += count


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_immutable_artifact(root: Path, content: bytes, digest: str) -> Path:
    target = root / f"intensive-reading.{digest}.md"
    fd, temporary_name = tempfile.mkstemp(prefix=".note-artifact.", dir=root)
    temporary = Path(temporary_name)
    try:
        _write_all(fd, content)
        os.fsync(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("publication artifact temporary file is unsafe")
        os.close(fd)
        fd = -1
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if _read_regular_bytes(target) != content:
                raise ValueError("publication artifact hash collision") from None
        else:
            temporary.unlink()
            _fsync_directory(root)
        if _read_regular_bytes(target) != content:
            raise ValueError("publication artifact is invalid")
        return target
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _publication_thread_lock(root: Path) -> threading.RLock:
    with _PUBLICATION_THREAD_LOCKS_GUARD:
        lock = _PUBLICATION_THREAD_LOCKS.get(root)
        if lock is None:
            lock = threading.RLock()
            _PUBLICATION_THREAD_LOCKS[root] = lock
        return lock


def _open_publication_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        flags |= no_follow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValueError("publication lock cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        path_info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _MAX_ARTIFACT_BYTES
            or not stat.S_ISREG(path_info.st_mode)
            or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("publication lock is not a safe regular file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_locked_file(path: Path, fd: int) -> None:
    info = os.fstat(fd)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ValueError("publication lock changed while acquiring") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not stat.S_ISREG(path_info.st_mode)
        or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
    ):
        raise ValueError("publication lock changed while acquiring")


@contextmanager
def _publication_transaction_lock(paths: WorkflowPaths) -> Iterator[None]:
    paths.root.mkdir(parents=True, exist_ok=True)
    with _publication_thread_lock(paths.root):
        fd = _open_publication_lock(paths.publication_lock)
        locked = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            _validate_locked_file(paths.publication_lock, fd)
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@contextmanager
def _temporary_note_path(root: Path) -> Iterator[Path]:
    fd, name = tempfile.mkstemp(prefix=".intensive-reading.", suffix=".tmp.md", dir=root)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _json_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _markdown_publication_is_valid(
    metadata: dict[str, Any],
    markdown: str,
    input_fingerprint: str,
    *,
    expected_sha256: str,
) -> bool:
    if not markdown.endswith("\n") or "待由 DeepSeek" in markdown:
        return False
    if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != expected_sha256:
        return False
    if (
        metadata.get("model") != "deepseek-v4-pro"
        or metadata.get("prompt_rules_version") != "mba-intensive-reading-v1"
    ):
        return False
    if metadata.get("input_fingerprint") != input_fingerprint:
        return False
    chapter_fingerprint = metadata.get("chapter_fingerprint")
    evidence_fingerprint = metadata.get("evidence_fingerprint")
    prompt_fingerprint = metadata.get("prompt_fingerprint")
    chapter_id = metadata.get("chapter_id")
    if not all(
        isinstance(value, str) and value
        for value in (chapter_fingerprint, evidence_fingerprint, prompt_fingerprint, chapter_id)
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


def _legacy_markdown_publication_is_valid(
    final: dict[str, Any], markdown: str, input_fingerprint: str
) -> bool:
    if not markdown.endswith("\n") or "待由 DeepSeek" in markdown:
        return False
    if final.get("markdown_sha256") != hashlib.sha256(markdown.encode("utf-8")).hexdigest():
        return False
    if (
        final.get("model") != "deepseek-v4-pro"
        or final.get("ruleset") != "mba-intensive-reading-v1"
        or final.get("note_input_fingerprint") != input_fingerprint
    ):
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
    expected_tree: dict[str, Any] | None = None,
    confirmation: dict[str, Any] | None = None,
    publication_path: str | Path | None = None,
) -> Path:
    """Atomically bind a note to immutable OCR and chapter snapshots."""
    final_target = Path(final_path)
    note_target = Path(note_path)
    publication_target = (
        Path(publication_path)
        if publication_path is not None
        else final_target.with_name("note-publication.json")
    )
    state_root = publication_target.parent
    if final_target.parent != state_root or note_target.parent != state_root:
        raise ValueError("published note must stay inside the OCR state root")
    if not isinstance(expected_final, dict):
        raise ValueError("expected OCR final is required")
    final = _read_regular_json(final_target)
    if final != expected_final:
        raise ValueError("OCR final changed during note generation")

    pages = _normalized_completed_pages(expected_final)
    detected_tree = detect_chapter_tree(pages, input_fingerprint=_input_fingerprint(expected_final))
    if expected_tree is None:
        expected_tree = _read_regular_json(final_target.with_name("chapter-tree.json"))
    if expected_tree != detected_tree:
        raise ValueError("published chapter tree fingerprint is invalid")
    if confirmation is None:
        confirmation = load_chapter_confirmation(
            final_target.with_name("chapter-confirmation.json")
        )
    validate_chapter_confirmation(confirmation, expected_tree)
    if confirmation.get("action") != "confirm":
        raise ValueError("published chapter confirmation is invalid")

    if not isinstance(metadata, dict) or metadata.get("model") != "deepseek-v4-pro":
        raise ValueError("published model is invalid")
    if metadata.get("prompt_rules_version") != "mba-intensive-reading-v1":
        raise ValueError("published ruleset is invalid")
    if metadata.get("input_fingerprint") != expected_final.get("input_fingerprint"):
        raise ValueError("published input fingerprint is invalid")
    if metadata.get("chapter_fingerprint") != confirmation.get("chapter_fingerprint"):
        raise ValueError("published chapter fingerprint is invalid")
    if metadata.get("evidence_fingerprint") != expected_tree.get("evidence_fingerprint"):
        raise ValueError("published evidence fingerprint is invalid")
    if metadata.get("chapter_id") != confirmation.get("chapter_id"):
        raise ValueError("published chapter id is invalid")
    prompt_fingerprint = metadata.get("prompt_fingerprint")
    if not isinstance(prompt_fingerprint, str) or not prompt_fingerprint:
        raise ValueError("published prompt fingerprint is invalid")

    content = _read_regular_bytes(note_target)
    try:
        markdown = content.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("published markdown is invalid") from exc
    artifact_sha256 = hashlib.sha256(content).hexdigest()
    if not _markdown_publication_is_valid(
        metadata,
        markdown,
        metadata["input_fingerprint"],
        expected_sha256=artifact_sha256,
    ):
        raise ValueError("published markdown is invalid")
    artifact_path = _publish_immutable_artifact(state_root, content, artifact_sha256)
    try:
        manifest_metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("published metadata is invalid") from exc
    if not isinstance(manifest_metadata, dict):
        raise ValueError("published metadata is invalid")
    publication = {
        "schema_version": 1,
        "final_snapshot_sha256": _json_fingerprint(expected_final),
        "artifact_basename": artifact_path.name,
        "artifact_sha256": artifact_sha256,
        "proposal_fingerprint": expected_tree["proposal_fingerprint"],
        "metadata": manifest_metadata,
    }

    try:
        previous_manifest = _read_regular_bytes(publication_target)
    except FileNotFoundError:
        previous_manifest = None
    if _read_regular_json(final_target) != expected_final:
        raise ValueError("OCR final changed during note generation")
    try:
        _atomic_json(publication_target, publication)
        if _read_regular_json(final_target) != expected_final:
            raise ValueError("OCR final changed during note generation")
        if _read_regular_json(publication_target) != publication:
            raise ValueError("OCR publication changed during note generation")
        if _read_regular_bytes(artifact_path) != content:
            raise ValueError("OCR publication artifact changed during note generation")
    except Exception:
        _restore_previous_manifest(
            publication_target,
            candidate=publication,
            previous=previous_manifest,
        )
        raise
    return artifact_path


def _restore_previous_manifest(
    path: Path,
    *,
    candidate: dict[str, Any],
    previous: bytes | None,
) -> None:
    try:
        current = _read_regular_json(path)
    except FileNotFoundError:
        return
    if current != candidate:
        return
    if previous is None:
        _validate_atomic_target(path)
        path.unlink()
        _fsync_directory(path.parent)
        return
    _atomic_bytes(path, previous)


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
        self._lock = threading.RLock()

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
        with self._lock:
            final, pages = self.completed_evidence()
            fingerprint = _input_fingerprint(final)
            tree = detect_chapter_tree(pages, input_fingerprint=fingerprint)
            _atomic_json(self.paths.chapter_tree, tree)
            return tree

    def completed_evidence(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._lock:
            status, _error, final = self._effective_status()
            if status is not WorkflowStatus.COMPLETED or final is None:
                raise ValueError("OCR 尚未完成，不能读取证据")
            return final, _normalized_completed_pages(final)

    def completed_chapter_context(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        with self._lock:
            final, pages = self.completed_evidence()
            tree = _read_regular_json(self.paths.chapter_tree)
            expected = detect_chapter_tree(pages, input_fingerprint=_input_fingerprint(final))
            if tree != expected:
                raise ValueError("章节树与当前 OCR 证据不一致")
            return final, pages, tree

    def generate_and_publish(
        self,
        generate: Callable[[Path], dict[str, Any]],
        *,
        expected_final: dict[str, Any],
        expected_tree: dict[str, Any],
        confirmation: dict[str, Any],
    ) -> tuple[dict[str, Any], Path]:
        with self._lock:
            with _publication_transaction_lock(self.paths):
                current_final, pages = self.completed_evidence()
                if current_final != expected_final:
                    raise ValueError("OCR final changed during note generation")
                detected_tree = detect_chapter_tree(
                    pages, input_fingerprint=_input_fingerprint(current_final)
                )
                if expected_tree != detected_tree:
                    raise ValueError("published chapter tree fingerprint is invalid")
                validate_chapter_confirmation(confirmation, expected_tree)
                if confirmation.get("action") != "confirm":
                    raise ValueError("published chapter confirmation is invalid")
                with _temporary_note_path(self.paths.root) as temporary_path:
                    note = generate(temporary_path)
                    if not isinstance(note, dict):
                        raise ValueError("generated note is invalid")
                    metadata = note.get("metadata")
                    markdown = note.get("markdown")
                    try:
                        persisted_markdown = _read_regular_bytes(temporary_path).decode("utf-8")
                    except (OSError, UnicodeError, ValueError) as exc:
                        raise ValueError("generated note artifact is invalid") from exc
                    if (
                        not isinstance(metadata, dict)
                        or not isinstance(markdown, str)
                        or markdown != persisted_markdown
                    ):
                        raise ValueError("generated note does not match its artifact")
                    artifact_path = bind_published_note(
                        self.paths.final,
                        temporary_path,
                        metadata,
                        expected_final=expected_final,
                        expected_tree=expected_tree,
                        confirmation=confirmation,
                        publication_path=self.paths.publication,
                    )
                    published, error, published_path = _publication_status(
                        expected_final, self.paths
                    )
                    if not published or error is not None or published_path != artifact_path:
                        raise ValueError("OCR publication failed post-commit validation")
                    return note, artifact_path

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
        with self._lock:
            _final, _pages, tree = self.completed_chapter_context()
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


def _validate_atomic_target(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("atomic target cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("atomic target is not a safe regular file")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    _atomic_bytes(path, encoded)


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_atomic_target(path)
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError("atomic artifact is too large")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        _write_all(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)


def _safe_error(exc: Exception) -> str:
    return str(exc) if str(exc) and len(str(exc)) <= 240 else "OCR 任务失败，请查看日志后重试"
