"""Application-facing bridge for the unattended OCR and intensive-reading flow."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
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
    persist_chapter_confirmation,
    validate_chapter_confirmation,
    validate_chapter_tree,
)
from .markdown_notes import validate_mermaid_block
from .orchestrator import (
    BatchStatus,
    OcrOrchestrator,
    _interrupted_run_config,
    _is_batch_state,
    _snapshot_pdf,
)

_MARKDOWN_FENCE_RE = re.compile(r"```mermaid\n([\s\S]*?)\n```", re.MULTILINE)
_PUBLISHED_NOTE_RE = re.compile(r"^intensive-reading\.([0-9a-f]{64})\.md$")
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PUBLICATION_STATUS_RETRIES = 3
_PUBLICATION_MANIFEST_FIELDS = {
    "schema_version",
    "final_snapshot_sha256",
    "artifact_basename",
    "artifact_sha256",
    "proposal_fingerprint",
    "metadata",
}
_PUBLICATION_METADATA_FIELDS = {
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
_PUBLICATION_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_PUBLICATION_THREAD_LOCKS_GUARD = threading.Lock()
_OCR_WORKER_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_OCR_WORKER_THREAD_LOCKS_GUARD = threading.Lock()
_ACTIVE_OCR_WORKER_CLAIMS: dict[Path, _OcrWorkerClaim] = {}
_ACTIVE_OCR_WORKER_CLAIMS_GUARD = threading.Lock()
_OCR_WORKER_LOCK_BASENAME = ".ocr-worker.lock"
_LOCK_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_LOCK_TOKEN_PREFIX_RE = re.compile(rb"[0-9a-f]{0,64}")
_LOCK_RECOVERY_EVIDENCE_BASENAMES = {
    "ocr-v1-v2-migration.json",
    "batch-final.v1.backup.json",
    "batch-state.v1.backup.json",
}
_LOCK_RECOVERY_EVIDENCE_PREFIX = ".ocr-v1-v2-"
_MAX_LOCK_RECOVERY_DIRECTORY_ENTRIES = 4096
_AT_FDCWD = -2
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_LEGACY_V1_CORE_FIELDS = {
    "schema_version",
    "status",
    "input_fingerprint",
    "pdf_snapshot",
    "pages",
    "updated_at",
    "error",
}
_LEGACY_V1_PUBLICATION_FIELDS = {
    "markdown_sha256",
    "model",
    "ruleset",
    "prompt_fingerprint",
    "chapter_fingerprint",
    "note_input_fingerprint",
    "note_evidence_fingerprint",
}
_LEGACY_V1_SAMPLE_RATES: tuple[int | float, ...] = (0, 0.05)
_LEGACY_CITATION_RE = re.compile(
    r"\[src:([A-Za-z0-9._-]{1,128}):p[1-9][0-9]{0,4}:[^\]\r\n]{1,128}\]"
)
_LEGACY_MIGRATION_JOURNAL_BASENAME = "ocr-v1-v2-migration.json"
_LEGACY_MIGRATION_JOURNAL_FIELDS = {
    "schema_version",
    "transaction_id",
    "transaction_anchor_basename",
    "transaction_anchor_dev",
    "transaction_anchor_ino",
    "transaction_anchor_sha256",
    "kind",
    "phase",
    "journal_self_dev",
    "journal_self_ino",
    "journal_self_sha256",
    "lock_dev",
    "lock_ino",
    "lock_token",
    "source_basename",
    "source_sha256",
    "source_size",
    "backup_basename",
    "backup_dev",
    "backup_ino",
    "journal_previous_sha256",
    "journal_previous_dev",
    "journal_previous_ino",
    "target_sha256",
    "target_dev",
    "target_ino",
    "swap_path_basename",
    "swap_replacement_dev",
    "swap_replacement_ino",
    "swap_expected_dev",
    "swap_expected_ino",
    "swap_displaced_dev",
    "swap_displaced_ino",
    "artifact_sha256",
    "artifact_dev",
    "artifact_ino",
    "publication_sha256",
    "manifest_candidate_basename",
    "manifest_candidate_dev",
    "manifest_candidate_ino",
    "manifest_dev",
    "manifest_ino",
    "sidecar_transaction_tokens",
}
_LEGACY_MIGRATION_PHASES = {
    "completed": (
        "intent",
        "prepared",
        "backup_published",
        "artifact_published",
        "source_swap_prepared",
        "final_published",
        "manifest_linked",
        "manifest_published",
        "commit_ready",
    ),
    "running": (
        "intent",
        "prepared",
        "backup_published",
        "source_swap_prepared",
        "state_published",
        "commit_ready",
    ),
}
_MIGRATION_SIDECAR_LABELS = (
    "anchor",
    "manifest",
    "backup",
    "artifact",
    "swap",
    "commit",
)
_MIGRATION_SIDECAR_STAGES = ("declared", "allocated", "ready", "published")
_MIGRATION_SIDECAR_RECEIPT_FIELDS = {
    "schema_version",
    "transaction_id",
    "transaction_token",
    "label",
    "stage",
    "sequence",
    "target_basename",
    "content_sha256",
    "allocation_basename",
    "candidate_basename",
    "capture_basename",
    "allocation_dev",
    "allocation_ino",
    "candidate_dev",
    "candidate_ino",
    "published_dev",
    "published_ino",
    "receipt_self_dev",
    "receipt_self_ino",
    "receipt_self_sha256",
    "receipt_previous_dev",
    "receipt_previous_ino",
    "receipt_previous_sha256",
}
_COMMIT_WITNESS_FIELDS = {
    "schema_version",
    "journal",
    "journal_sha256",
    "journal_dev",
    "journal_ino",
}
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
    safe_error = (
        error if isinstance(error, str) and re.fullmatch(r"ocr_[a-z0-9_]+", error) else None
    )
    return status, safe_error


class WorkflowBlockedError(RuntimeError):
    """A required local or remote engine is not configured."""


class _LegacyMigrationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoReplacePublicationUncertain(_LegacyMigrationError):
    """The no-replace rename completed, but its directory sync did not."""

    def __init__(self, code: str, published_identity: tuple[int, int]):
        super().__init__(code)
        self.published_identity = published_identity


class _MigrationJournalPublicationUncertain(_LegacyMigrationError):
    """The active journal is owned, but its first directory sync failed."""


def _migration_checkpoint(_point: str) -> None:
    """Fault-injection boundary used by migration crash-recovery tests."""


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


@dataclass(frozen=True)
class _RegularFileSnapshot:
    content: bytes
    device: int
    inode: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True)
class _MigrationCandidateOwner:
    transaction_id: str
    label: str
    transaction_token: str
    root: Path
    kind: str
    publication_lock: _PublicationLock | None = None


@dataclass(frozen=True, eq=False, slots=True)
class _PublicationLock:
    root_path: Path
    path: Path
    device: int
    inode: int
    token: str
    pid: int
    thread_id: int

    def validate(self) -> None:
        _validate_active_publication_lock(self)

    def journal_identity(self) -> dict[str, object]:
        self.validate()
        return {
            "lock_dev": self.device,
            "lock_ino": self.inode,
            "lock_token": self.token,
        }


@dataclass(frozen=True, slots=True)
class _ActivePublicationLease:
    root_path: Path
    root_fd: int
    root_device: int
    root_inode: int
    path: Path
    fd: int
    device: int
    inode: int
    token: str
    pid: int
    thread_id: int


# The guard object's identity is the in-process capability. Lock descriptors stay
# only in this private registry, so cooperative production code cannot unlock them.
# Reflective mutation inside the same Python process is outside the M0 boundary.
_ACTIVE_PUBLICATION_LEASES: dict[_PublicationLock, _ActivePublicationLease] = {}
_ACTIVE_PUBLICATION_LEASES_GUARD = threading.Lock()


@dataclass
class _OcrWorkerClaim:
    root_path: Path
    root_fd: int | None
    path: Path
    fd: int | None
    thread_lock: threading.Lock

    def validate(self) -> None:
        if self.fd is None or self.root_fd is None:
            raise ValueError("OCR worker claim is closed")
        _validate_locked_directory(self.root_path, self.root_fd)
        _validate_locked_file(self.path, self.fd)

    def release(self) -> None:
        with _ACTIVE_OCR_WORKER_CLAIMS_GUARD:
            if _ACTIVE_OCR_WORKER_CLAIMS.get(self.root_path) is self:
                del _ACTIVE_OCR_WORKER_CLAIMS[self.root_path]
        fd = self.fd
        root_fd = self.root_fd
        if fd is None or root_fd is None:
            return
        self.fd = None
        self.root_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            finally:
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
                finally:
                    os.close(root_fd)
                    self.thread_lock.release()


def workflow_paths(root: str | Path) -> WorkflowPaths:
    # Keep the final path component lexical so O_NOFOLLOW can reject a state-root
    # symlink instead of silently resolving it before the transaction lock opens it.
    root_path = Path(os.path.abspath(os.fspath(Path(root).expanduser())))
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
            _validate_finalized_migration_receipts(paths)
            completed_final = _read_completed_ocr_final(paths.final, source_path)
        except _LegacyMigrationError as exc:
            status = WorkflowStatus.BLOCKED
            error = exc.code
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
        if final.get("status") != BatchStatus.COMPLETED.value or not _is_batch_state(final):
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
        sample_rate = float(final["run_config"]["sample_rate"])
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
            if not validator._completed_evidence_is_valid(final, record, page, sample_rate):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _legacy_v1_core_is_valid(
    value: object, source_path: str | Path, *, expected_status: str
) -> bool:
    if not isinstance(value, dict):
        return False
    required = _LEGACY_V1_CORE_FIELDS - {"error"}
    allowed = _LEGACY_V1_CORE_FIELDS | _LEGACY_V1_PUBLICATION_FIELDS
    fields = set(value)
    publication_fields = fields & _LEGACY_V1_PUBLICATION_FIELDS
    if (
        value.get("schema_version") != 1
        or not required <= fields <= allowed
        or publication_fields not in (set(), _LEGACY_V1_PUBLICATION_FIELDS)
        or value.get("status") != expected_status
        or value.get("error") is not None
        or not isinstance(value.get("updated_at"), int)
    ):
        return False
    fingerprint = value.get("input_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        return False
    snapshot = value.get("pdf_snapshot")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"sha256", "size"}
        or snapshot != _snapshot_pdf(source_path)
    ):
        return False
    pages = value.get("pages")
    if not isinstance(pages, dict) or not pages:
        return False
    try:
        page_numbers = sorted(int(key) for key in pages)
    except (TypeError, ValueError):
        return False
    if (
        page_numbers != list(range(1, len(page_numbers) + 1))
        or set(pages) != {str(page) for page in page_numbers}
        or any(not isinstance(pages[str(page)], dict) for page in page_numbers)
    ):
        return False
    return True


def _legacy_v1_run_config(final: dict[str, Any]) -> dict[str, Any]:
    pages = sorted(int(key) for key in final["pages"])
    dpis: set[int] = set()
    language_configs: set[tuple[str, ...]] = set()
    for page in pages:
        record = final["pages"][str(page)]
        vision = record.get("vision") if isinstance(record, dict) else None
        if not isinstance(vision, dict):
            raise _LegacyMigrationError("ocr_evidence_invalid")
        dpi = vision.get("dpi")
        languages = vision.get("language_config")
        if (
            not isinstance(dpi, int)
            or isinstance(dpi, bool)
            or not 72 <= dpi <= 1200
            or not isinstance(languages, list | tuple)
            or not languages
            or any(not isinstance(language, str) or not language for language in languages)
        ):
            raise _LegacyMigrationError("ocr_evidence_invalid")
        dpis.add(dpi)
        language_configs.add(tuple(languages))
    if len(dpis) != 1 or len(language_configs) != 1:
        raise _LegacyMigrationError("ocr_evidence_invalid")
    dpi = next(iter(dpis))
    languages = list(next(iter(language_configs)))
    matches = []
    for sample_rate in _LEGACY_V1_SAMPLE_RATES:
        candidate = {
            "pages": pages,
            "dpi": dpi,
            "languages": languages,
            "sample_rate": sample_rate,
        }
        if _json_fingerprint({"pdf_snapshot": final["pdf_snapshot"], **candidate}) == final.get(
            "input_fingerprint"
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise _LegacyMigrationError("ocr_evidence_invalid")
    return matches[0]


def _migrate_legacy_completed_final(
    final: dict[str, Any], *, source_path: str | Path
) -> dict[str, Any]:
    if not _legacy_v1_core_is_valid(
        final, source_path, expected_status=BatchStatus.COMPLETED.value
    ):
        raise _LegacyMigrationError("ocr_evidence_invalid")
    migrated = {
        "schema_version": 2,
        "status": BatchStatus.COMPLETED.value,
        "input_fingerprint": final["input_fingerprint"],
        "pdf_snapshot": final["pdf_snapshot"],
        "run_config": _legacy_v1_run_config(final),
        "pages": final["pages"],
        "updated_at": final["updated_at"],
    }
    if not _completed_ocr_final_is_valid(migrated, source_path):
        raise _LegacyMigrationError("ocr_evidence_invalid")
    return migrated


def _legacy_publication_plan(
    legacy: dict[str, Any], migrated: dict[str, Any], paths: WorkflowPaths
) -> tuple[bytes, dict[str, Any], dict[str, Any]] | None:
    present = set(legacy) & _LEGACY_V1_PUBLICATION_FIELDS
    if not present:
        return None
    if present != _LEGACY_V1_PUBLICATION_FIELDS:
        raise _LegacyMigrationError("ocr_evidence_invalid")
    try:
        content = _read_regular_bytes(paths.note)
        markdown = content.decode("utf-8")
        if not _legacy_markdown_publication_is_valid(
            legacy, markdown, str(legacy["input_fingerprint"])
        ):
            raise ValueError
        detected_tree = detect_chapter_tree(
            _normalized_completed_pages(migrated),
            input_fingerprint=str(migrated["input_fingerprint"]),
        )
        tree, confirmation = _read_matching_chapter_context(
            paths,
            expected_tree=detected_tree,
            expected_confirmation=None,
        )
        chapter = confirmation.get("chapter")
        if (
            confirmation.get("action") not in {"confirm", "edit"}
            or not isinstance(chapter, dict)
            or legacy.get("chapter_fingerprint") != _chapter_fingerprint(chapter)
            or legacy.get("note_evidence_fingerprint") != tree["evidence_fingerprint"]
        ):
            raise ValueError
        citation_matches = list(_LEGACY_CITATION_RE.finditer(markdown))
        source_ids = {match.group(1) for match in citation_matches}
        citation_ids = list(dict.fromkeys(match.group(0) for match in citation_matches))
        if len(source_ids) != 1 or not citation_ids:
            raise ValueError
        metadata = {
            "input_fingerprint": migrated["input_fingerprint"],
            "chapter_fingerprint": legacy["chapter_fingerprint"],
            "evidence_fingerprint": legacy["note_evidence_fingerprint"],
            "prompt_rules_version": legacy["ruleset"],
            "source_id": next(iter(source_ids)),
            "chapter_id": chapter["id"],
            "chapter_number": chapter["number"],
            "chapter_title": chapter["title"],
            "page_start": chapter["page_start"],
            "page_end": chapter["page_end"],
            "citation_ids": citation_ids,
            "model": legacy["model"],
            "prompt_fingerprint": legacy["prompt_fingerprint"],
        }
        _validate_publication_metadata(
            metadata,
            input_fingerprint=str(migrated["input_fingerprint"]),
            evidence_fingerprint=tree["evidence_fingerprint"],
            chapter=chapter,
        )
    except (KeyError, OSError, UnicodeError, ValueError, TypeError):
        raise _LegacyMigrationError("ocr_evidence_invalid") from None
    return content, metadata, tree


def _publication_status(
    final: dict[str, Any], paths: WorkflowPaths
) -> tuple[bool, str | None, Path | None]:
    expected_final_fingerprint = _json_fingerprint(final)
    for _attempt in range(_PUBLICATION_STATUS_RETRIES):
        try:
            final_before = _read_regular_json(paths.final)
            publication_before = _read_publication_snapshot(paths.publication)
        except (OSError, ValueError):
            return False, "ocr_publication_invalid", None
        if _json_fingerprint(final_before) != expected_final_fingerprint:
            return False, "ocr_publication_invalid", None

        result = (
            _legacy_publication_status(final, paths)
            if publication_before is None
            else _publication_manifest_status(final, paths, publication_before)
        )
        try:
            publication_after = _read_publication_snapshot(paths.publication)
            final_after = _read_regular_json(paths.final)
        except (OSError, ValueError):
            return False, "ocr_publication_invalid", None
        if _json_fingerprint(final_after) != expected_final_fingerprint:
            return False, "ocr_publication_invalid", None
        if publication_after == publication_before:
            return result
    return False, "ocr_publication_invalid", None


def _read_publication_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        return _read_regular_json(path)
    except FileNotFoundError:
        return None
    except ValueError:
        pass
    snapshot = _read_regular_snapshot(path, allowed_link_counts=(2,))
    try:
        value = json.loads(snapshot.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("artifact JSON is invalid")
    if _migration_receipt_binds_manifest(path.parent, snapshot, value):
        return value
    raise ValueError("publication manifest has an unbound hard link")


def _migration_receipt_binds_manifest(
    root: Path,
    manifest: _RegularFileSnapshot,
    publication: dict[str, Any],
) -> bool:
    pattern = re.compile(r"\.ocr-v1-v2-([0-9a-f]{64})\.sealed")
    try:
        receipt_paths = tuple(path for path in root.iterdir() if pattern.fullmatch(path.name))
    except OSError as exc:
        raise ValueError("migration receipt cannot be inspected") from exc
    matched = False
    for receipt_path in receipt_paths:
        try:
            receipt_snapshot = _read_regular_snapshot(receipt_path)
            receipt_value = json.loads(receipt_snapshot.content.decode("utf-8"))
            receipt = _validate_commit_witness(receipt_value)
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            _LegacyMigrationError,
        ) as exc:
            raise ValueError("migration receipt is invalid") from exc
        journal = receipt["journal"]
        paths = workflow_paths(root)
        if receipt_path != _commit_sealed_path(paths, journal):
            raise ValueError("migration receipt path is invalid")
        try:
            _validate_finalized_migration_receipt(
                paths,
                receipt_path,
                receipt_snapshot,
                receipt,
            )
        except _LegacyMigrationError as exc:
            raise ValueError("migration receipt bindings are invalid") from exc
        if journal["kind"] != "completed" or _identity_pair(journal, "manifest") != (
            manifest.identity
        ):
            continue
        if matched:
            raise ValueError("publication has ambiguous migration receipts")
        publication_bytes = _canonical_json_bytes(publication)
        if (
            _json_fingerprint(publication) != journal["publication_sha256"]
            or manifest.content != publication_bytes
        ):
            raise ValueError("migration publication receipt is invalid")
        candidate = _read_regular_snapshot(
            _migration_sidecar_path(paths, journal, "manifest-retired"),
            allowed_link_counts=(2,),
        )
        retired_journal = _read_regular_snapshot(
            _migration_sidecar_path(paths, journal, "journal-retired")
        )
        if (
            candidate.identity != manifest.identity
            or candidate.content != manifest.content
            or retired_journal.identity != (receipt["journal_dev"], receipt["journal_ino"])
            or retired_journal.content != _canonical_json_bytes(journal)
        ):
            raise ValueError("migration publication receipt is invalid")
        matched = True
    return matched


def _publication_manifest_status(
    final: dict[str, Any], paths: WorkflowPaths, publication: dict[str, Any]
) -> tuple[bool, str | None, Path | None]:
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
        _persisted_tree, confirmation = _read_matching_chapter_context(
            paths,
            expected_tree=expected_tree,
            expected_confirmation=None,
        )
        if confirmation.get("action") not in {"confirm", "edit"}:
            raise ValueError("publication chapter is invalid")
        chapter = confirmation.get("chapter")
        if not isinstance(chapter, dict):
            raise ValueError("publication chapter is invalid")
        _validate_publication_metadata(
            metadata,
            input_fingerprint=input_fingerprint,
            evidence_fingerprint=expected_tree["evidence_fingerprint"],
            chapter=chapter,
        )
        artifact_path = paths.root / artifact_basename
        content = _read_regular_bytes(artifact_path)
        markdown = content.decode("utf-8")
        current_final = _read_regular_json(paths.final)
        if (
            publication.get("final_snapshot_sha256") != _json_fingerprint(final)
            or _json_fingerprint(current_final) != _json_fingerprint(final)
            or hashlib.sha256(content).hexdigest() != artifact_sha256
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


def _read_regular_snapshot(
    path: Path, *, allowed_link_counts: tuple[int, ...] = (1,)
) -> _RegularFileSnapshot:
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
            or before.st_nlink not in allowed_link_counts
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
        return _RegularFileSnapshot(
            content=data,
            device=before.st_dev,
            inode=before.st_ino,
        )
    except OSError as exc:
        raise ValueError("artifact cannot be read safely") from exc
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path) -> bytes:
    return _read_regular_snapshot(path).content


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


def _rename_with_flags(first: Path, second: Path, flags: int) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(library, "renameatx_np", None)
    if renameatx_np is None:
        raise OSError(errno.ENOTSUP, "atomic exchange is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameatx_np(
        _AT_FDCWD,
        os.fsencode(first),
        _AT_FDCWD,
        os.fsencode(second),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error))


def _rename_swap(first: Path, second: Path) -> None:
    _rename_with_flags(first, second, _RENAME_SWAP)


def _rename_no_replace(source: Path, target: Path) -> None:
    _rename_with_flags(source, target, _RENAME_EXCL)


def _temporary_retirement_path(path: Path, identity: tuple[int, int]) -> Path:
    identity_hash = hashlib.sha256(f"{identity[0]}:{identity[1]}".encode("ascii")).hexdigest()[:16]
    return path.with_name(f"{path.name}.{identity_hash}.retired")


def _retire_owned_temporary(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    error_code: str,
) -> None:
    retained = _temporary_retirement_path(path, expected_identity)
    try:
        active_identity = _path_identity(path)
    except FileNotFoundError:
        try:
            retained_identity = _path_identity(retained)
        except FileNotFoundError:
            return
        except OSError:
            raise _LegacyMigrationError(error_code) from None
        if retained_identity != expected_identity:
            raise _LegacyMigrationError(error_code) from None
        return
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    if active_identity != expected_identity:
        raise _LegacyMigrationError(error_code)
    try:
        _rename_no_replace(path, retained)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    try:
        _fsync_directory(path.parent)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    try:
        moved_identity = _path_identity(retained)
    except OSError:
        _restore_retained_path(retained, path)
        raise _LegacyMigrationError(error_code) from None
    if moved_identity != expected_identity:
        _restore_retained_path(retained, path)
        raise _LegacyMigrationError(error_code)


def _migration_candidate_owner(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    label: str,
    publication_lock: _PublicationLock | None = None,
) -> _MigrationCandidateOwner:
    transaction_id = journal.get("transaction_id")
    kind = journal.get("kind")
    tokens = journal.get("sidecar_transaction_tokens")
    if (
        not isinstance(transaction_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", transaction_id) is None
        or kind not in _LEGACY_MIGRATION_PHASES
        or label not in _MIGRATION_SIDECAR_LABELS
        or not isinstance(tokens, dict)
        or not _is_sha256(tokens.get(label))
    ):
        raise _LegacyMigrationError(_migration_error_code(kind))
    owner = _MigrationCandidateOwner(
        transaction_id=transaction_id,
        label=label,
        transaction_token=str(tokens[label]),
        root=paths.root,
        kind=str(kind),
        publication_lock=publication_lock,
    )
    if publication_lock is not None:
        _validate_migration_lock(paths, journal, publication_lock)
        _require_migration_bootstrap_guard(owner)
    return owner


def _require_migration_bootstrap_guard(owner: _MigrationCandidateOwner) -> _PublicationLock:
    """Require the live exclusive guard for mutable internal sidecar bootstrap.

    M0 serializes cooperating PDF2MD processes inside an owner-only transaction
    root. POSIX cannot atomically make a newly created inode identity durable;
    replacement by a malicious same-UID process in that bootstrap micro-window
    is outside this boundary and must not be described as covered.
    """

    publication_lock = owner.publication_lock
    code = _migration_error_code(owner.kind)
    if publication_lock is None:
        raise _LegacyMigrationError(code)
    try:
        publication_lock = _require_active_publication_lock(publication_lock)
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if publication_lock.root_path != owner.root:
        raise _LegacyMigrationError(code)
    return publication_lock


def _migration_owner_uid() -> int:
    return os.geteuid()


def _migration_sidecar_receipt_path(owner: _MigrationCandidateOwner) -> Path:
    return owner.root / f".ocr-v1-v2-{owner.transaction_id}.sidecar-{owner.label}.json"


def _migration_sidecar_receipt_stage_path(
    owner: _MigrationCandidateOwner,
    sequence: int,
) -> Path:
    return owner.root / (
        f".ocr-v1-v2-{owner.transaction_id}.sidecar-{owner.label}.stage-{sequence}"
    )


def _migration_sidecar_object_basenames(
    owner: _MigrationCandidateOwner,
) -> tuple[str, str, str]:
    prefix = f".ocr-v1-v2-{owner.transaction_id}"
    suffix = f"{owner.label}-{owner.transaction_token}"
    return (
        f"{prefix}.allocating-{suffix}",
        f"{prefix}.candidate-{suffix}",
        f"{prefix}.capture-{suffix}",
    )


def _migration_sidecar_receipt_self_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload["receipt_self_sha256"] = None
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _validate_migration_sidecar_receipt(
    value: object,
    *,
    allow_unbound_self: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _MIGRATION_SIDECAR_RECEIPT_FIELDS:
        raise ValueError("migration sidecar receipt is invalid")
    label = value.get("label")
    transaction_id = value.get("transaction_id")
    token = value.get("transaction_token")
    stage = value.get("stage")
    sequence = value.get("sequence")
    if (
        value.get("schema_version") != 1
        or label not in _MIGRATION_SIDECAR_LABELS
        or not _is_sha256(transaction_id)
        or not _is_sha256(token)
        or stage not in _MIGRATION_SIDECAR_STAGES
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != _MIGRATION_SIDECAR_STAGES.index(str(stage))
    ):
        raise ValueError("migration sidecar receipt is invalid")
    target_basename = value.get("target_basename")
    if (
        not isinstance(target_basename, str)
        or not target_basename
        or Path(target_basename).name != target_basename
        or not _is_sha256(value.get("content_sha256"))
    ):
        raise ValueError("migration sidecar receipt is invalid")
    owner = _MigrationCandidateOwner(
        transaction_id=str(transaction_id),
        label=str(label),
        transaction_token=str(token),
        root=Path("."),
        kind="completed",
    )
    expected_basenames = _migration_sidecar_object_basenames(owner)
    if (
        tuple(
            value.get(field)
            for field in ("allocation_basename", "candidate_basename", "capture_basename")
        )
        != expected_basenames
    ):
        raise ValueError("migration sidecar receipt is invalid")
    identities = {
        prefix: _identity_pair(value, prefix)
        for prefix in ("allocation", "candidate", "published", "receipt_self", "receipt_previous")
    }
    for prefix, identity in identities.items():
        raw = (value.get(f"{prefix}_dev"), value.get(f"{prefix}_ino"))
        if (raw == (None, None)) != (identity is None):
            raise ValueError("migration sidecar receipt is invalid")
    self_hash = value.get("receipt_self_sha256")
    self_unbound = identities["receipt_self"] is None and self_hash is None
    if self_unbound:
        if not allow_unbound_self:
            raise ValueError("migration sidecar receipt is invalid")
    elif (
        identities["receipt_self"] is None
        or not _is_sha256(self_hash)
        or self_hash != _migration_sidecar_receipt_self_hash(value)
    ):
        raise ValueError("migration sidecar receipt is invalid")
    previous_hash = value.get("receipt_previous_sha256")
    if sequence == 0:
        if identities["receipt_previous"] is not None or previous_hash is not None:
            raise ValueError("migration sidecar receipt is invalid")
    elif identities["receipt_previous"] is None or not _is_sha256(previous_hash):
        raise ValueError("migration sidecar receipt is invalid")
    allocation_expected = sequence >= 1
    candidate_expected = sequence >= 2
    published_expected = sequence >= 3
    if (
        (identities["allocation"] is not None) != allocation_expected
        or (identities["candidate"] is not None) != candidate_expected
        or (identities["published"] is not None) != published_expected
    ):
        raise ValueError("migration sidecar receipt is invalid")
    if candidate_expected and identities["candidate"] != identities["allocation"]:
        raise ValueError("migration sidecar receipt is invalid")
    if published_expected and identities["published"] != identities["candidate"]:
        raise ValueError("migration sidecar receipt is invalid")
    return value


def _bind_migration_sidecar_receipt_identity(
    receipt: dict[str, Any],
    identity: tuple[int, int],
) -> dict[str, Any]:
    receipt = _validate_migration_sidecar_receipt(receipt, allow_unbound_self=True)
    if any(
        receipt[field] is not None
        for field in ("receipt_self_dev", "receipt_self_ino", "receipt_self_sha256")
    ):
        raise ValueError("migration sidecar receipt is already bound")
    bound = dict(receipt)
    bound["receipt_self_dev"], bound["receipt_self_ino"] = identity
    bound["receipt_self_sha256"] = None
    bound["receipt_self_sha256"] = _migration_sidecar_receipt_self_hash(bound)
    return _validate_migration_sidecar_receipt(bound)


def _read_migration_sidecar_receipt(
    path: Path,
    *,
    expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], _RegularFileSnapshot]:
    snapshot = _read_regular_snapshot(path)
    info = path.lstat()
    try:
        value = json.loads(snapshot.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("migration sidecar receipt is invalid") from exc
    receipt = _validate_migration_sidecar_receipt(value)
    if (
        snapshot.content != _canonical_json_bytes(receipt)
        or snapshot.identity != _identity_pair(receipt, "receipt_self")
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != _migration_owner_uid()
        or (info.st_dev, info.st_ino) != snapshot.identity
        or (expected is not None and receipt != expected)
    ):
        raise ValueError("migration sidecar receipt is invalid")
    return receipt, snapshot


def _write_migration_sidecar_receipt_candidate(
    path: Path,
    receipt: dict[str, Any],
    *,
    owner: _MigrationCandidateOwner,
) -> tuple[dict[str, Any], _RegularFileSnapshot]:
    _require_migration_bootstrap_guard(owner)
    draft = dict(receipt)
    draft["receipt_self_dev"] = None
    draft["receipt_self_ino"] = None
    draft["receipt_self_sha256"] = None
    draft = _validate_migration_sidecar_receipt(draft, allow_unbound_self=True)
    checkpoint_prefix = f"migration-sidecar-receipt:{draft['label']}:{draft['stage']}"
    if (
        path.parent != owner.root
        or draft["transaction_id"] != owner.transaction_id
        or draft["transaction_token"] != owner.transaction_token
        or draft["label"] != owner.label
    ):
        raise ValueError("migration sidecar receipt owner changed")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    try:
        fd, info = _open_owned_sidecar(path, owner=owner, flags=flags)
    except FileExistsError:
        try:
            existing, snapshot = _read_migration_sidecar_receipt(path)
        except (OSError, ValueError):
            # Crash recovery may finish our own empty/canonical prefix while the
            # exclusive guard is held. A malicious same-UID inode replacement in
            # the pre-receipt identity window is outside the M0 threat model.
            fd, info = _open_owned_sidecar(path, owner=owner, flags=os.O_RDWR)
        else:
            existing_unbound = dict(existing)
            existing_unbound["receipt_self_dev"] = None
            existing_unbound["receipt_self_ino"] = None
            existing_unbound["receipt_self_sha256"] = None
            if existing_unbound != draft:
                raise ValueError("migration sidecar receipt conflicts") from None
            _migration_checkpoint(f"{checkpoint_prefix}:fsynced")
            _fsync_directory(path.parent)
            return existing, snapshot
    bound = _bind_migration_sidecar_receipt_identity(
        draft,
        (info.st_dev, info.st_ino),
    )
    content = _canonical_json_bytes(bound)
    try:
        if info.st_size:
            # Under the transaction lock, only a strict canonical prefix can be
            # resumed. A malicious process running as the same user can still
            # race this namespace; any complete or divergent object is retained
            # and fails closed.
            if info.st_size >= len(content) or info.st_size > _MAX_ARTIFACT_BYTES:
                raise ValueError("migration sidecar receipt conflicts")
            partial = os.pread(fd, info.st_size + 1, 0)
            if len(partial) != info.st_size or not content.startswith(partial):
                raise ValueError("migration sidecar receipt conflicts")
        _migration_checkpoint(f"{checkpoint_prefix}:opened")
        os.ftruncate(fd, 0)
        split = max(1, len(content) // 2)
        _write_all(fd, content[:split])
        _migration_checkpoint(f"{checkpoint_prefix}:partial-written")
        _write_all(fd, content[split:])
        os.fsync(fd)
        _safe_owned_sidecar_fd(fd, (info.st_dev, info.st_ino))
        _require_migration_bootstrap_guard(owner)
        _migration_checkpoint(f"{checkpoint_prefix}:fsynced")
    finally:
        os.close(fd)
    bound, snapshot = _read_migration_sidecar_receipt(path, expected=bound)
    _fsync_directory(path.parent)
    return bound, snapshot


def _ensure_migration_sidecar_receipt(
    owner: _MigrationCandidateOwner,
    *,
    target_basename: str,
    content: bytes,
) -> dict[str, Any]:
    _require_migration_bootstrap_guard(owner)
    path = _migration_sidecar_receipt_path(owner)
    allocation, candidate, capture = _migration_sidecar_object_basenames(owner)
    expected = {
        "schema_version": 1,
        "transaction_id": owner.transaction_id,
        "transaction_token": owner.transaction_token,
        "label": owner.label,
        "stage": "declared",
        "sequence": 0,
        "target_basename": target_basename,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "allocation_basename": allocation,
        "candidate_basename": candidate,
        "capture_basename": capture,
        "allocation_dev": None,
        "allocation_ino": None,
        "candidate_dev": None,
        "candidate_ino": None,
        "published_dev": None,
        "published_ino": None,
        "receipt_self_dev": None,
        "receipt_self_ino": None,
        "receipt_self_sha256": None,
        "receipt_previous_dev": None,
        "receipt_previous_ino": None,
        "receipt_previous_sha256": None,
    }
    try:
        receipt, _snapshot = _read_migration_sidecar_receipt(path)
    except (FileNotFoundError, OSError, ValueError):
        receipt, _snapshot = _write_migration_sidecar_receipt_candidate(
            path,
            expected,
            owner=owner,
        )
    _migration_checkpoint(
        f"migration-sidecar-receipt:{receipt['label']}:{receipt['stage']}:fsynced"
    )
    expected_stable = {
        key: value
        for key, value in expected.items()
        if key
        not in {
            "stage",
            "sequence",
            "allocation_dev",
            "allocation_ino",
            "candidate_dev",
            "candidate_ino",
            "published_dev",
            "published_ino",
            "receipt_self_dev",
            "receipt_self_ino",
            "receipt_self_sha256",
            "receipt_previous_dev",
            "receipt_previous_ino",
            "receipt_previous_sha256",
        }
    }
    if any(receipt.get(key) != value for key, value in expected_stable.items()):
        raise ValueError("migration sidecar receipt conflicts")
    return receipt


def _advance_migration_sidecar_receipt(
    owner: _MigrationCandidateOwner,
    receipt: dict[str, Any],
    stage: str,
    *,
    identity: tuple[int, int],
) -> dict[str, Any]:
    _require_migration_bootstrap_guard(owner)
    receipt = _validate_migration_sidecar_receipt(receipt)
    current_index = _MIGRATION_SIDECAR_STAGES.index(str(receipt["stage"]))
    target_index = _MIGRATION_SIDECAR_STAGES.index(stage)
    identity_prefix = {"allocated": "allocation", "ready": "candidate", "published": "published"}[
        stage
    ]
    if current_index >= target_index:
        if current_index == target_index and _identity_pair(receipt, identity_prefix) != identity:
            raise ValueError("migration sidecar identity conflicts")
        return receipt
    if target_index != current_index + 1:
        raise ValueError("migration sidecar stage is not sequential")
    path = _migration_sidecar_receipt_path(owner)
    current, current_snapshot = _read_migration_sidecar_receipt(path, expected=receipt)
    updated = dict(current)
    updated["stage"] = stage
    updated["sequence"] = target_index
    updated[f"{identity_prefix}_dev"], updated[f"{identity_prefix}_ino"] = identity
    updated["receipt_previous_dev"] = current_snapshot.device
    updated["receipt_previous_ino"] = current_snapshot.inode
    updated["receipt_previous_sha256"] = hashlib.sha256(current_snapshot.content).hexdigest()
    updated["receipt_self_dev"] = None
    updated["receipt_self_ino"] = None
    updated["receipt_self_sha256"] = None
    stage_path = _migration_sidecar_receipt_stage_path(owner, target_index)
    updated, candidate_snapshot = _write_migration_sidecar_receipt_candidate(
        stage_path,
        updated,
        owner=owner,
    )
    try:
        _rename_swap(stage_path, path)
        _fsync_directory(owner.root)
    except OSError as exc:
        raise ValueError("migration sidecar receipt CAS failed") from exc
    committed, committed_snapshot = _read_migration_sidecar_receipt(path, expected=updated)
    displaced, displaced_snapshot = _read_migration_sidecar_receipt(
        stage_path,
        expected=current,
    )
    if (
        committed_snapshot.identity != candidate_snapshot.identity
        or displaced_snapshot.identity != current_snapshot.identity
        or displaced != current
    ):
        raise ValueError("migration sidecar receipt CAS is ambiguous")
    return committed


def _assert_migration_sidecar_namespace(
    owner: _MigrationCandidateOwner,
    receipt: dict[str, Any],
) -> None:
    expected = {
        str(receipt["allocation_basename"]),
        str(receipt["candidate_basename"]),
        str(receipt["capture_basename"]),
    }
    prefixes = tuple(basename.rsplit("-", 1)[0] for basename in expected)
    try:
        conflicting = tuple(
            path
            for path in owner.root.iterdir()
            if any(path.name.startswith(prefix) for prefix in prefixes)
            and path.name not in expected
        )
    except OSError as exc:
        raise ValueError("migration sidecar namespace cannot be inspected") from exc
    if conflicting:
        raise ValueError("migration sidecar namespace conflicts")


def _safe_owned_sidecar_fd(
    fd: int, expected_identity: tuple[int, int] | None = None
) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != _migration_owner_uid()
        or (expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity)
    ):
        raise ValueError("migration sidecar is unsafe")
    return info


def _open_owned_sidecar(
    path: Path,
    *,
    owner: _MigrationCandidateOwner,
    flags: int,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, os.stat_result]:
    _require_migration_bootstrap_guard(owner)
    if path.parent != owner.root:
        raise ValueError("migration sidecar root changed")
    open_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, open_flags, 0o600)
    try:
        info = _safe_owned_sidecar_fd(fd, expected_identity)
        path_info = path.lstat()
        if not stat.S_ISREG(path_info.st_mode) or (path_info.st_dev, path_info.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise ValueError("migration sidecar path changed")
        _require_migration_bootstrap_guard(owner)
        return fd, info
    except Exception:
        os.close(fd)
        raise


def _read_owned_sidecar_fd(fd: int, expected_identity: tuple[int, int]) -> _RegularFileSnapshot:
    before = _safe_owned_sidecar_fd(fd, expected_identity)
    if before.st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("migration sidecar is too large")
    content = os.pread(fd, before.st_size + 1, 0)
    after = _safe_owned_sidecar_fd(fd, expected_identity)
    if (
        len(content) != before.st_size
        or len(content) > _MAX_ARTIFACT_BYTES
        or any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        )
    ):
        raise ValueError("migration sidecar changed while reading")
    return _RegularFileSnapshot(content, before.st_dev, before.st_ino)


def _read_owned_migration_candidate(
    path: Path,
    *,
    encoded_identity: tuple[int, int],
    content: bytes,
    error_code: str,
) -> _RegularFileSnapshot:
    try:
        before = path.lstat()
        snapshot = _read_regular_snapshot(path)
        after = path.lstat()
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    observed_identity = (before.st_dev, before.st_ino)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != _migration_owner_uid()
        or observed_identity != encoded_identity
        or snapshot.identity != encoded_identity
        or snapshot.content != content
        or (after.st_dev, after.st_ino) != encoded_identity
        or after.st_mode != before.st_mode
        or after.st_uid != before.st_uid
    ):
        raise _LegacyMigrationError(error_code)
    return snapshot


def _write_owned_migration_candidate(
    parent: Path,
    *,
    owner: _MigrationCandidateOwner,
    target_basename: str,
    content: bytes,
    error_code: str,
) -> tuple[Path, _RegularFileSnapshot]:
    try:
        _require_migration_bootstrap_guard(owner)
        if parent != owner.root:
            raise ValueError("migration sidecar root changed")
        receipt = _ensure_migration_sidecar_receipt(
            owner,
            target_basename=target_basename,
            content=content,
        )
        _assert_migration_sidecar_namespace(owner, receipt)
        allocation = owner.root / str(receipt["allocation_basename"])
        candidate = owner.root / str(receipt["candidate_basename"])
        expected_identity = _identity_pair(receipt, "allocation")
        if receipt["stage"] == "declared":
            try:
                fd, created = _open_owned_sidecar(
                    allocation,
                    owner=owner,
                    flags=os.O_RDWR,
                )
            except FileNotFoundError:
                fd, created = _open_owned_sidecar(
                    allocation,
                    owner=owner,
                    flags=os.O_RDWR | os.O_CREAT | os.O_EXCL,
                )
            if created.st_size != 0:
                os.close(fd)
                raise ValueError("unbound migration allocation is not empty")
            # The private root and exclusive guard serialize cooperating app
            # processes. Self-crash recovery may reclaim this empty inode; a
            # malicious same-UID replacement before durable identity binding is
            # explicitly outside the M0 bootstrap boundary.
            _migration_checkpoint(f"migration-sidecar:{owner.label}:allocation-opened")
            receipt = _advance_migration_sidecar_receipt(
                owner,
                receipt,
                "allocated",
                identity=(created.st_dev, created.st_ino),
            )
            expected_identity = (created.st_dev, created.st_ino)
            os.close(fd)
        if expected_identity is None:
            raise ValueError("migration allocation identity is missing")
        if receipt["stage"] in {"ready", "published"}:
            capture = owner.root / str(receipt["capture_basename"])
            published = owner.root / str(receipt["target_basename"])
            existing_paths = []
            for current in (candidate, capture, published):
                try:
                    current.lstat()
                except FileNotFoundError:
                    continue
                existing_paths.append(current)
            if len(existing_paths) != 1:
                raise ValueError("migration candidate location is ambiguous")
            current = existing_paths[0]
            snapshot = _read_owned_migration_candidate(
                current,
                encoded_identity=expected_identity,
                content=content,
                error_code=error_code,
            )
            return current, snapshot
        try:
            candidate_info = candidate.lstat()
        except FileNotFoundError:
            try:
                allocation_info = allocation.lstat()
            except OSError as exc:
                raise ValueError("migration allocation is missing") from exc
            if (allocation_info.st_dev, allocation_info.st_ino) != expected_identity:
                raise ValueError("migration allocation identity changed") from None
            _rename_no_replace(allocation, candidate)
            _fsync_directory(owner.root)
        else:
            if (candidate_info.st_dev, candidate_info.st_ino) != expected_identity:
                raise ValueError("migration candidate identity changed")
            try:
                allocation.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("migration allocation and candidate both exist")
        _migration_checkpoint(f"migration-sidecar:{owner.label}:allocation-promoted")
        if receipt["stage"] == "allocated":
            fd, _info = _open_owned_sidecar(
                candidate,
                owner=owner,
                flags=os.O_RDWR,
                expected_identity=expected_identity,
            )
            try:
                os.ftruncate(fd, 0)
                split = max(1, len(content) // 2) if content else 0
                if split:
                    _write_all(fd, content[:split])
                _migration_checkpoint(f"migration-sidecar:{owner.label}:partial-written")
                if split < len(content):
                    _write_all(fd, content[split:])
                os.fsync(fd)
                snapshot = _read_owned_sidecar_fd(fd, expected_identity)
                if snapshot.content != content:
                    raise ValueError("migration candidate content changed")
                _migration_checkpoint(f"migration-sidecar:{owner.label}:candidate-fsynced")
            finally:
                os.close(fd)
            receipt = _advance_migration_sidecar_receipt(
                owner,
                receipt,
                "ready",
                identity=expected_identity,
            )
        if receipt["stage"] != "ready":
            raise ValueError("migration candidate is not ready")
        snapshot = _read_owned_migration_candidate(
            candidate,
            encoded_identity=expected_identity,
            content=content,
            error_code=error_code,
        )
        return candidate, snapshot
    except _LegacyMigrationError:
        raise
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None


def _write_temporary_regular(
    parent: Path,
    *,
    prefix: str,
    content: bytes,
    error_code: str = "ocr_state_invalid",
    candidate_owner: _MigrationCandidateOwner | None = None,
    target_basename: str | None = None,
) -> tuple[Path, _RegularFileSnapshot]:
    if candidate_owner is not None:
        if target_basename is None or Path(target_basename).name != target_basename:
            raise _LegacyMigrationError(error_code)
        return _write_owned_migration_candidate(
            parent,
            owner=candidate_owner,
            target_basename=target_basename,
            content=content,
            error_code=error_code,
        )
    fd, temporary_name = tempfile.mkstemp(prefix=prefix, dir=parent)
    temporary = Path(temporary_name)
    created = os.fstat(fd)
    created_identity = (created.st_dev, created.st_ino)
    try:
        _write_all(fd, content)
        os.fsync(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("temporary file is unsafe")
        os.close(fd)
        fd = -1
        snapshot = _read_regular_snapshot(temporary)
        if snapshot.content != content or snapshot.identity != (info.st_dev, info.st_ino):
            raise ValueError("temporary file changed")
        return temporary, snapshot
    except Exception:
        if fd >= 0:
            os.close(fd)
            fd = -1
        _retire_owned_temporary(
            temporary,
            created_identity,
            error_code=error_code,
        )
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _path_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def _rollback_swap(
    temporary: Path,
    target: Path,
    *,
    committed_identity: tuple[int, int],
) -> bool:
    try:
        if _path_identity(target) != committed_identity:
            return False
        temporary.lstat()
        _rename_swap(temporary, target)
        _fsync_directory(target.parent)
        return True
    except OSError:
        return False


def _publish_owned_migration_candidate(
    temporary: Path,
    target: Path,
    content: bytes,
    owner: _MigrationCandidateOwner,
    *,
    error_code: str,
) -> _RegularFileSnapshot:
    del temporary
    try:
        _require_migration_bootstrap_guard(owner)
        receipt, _receipt_snapshot = _read_migration_sidecar_receipt(
            _migration_sidecar_receipt_path(owner)
        )
        if (
            receipt["transaction_id"] != owner.transaction_id
            or receipt["transaction_token"] != owner.transaction_token
            or receipt["label"] != owner.label
            or receipt["target_basename"] != target.name
            or receipt["content_sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("migration sidecar receipt conflicts")
        expected_identity = _identity_pair(receipt, "candidate")
        if expected_identity is None:
            raise ValueError("migration candidate identity is missing")
        _assert_migration_sidecar_namespace(owner, receipt)
        candidate = owner.root / str(receipt["candidate_basename"])
        capture = owner.root / str(receipt["capture_basename"])
        if receipt["stage"] == "published":
            published_identity = _identity_pair(receipt, "published")
            if published_identity != expected_identity:
                raise ValueError("migration publication identity conflicts")
            return _read_owned_migration_candidate(
                target,
                encoded_identity=expected_identity,
                content=content,
                error_code=error_code,
            )
        if receipt["stage"] != "ready":
            raise ValueError("migration candidate is not ready")
        _require_migration_bootstrap_guard(owner)

        try:
            target.lstat()
        except FileNotFoundError:
            target_exists = False
        else:
            target_exists = True
        try:
            capture.lstat()
        except FileNotFoundError:
            capture_exists = False
        else:
            capture_exists = True
        try:
            candidate.lstat()
        except FileNotFoundError:
            candidate_exists = False
        else:
            candidate_exists = True
        if sum((target_exists, capture_exists, candidate_exists)) != 1:
            raise ValueError("migration publication namespace is ambiguous")

        if candidate_exists:
            _rename_no_replace(candidate, capture)
            _fsync_directory(owner.root)
            capture_exists = True
        if capture_exists:
            captured = _read_owned_migration_candidate(
                capture,
                encoded_identity=expected_identity,
                content=content,
                error_code=error_code,
            )
            if captured.identity != expected_identity:
                raise ValueError("migration capture identity changed")
            _migration_checkpoint(f"migration-sidecar:{owner.label}:captured-verified")
            _rename_no_replace(capture, target)
            _fsync_directory(owner.root)
        _migration_checkpoint(f"migration-sidecar:{owner.label}:published-unverified")
        published = _read_owned_migration_candidate(
            target,
            encoded_identity=expected_identity,
            content=content,
            error_code=error_code,
        )
        receipt = _advance_migration_sidecar_receipt(
            owner,
            receipt,
            "published",
            identity=published.identity,
        )
        if _identity_pair(receipt, "published") != published.identity:
            raise ValueError("migration publication receipt is invalid")
        return published
    except _LegacyMigrationError:
        raise
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None


def _atomic_compare_exchange_bytes(
    path: Path,
    *,
    expected: bytes,
    replacement: bytes,
    error_code: str,
    before_swap_checkpoint: str | None = None,
    after_swap_checkpoint: str | None = None,
) -> _RegularFileSnapshot:
    temporary, candidate = _write_temporary_regular(
        path.parent,
        prefix=f".{path.name}.cas-",
        content=replacement,
        error_code=error_code,
    )
    current = _read_regular_snapshot(path)
    if current.content != expected:
        raise _LegacyMigrationError(error_code)
    if before_swap_checkpoint is not None:
        _migration_checkpoint(before_swap_checkpoint)
    try:
        _rename_swap(temporary, path)
    except OSError:
        raise _LegacyMigrationError(error_code) from None

    try:
        _fsync_directory(path.parent)
    except OSError:
        # The exchange already happened. Both directory entries are retained so
        # recovery can decide from their identities instead of guessing.
        raise _LegacyMigrationError(error_code) from None

    try:
        committed = _read_regular_snapshot(path)
        displaced = _read_regular_snapshot(temporary)
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if (
        committed.identity != candidate.identity
        or committed.content != replacement
        or displaced.identity != current.identity
        or displaced.content != expected
    ):
        _rollback_swap(
            temporary,
            path,
            committed_identity=candidate.identity,
        )
        raise _LegacyMigrationError(error_code)
    if after_swap_checkpoint is not None:
        _migration_checkpoint(after_swap_checkpoint)
    return committed


def _write_regular_no_replace(
    path: Path,
    content: bytes,
    *,
    error_code: str,
    candidate_owner: _MigrationCandidateOwner | None = None,
) -> _RegularFileSnapshot:
    temporary, candidate = _write_temporary_regular(
        path.parent,
        prefix=f".{path.name}.new-",
        content=content,
        error_code=error_code,
        candidate_owner=candidate_owner,
        target_basename=path.name if candidate_owner is not None else None,
    )
    if candidate_owner is not None:
        return _publish_owned_migration_candidate(
            temporary,
            path,
            content,
            candidate_owner,
            error_code=error_code,
        )
    published_by_us = False
    try:
        try:
            _rename_no_replace(temporary, path)
        except FileExistsError:
            raise _LegacyMigrationError(error_code) from None
        except OSError:
            raise _LegacyMigrationError(error_code) from None
        published_by_us = True
        try:
            _fsync_directory(path.parent)
        except OSError:
            _require_bound_snapshot(
                path,
                content,
                candidate.identity,
                error_code=error_code,
            )
            raise _NoReplacePublicationUncertain(error_code, candidate.identity) from None
        published = _read_regular_snapshot(path)
        if published.content != content or published.identity != candidate.identity:
            raise _LegacyMigrationError(error_code)
        return published
    finally:
        try:
            _retire_owned_temporary(
                temporary,
                candidate.identity,
                error_code=error_code,
            )
        except _LegacyMigrationError:
            if published_by_us:
                raise _NoReplacePublicationUncertain(
                    error_code,
                    candidate.identity,
                ) from None
            raise


def _publish_immutable_artifact(
    root: Path,
    content: bytes,
    digest: str,
    *,
    candidate_owner: _MigrationCandidateOwner | None = None,
) -> Path:
    target = root / f"intensive-reading.{digest}.md"
    if candidate_owner is not None:
        _write_regular_no_replace(
            target,
            content,
            error_code="ocr_evidence_invalid",
            candidate_owner=candidate_owner,
        )
        return target
    try:
        existing = _read_regular_snapshot(target)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise ValueError("publication artifact is invalid") from None
    else:
        if existing.content != content:
            raise ValueError("publication artifact hash collision")
        return target
    temporary, candidate = _write_temporary_regular(
        root,
        prefix=".note-artifact.",
        content=content,
        error_code="ocr_evidence_invalid",
        candidate_owner=candidate_owner,
    )
    published_by_us = False
    try:
        try:
            _rename_no_replace(temporary, target)
        except FileExistsError:
            if _read_regular_bytes(target) != content:
                raise ValueError("publication artifact hash collision") from None
            return target
        except OSError:
            raise ValueError("publication artifact cannot be published") from None
        published_by_us = True
        try:
            _fsync_directory(root)
        except OSError:
            _require_bound_snapshot(
                target,
                content,
                candidate.identity,
                error_code="ocr_evidence_invalid",
            )
            raise ValueError("publication artifact directory sync failed") from None
        published = _read_regular_snapshot(target)
        if published.content != content or published.identity != candidate.identity:
            raise ValueError("publication artifact is invalid")
        return target
    finally:
        try:
            _retire_owned_temporary(
                temporary,
                candidate.identity,
                error_code="ocr_evidence_invalid",
            )
        except _LegacyMigrationError:
            if published_by_us:
                raise ValueError("publication artifact cleanup is ambiguous") from None
            raise


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
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or path_info.st_uid != os.geteuid()
            or stat.S_IMODE(path_info.st_mode) & 0o077
            or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("publication lock is not a safe regular file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_root_lock(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow:
        flags |= no_follow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("state root cannot be opened safely") from exc
    try:
        _validate_locked_directory(path, fd, require_private=False)
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_locked_directory(
    path: Path,
    fd: int,
    *,
    require_private: bool = True,
) -> None:
    info = os.fstat(fd)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ValueError("state root changed while locked") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or not stat.S_ISDIR(path_info.st_mode)
        or path_info.st_uid != os.geteuid()
        or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
        or (
            require_private
            and (stat.S_IMODE(info.st_mode) != 0o700 or stat.S_IMODE(path_info.st_mode) != 0o700)
        )
    ):
        raise ValueError("state root changed while locked")


def _harden_private_locked_directory(path: Path, fd: int) -> None:
    _validate_locked_directory(path, fd, require_private=False)
    info = os.fstat(fd)
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(fd, 0o700)
        os.fsync(fd)
    _validate_locked_directory(path, fd)


def _validate_locked_file(path: Path, fd: int) -> None:
    info = os.fstat(fd)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ValueError("publication lock changed while acquiring") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > _MAX_ARTIFACT_BYTES
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or not stat.S_ISREG(path_info.st_mode)
        or path_info.st_nlink != 1
        or path_info.st_uid != os.geteuid()
        or stat.S_IMODE(path_info.st_mode) & 0o077
        or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
    ):
        raise ValueError("publication lock changed while acquiring")


def _read_lock_token(fd: int) -> str:
    info = os.fstat(fd)
    if info.st_size != 65:
        raise ValueError("publication lock token is invalid")
    try:
        content = os.pread(fd, 66, 0)
    except OSError as exc:
        raise ValueError("publication lock token cannot be read") from exc
    if len(content) != 65 or content[-1:] != b"\n":
        raise ValueError("publication lock token is invalid")
    try:
        token = content[:-1].decode("ascii")
    except UnicodeError as exc:
        raise ValueError("publication lock token is invalid") from exc
    if _LOCK_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("publication lock token is invalid")
    return token


def _persisted_migration_evidence_error(root_fd: int) -> str | None:
    entries = 0
    evidence_found = False
    completed_evidence_found = False
    try:
        with os.scandir(root_fd) as iterator:
            for entry in iterator:
                entries += 1
                if entries > _MAX_LOCK_RECOVERY_DIRECTORY_ENTRIES:
                    raise ValueError("publication lock transaction evidence is unbounded")
                if entry.name in _LOCK_RECOVERY_EVIDENCE_BASENAMES or entry.name.startswith(
                    _LOCK_RECOVERY_EVIDENCE_PREFIX
                ):
                    evidence_found = True
                    completed_evidence_found = (
                        completed_evidence_found or entry.name == "batch-final.v1.backup.json"
                    )
    except OSError as exc:
        raise ValueError("publication lock transaction evidence cannot be inspected") from exc
    if completed_evidence_found:
        return "ocr_evidence_invalid"
    if evidence_found:
        return "ocr_state_invalid"
    return None


def _initialize_lock_token(
    path: Path,
    fd: int,
    *,
    root_fd: int,
    require_clean_migration_state: bool = False,
) -> str:
    _validate_locked_directory(path.parent, root_fd)
    _validate_locked_file(path, fd)
    root_before = os.fstat(root_fd)
    lock_before = os.fstat(fd)
    if lock_before.st_size == 65:
        return _read_lock_token(fd)
    if lock_before.st_size > 64:
        raise ValueError("publication lock token is invalid")
    try:
        partial = os.pread(fd, lock_before.st_size + 1, 0)
    except OSError as exc:
        raise ValueError("publication lock token cannot be read") from exc
    if len(partial) != lock_before.st_size or _LOCK_TOKEN_PREFIX_RE.fullmatch(partial) is None:
        raise ValueError("publication lock token is invalid")
    if require_clean_migration_state:
        evidence_error = _persisted_migration_evidence_error(root_fd)
        if evidence_error is not None:
            raise _LegacyMigrationError(evidence_error)

    content = f"{secrets.token_hex(32)}\n".encode("ascii")
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    _write_all(fd, content)
    os.fsync(fd)
    lock_after = os.fstat(fd)
    if (
        (lock_after.st_dev, lock_after.st_ino) != (lock_before.st_dev, lock_before.st_ino)
        or stat.S_IMODE(lock_after.st_mode) != 0o600
        or lock_after.st_uid != os.geteuid()
    ):
        raise ValueError("publication lock identity changed")
    _validate_locked_file(path, fd)
    root_after = os.fstat(root_fd)
    if (root_after.st_dev, root_after.st_ino) != (root_before.st_dev, root_before.st_ino):
        raise ValueError("state root changed while locked")
    os.fsync(root_fd)
    _validate_locked_directory(path.parent, root_fd)
    _validate_locked_file(path, fd)
    return _read_lock_token(fd)


def _validate_active_publication_lock(publication_lock: _PublicationLock) -> None:
    if (
        type(publication_lock) is not _PublicationLock
        or publication_lock.pid != os.getpid()
        or publication_lock.thread_id != threading.get_ident()
    ):
        raise ValueError("publication lock guard changed execution context")
    with _ACTIVE_PUBLICATION_LEASES_GUARD:
        lease = _ACTIVE_PUBLICATION_LEASES.get(publication_lock)
        if lease is None:
            raise ValueError("publication lock guard is not active")
        if (
            lease.pid != publication_lock.pid
            or lease.thread_id != publication_lock.thread_id
            or lease.root_path != publication_lock.root_path
            or lease.path != publication_lock.path
            or lease.device != publication_lock.device
            or lease.inode != publication_lock.inode
            or lease.token != publication_lock.token
        ):
            raise ValueError("publication lock guard identity changed")
        _validate_locked_directory(lease.root_path, lease.root_fd)
        root_info = os.fstat(lease.root_fd)
        if (root_info.st_dev, root_info.st_ino) != (
            lease.root_device,
            lease.root_inode,
        ):
            raise ValueError("publication lock root identity changed")
        _validate_locked_file(lease.path, lease.fd)
        lock_info = os.fstat(lease.fd)
        if (lock_info.st_dev, lock_info.st_ino) != (lease.device, lease.inode):
            raise ValueError("publication lock identity changed")
        if _read_lock_token(lease.fd) != lease.token:
            raise ValueError("publication lock token changed")


def _require_active_publication_lock(value: object) -> _PublicationLock:
    if type(value) is not _PublicationLock:
        raise ValueError("publication lock guard is not active")
    publication_lock = value
    assert isinstance(publication_lock, _PublicationLock)
    _validate_active_publication_lock(publication_lock)
    return publication_lock


def _cleanup_publication_lock_resources(
    publication_lock: _PublicationLock | None,
    *,
    root_fd: int,
    root_locked: bool,
    root_borrowed: bool,
    fd: int | None,
    locked: bool,
) -> list[tuple[str, BaseException]]:
    if publication_lock is not None:
        with _ACTIVE_PUBLICATION_LEASES_GUARD:
            _ACTIVE_PUBLICATION_LEASES.pop(publication_lock, None)

    failures: list[tuple[str, BaseException]] = []

    if fd is not None:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException as exc:
            failures.append(("publication unlock", exc))
        finally:
            try:
                os.close(fd)
            except BaseException as exc:
                failures.append(("publication close", exc))

    if root_borrowed:
        try:
            if root_locked:
                fcntl.flock(root_fd, fcntl.LOCK_SH)
        except BaseException as exc:
            failures.append(("root downgrade", exc))
    else:
        try:
            if root_locked:
                fcntl.flock(root_fd, fcntl.LOCK_UN)
        except BaseException as exc:
            failures.append(("root unlock", exc))
        finally:
            try:
                os.close(root_fd)
            except BaseException as exc:
                failures.append(("root close", exc))

    return failures


def _add_publication_cleanup_notes(
    primary: BaseException,
    failures: list[tuple[str, BaseException]],
    *,
    primary_is_cleanup_failure: bool,
) -> None:
    start = 1 if primary_is_cleanup_failure else 0
    if primary_is_cleanup_failure:
        operation, _error = failures[0]
        primary.add_note(f"publication lock cleanup primary failure: {operation}")
    for operation, error in failures[start:]:
        primary.add_note(
            "additional publication lock cleanup failure during "
            f"{operation}: {type(error).__name__}: {error}"
        )


def _check_publication_lock_control(
    deadline: float | None,
    cancel_event: Any | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("publication lock acquisition cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("publication lock acquisition timed out")


def _publication_lock_poll_seconds(deadline: float | None) -> float:
    if deadline is None:
        return 0.05
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("publication lock acquisition timed out")
    return min(0.05, remaining)


def _acquire_publication_thread_lock(
    lock: threading.RLock,
    *,
    deadline: float | None,
    cancel_event: Any | None,
) -> None:
    if deadline is None and cancel_event is None:
        lock.acquire()
        return
    while True:
        _check_publication_lock_control(deadline, cancel_event)
        if lock.acquire(timeout=_publication_lock_poll_seconds(deadline)):
            return


def _acquire_publication_flock(
    fd: int,
    *,
    deadline: float | None,
    cancel_event: Any | None,
) -> None:
    if deadline is None and cancel_event is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while True:
        _check_publication_lock_control(deadline, cancel_event)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        time.sleep(_publication_lock_poll_seconds(deadline))


def _active_ocr_worker_claim(root: Path) -> _OcrWorkerClaim | None:
    with _ACTIVE_OCR_WORKER_CLAIMS_GUARD:
        claim = _ACTIVE_OCR_WORKER_CLAIMS.get(root)
    if claim is not None:
        claim.validate()
    return claim


@contextmanager
def _publication_transaction_lock(
    paths: WorkflowPaths,
    *,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> Iterator[_PublicationLock]:
    paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    thread_lock = _publication_thread_lock(paths.root)
    _acquire_publication_thread_lock(
        thread_lock,
        deadline=deadline,
        cancel_event=cancel_event,
    )
    try:
        worker_claim = _active_ocr_worker_claim(paths.root)
        root_borrowed = worker_claim is not None
        if worker_claim is None:
            root_fd = _open_root_lock(paths.root)
        else:
            assert worker_claim.root_fd is not None
            root_fd = worker_claim.root_fd
        root_locked = root_borrowed
        fd: int | None = None
        locked = False
        publication_lock: _PublicationLock | None = None
        try:
            _acquire_publication_flock(
                root_fd,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            root_locked = True
            _harden_private_locked_directory(paths.root, root_fd)
            fd = _open_publication_lock(paths.publication_lock)
            _acquire_publication_flock(
                fd,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            locked = True
            _validate_locked_file(paths.publication_lock, fd)
            token = _initialize_lock_token(
                paths.publication_lock,
                fd,
                root_fd=root_fd,
                require_clean_migration_state=True,
            )
            root_info = os.fstat(root_fd)
            lock_info = os.fstat(fd)
            publication_lock = _PublicationLock(
                root_path=paths.root,
                path=paths.publication_lock,
                device=lock_info.st_dev,
                inode=lock_info.st_ino,
                token=token,
                pid=os.getpid(),
                thread_id=threading.get_ident(),
            )
            lease = _ActivePublicationLease(
                root_path=paths.root,
                root_fd=root_fd,
                root_device=root_info.st_dev,
                root_inode=root_info.st_ino,
                path=paths.publication_lock,
                fd=fd,
                device=lock_info.st_dev,
                inode=lock_info.st_ino,
                token=token,
                pid=publication_lock.pid,
                thread_id=publication_lock.thread_id,
            )
            with _ACTIVE_PUBLICATION_LEASES_GUARD:
                _ACTIVE_PUBLICATION_LEASES[publication_lock] = lease
            yield publication_lock
        except BaseException as operation_error:
            cleanup_failures = _cleanup_publication_lock_resources(
                publication_lock,
                root_fd=root_fd,
                root_locked=root_locked,
                root_borrowed=root_borrowed,
                fd=fd,
                locked=locked,
            )
            _add_publication_cleanup_notes(
                operation_error,
                cleanup_failures,
                primary_is_cleanup_failure=False,
            )
            raise
        else:
            cleanup_failures = _cleanup_publication_lock_resources(
                publication_lock,
                root_fd=root_fd,
                root_locked=root_locked,
                root_borrowed=root_borrowed,
                fd=fd,
                locked=locked,
            )
            if cleanup_failures:
                primary_cleanup_error = cleanup_failures[0][1]
                _add_publication_cleanup_notes(
                    primary_cleanup_error,
                    cleanup_failures,
                    primary_is_cleanup_failure=True,
                )
                raise primary_cleanup_error
    finally:
        thread_lock.release()


def _ocr_worker_thread_lock(root: Path) -> threading.Lock:
    with _OCR_WORKER_THREAD_LOCKS_GUARD:
        lock = _OCR_WORKER_THREAD_LOCKS.get(root)
        if lock is None:
            lock = threading.Lock()
            _OCR_WORKER_THREAD_LOCKS[root] = lock
        return lock


def _try_claim_ocr_worker(paths: WorkflowPaths) -> _OcrWorkerClaim | None:
    paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    thread_lock = _ocr_worker_thread_lock(paths.root)
    if not thread_lock.acquire(blocking=False):
        return None
    root_fd: int | None = None
    root_locked = False
    fd: int | None = None
    locked = False
    claimed = False
    path = paths.root / _OCR_WORKER_LOCK_BASENAME
    try:
        root_fd = _open_root_lock(paths.root)
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        root_locked = True
        _harden_private_locked_directory(paths.root, root_fd)
        fd = _open_publication_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        locked = True
        _validate_locked_file(path, fd)
        _initialize_lock_token(path, fd, root_fd=root_fd)
        fcntl.flock(root_fd, fcntl.LOCK_SH)
        claim = _OcrWorkerClaim(
            root_path=paths.root,
            root_fd=root_fd,
            path=path,
            fd=fd,
            thread_lock=thread_lock,
        )
        with _ACTIVE_OCR_WORKER_CLAIMS_GUARD:
            if paths.root in _ACTIVE_OCR_WORKER_CLAIMS:
                raise ValueError("OCR worker claim is already active")
            _ACTIVE_OCR_WORKER_CLAIMS[paths.root] = claim
        claimed = True
        root_fd = None
        fd = None
        return claim
    finally:
        if fd is not None:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if root_fd is not None:
            if root_locked:
                fcntl.flock(root_fd, fcntl.LOCK_UN)
            os.close(root_fd)
        if not claimed:
            thread_lock.release()


@contextmanager
def _temporary_note_path(root: Path) -> Iterator[Path]:
    fd, name = tempfile.mkstemp(prefix=".intensive-reading.", suffix=".tmp.md", dir=root)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        snapshot: _RegularFileSnapshot | None
        try:
            snapshot = _read_regular_snapshot(path)
        except FileNotFoundError:
            snapshot = None
        except (OSError, ValueError):
            raise ValueError("generated note temporary path is unsafe") from None
        if snapshot is not None:
            _retire_owned_temporary(
                path,
                snapshot.identity,
                error_code="ocr_evidence_invalid",
            )


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _validate_publication_metadata(
    metadata: dict[str, Any],
    *,
    input_fingerprint: str,
    evidence_fingerprint: str,
    chapter: dict[str, Any],
) -> None:
    fields = set(metadata)
    unexpected = fields - _PUBLICATION_METADATA_FIELDS
    if unexpected:
        raise ValueError("published metadata has unexpected fields")
    if fields != _PUBLICATION_METADATA_FIELDS:
        raise ValueError("published metadata is incomplete")
    if metadata.get("model") != "deepseek-v4-pro":
        raise ValueError("published model is invalid")
    if metadata.get("prompt_rules_version") != "mba-intensive-reading-v1":
        raise ValueError("published ruleset is invalid")
    if metadata.get("input_fingerprint") != input_fingerprint:
        raise ValueError("published input fingerprint is invalid")
    if metadata.get("chapter_fingerprint") != _chapter_fingerprint(chapter):
        raise ValueError("published chapter fingerprint is invalid")
    if metadata.get("evidence_fingerprint") != evidence_fingerprint:
        raise ValueError("published evidence fingerprint is invalid")
    if metadata.get("chapter_id") != chapter.get("id"):
        raise ValueError("published chapter id is invalid")
    if any(
        metadata.get(field) != chapter.get(chapter_field)
        for field, chapter_field in (
            ("chapter_number", "number"),
            ("chapter_title", "title"),
            ("page_start", "page_start"),
            ("page_end", "page_end"),
        )
    ):
        raise ValueError("published chapter metadata is invalid")
    source_id = metadata.get("source_id")
    prompt_fingerprint = metadata.get("prompt_fingerprint")
    citation_ids = metadata.get("citation_ids")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("published source id is invalid")
    if not isinstance(prompt_fingerprint, str) or not prompt_fingerprint:
        raise ValueError("published prompt fingerprint is invalid")
    if (
        not isinstance(citation_ids, list)
        or not citation_ids
        or any(
            not isinstance(citation, str)
            or not citation.startswith(f"[src:{source_id}:")
            or not citation.endswith("]")
            for citation in citation_ids
        )
    ):
        raise ValueError("published citation metadata is invalid")


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
    citation_ids = metadata.get("citation_ids")
    if not isinstance(citation_ids, list) or any(
        not isinstance(citation, str) or citation not in markdown for citation in citation_ids
    ):
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


def _read_matching_chapter_context(
    paths: WorkflowPaths,
    *,
    expected_tree: dict[str, Any] | None,
    expected_confirmation: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        tree = _read_regular_json(paths.chapter_tree)
        validate_chapter_tree(tree)
    except (OSError, ValueError) as exc:
        raise ValueError("published chapter tree is invalid") from exc
    if expected_tree is not None and tree != expected_tree:
        raise ValueError("published chapter tree changed during note generation")
    try:
        confirmation = _read_regular_json(paths.confirmation)
        validate_chapter_confirmation(confirmation, tree)
    except (OSError, ValueError) as exc:
        raise ValueError("published chapter confirmation is invalid") from exc
    if expected_confirmation is not None and confirmation != expected_confirmation:
        raise ValueError("published chapter confirmation changed during note generation")
    return tree, confirmation


def bind_published_note(
    final_path: str | Path,
    note_path: str | Path,
    metadata: dict[str, Any],
    *,
    expected_final: dict[str, Any] | None = None,
    expected_tree: dict[str, Any] | None = None,
    confirmation: dict[str, Any] | None = None,
    publication_path: str | Path | None = None,
    publication_lock: _PublicationLock | None = None,
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
    paths = WorkflowPaths(
        root=state_root,
        state=state_root / "batch-state.json",
        final=final_target,
        publication=publication_target,
        publication_lock=state_root / ".note-publication.lock",
        chapter_tree=state_root / "chapter-tree.json",
        confirmation=state_root / "chapter-confirmation.json",
        note=state_root / "intensive-reading.md",
    )
    if not isinstance(expected_final, dict):
        raise ValueError("expected OCR final is required")
    if publication_lock is not None:
        publication_lock = _require_active_publication_lock(publication_lock)
        if publication_lock.path != paths.publication_lock:
            raise ValueError("publication lock does not protect this state root")
    final = _read_regular_json(final_target)
    if final != expected_final:
        raise ValueError("OCR final changed during note generation")

    pages = _normalized_completed_pages(expected_final)
    detected_tree = detect_chapter_tree(pages, input_fingerprint=_input_fingerprint(expected_final))
    persisted_tree, persisted_confirmation = _read_matching_chapter_context(
        paths,
        expected_tree=expected_tree,
        expected_confirmation=confirmation,
    )
    if expected_tree is None:
        expected_tree = persisted_tree
    if expected_tree != detected_tree:
        raise ValueError("published chapter tree fingerprint is invalid")
    if confirmation is None:
        confirmation = persisted_confirmation
    validate_chapter_confirmation(confirmation, expected_tree)
    if confirmation.get("action") not in {"confirm", "edit"}:
        raise ValueError("published chapter confirmation is invalid")
    chapter = confirmation.get("chapter")
    if not isinstance(metadata, dict) or not isinstance(chapter, dict):
        raise ValueError("published metadata is invalid")
    _validate_publication_metadata(
        metadata,
        input_fingerprint=_input_fingerprint(expected_final),
        evidence_fingerprint=expected_tree["evidence_fingerprint"],
        chapter=chapter,
    )

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
    try:
        if publication_lock is not None:
            _require_active_publication_lock(publication_lock)
        if _read_regular_json(final_target) != expected_final:
            raise ValueError("OCR final changed during note generation")
        _read_matching_chapter_context(
            paths,
            expected_tree=expected_tree,
            expected_confirmation=confirmation,
        )
        _atomic_json(publication_target, publication)
        if publication_lock is not None:
            _require_active_publication_lock(publication_lock)
        if _read_regular_json(final_target) != expected_final:
            raise ValueError("OCR final changed during note generation")
        if _read_regular_json(publication_target) != publication:
            raise ValueError("OCR publication changed during note generation")
        if _read_regular_bytes(artifact_path) != content:
            raise ValueError("OCR publication artifact changed during note generation")
        published, error, published_path = _publication_status(expected_final, paths)
        if not published or error is not None or published_path != artifact_path:
            raise ValueError("OCR publication failed post-commit validation")
        if publication_lock is not None:
            _require_active_publication_lock(publication_lock)
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
    candidate_bytes = _canonical_json_bytes(candidate)
    candidate_snapshot = _require_bound_snapshot(
        path,
        candidate_bytes,
        None,
        error_code="ocr_evidence_invalid",
    )
    if previous is None:
        retained = path.with_name(f".{path.name}.{secrets.token_hex(16)}.rollback-retired")
        _retire_bound_path(
            path,
            retained,
            candidate_snapshot,
            error_code="ocr_evidence_invalid",
        )
        return
    _atomic_compare_exchange_bytes(
        path,
        expected=candidate_bytes,
        replacement=previous,
        error_code="ocr_evidence_invalid",
    )


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


def _migration_error_code(kind: object) -> str:
    return "ocr_evidence_invalid" if kind == "completed" else "ocr_state_invalid"


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _migration_journal_path(paths: WorkflowPaths) -> Path:
    return paths.root / _LEGACY_MIGRATION_JOURNAL_BASENAME


def _transaction_anchor_content(transaction_id: str) -> bytes:
    return f"ocr-v1-v2-transaction:{transaction_id}\n".encode("ascii")


def _migration_phases(journal: dict[str, Any]) -> tuple[str, ...]:
    phases = _LEGACY_MIGRATION_PHASES[str(journal["kind"])]
    if journal["kind"] == "completed" and journal["publication_sha256"] is None:
        return tuple(
            phase
            for phase in phases
            if phase not in {"artifact_published", "manifest_linked", "manifest_published"}
        )
    return phases


def _phase_at_least(journal: dict[str, Any], phase: str) -> bool:
    phases = _migration_phases(journal)
    return phase in phases and phases.index(str(journal["phase"])) >= phases.index(phase)


def _identity_pair(value: dict[str, Any], prefix: str) -> tuple[int, int] | None:
    device = value.get(f"{prefix}_dev")
    inode = value.get(f"{prefix}_ino")
    if device is None and inode is None:
        return None
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
    ):
        return None
    return device, inode


def _migration_journal_self_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload["journal_self_sha256"] = None
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _bind_migration_journal_identity(
    journal: dict[str, Any], identity: tuple[int, int]
) -> dict[str, Any]:
    journal = _validate_migration_journal(journal, allow_unbound_self=True)
    if any(
        journal[field] is not None
        for field in ("journal_self_dev", "journal_self_ino", "journal_self_sha256")
    ):
        raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
    bound = dict(journal)
    bound["journal_self_dev"], bound["journal_self_ino"] = identity
    bound["journal_self_sha256"] = None
    bound["journal_self_sha256"] = _migration_journal_self_hash(bound)
    return _validate_migration_journal(bound)


def _validate_migration_journal(
    value: object, *, allow_unbound_self: bool = False
) -> dict[str, Any]:
    kind = value.get("kind") if isinstance(value, dict) else None
    code = _migration_error_code(kind)
    if not isinstance(value, dict) or set(value) != _LEGACY_MIGRATION_JOURNAL_FIELDS:
        raise _LegacyMigrationError(code)
    if value.get("schema_version") != 1 or kind not in _LEGACY_MIGRATION_PHASES:
        raise _LegacyMigrationError(code)
    self_identity = _identity_pair(value, "journal_self")
    self_hash = value.get("journal_self_sha256")
    self_is_unbound = (
        value.get("journal_self_dev"),
        value.get("journal_self_ino"),
        self_hash,
    ) == (None, None, None)
    if self_is_unbound:
        if not allow_unbound_self:
            raise _LegacyMigrationError(code)
    elif (
        self_identity is None
        or not _is_sha256(self_hash)
        or self_hash != _migration_journal_self_hash(value)
    ):
        raise _LegacyMigrationError(code)
    expected_names = (
        ("batch-final.json", "batch-final.v1.backup.json")
        if kind == "completed"
        else ("batch-state.json", "batch-state.v1.backup.json")
    )
    if (
        (value.get("source_basename"), value.get("backup_basename")) != expected_names
        or not _is_sha256(value.get("transaction_id"))
        or not _is_sha256(value.get("transaction_anchor_sha256"))
        or not _is_sha256(value.get("lock_token"))
        or _identity_pair(value, "lock") is None
        or not _is_sha256(value.get("source_sha256"))
        or not _is_sha256(value.get("target_sha256"))
        or not isinstance(value.get("source_size"), int)
        or isinstance(value.get("source_size"), bool)
        or not 0 < value["source_size"] <= _MAX_ARTIFACT_BYTES
    ):
        raise _LegacyMigrationError(code)
    sidecar_tokens = value.get("sidecar_transaction_tokens")
    if (
        not isinstance(sidecar_tokens, dict)
        or set(sidecar_tokens) != set(_MIGRATION_SIDECAR_LABELS)
        or any(not _is_sha256(sidecar_tokens.get(label)) for label in _MIGRATION_SIDECAR_LABELS)
        or len(set(sidecar_tokens.values())) != len(_MIGRATION_SIDECAR_LABELS)
    ):
        raise _LegacyMigrationError(code)
    artifact_sha256 = value.get("artifact_sha256")
    publication_sha256 = value.get("publication_sha256")
    if (artifact_sha256 is None) != (publication_sha256 is None):
        raise _LegacyMigrationError(code)
    if artifact_sha256 is not None and (
        not _is_sha256(artifact_sha256) or not _is_sha256(publication_sha256)
    ):
        raise _LegacyMigrationError(code)
    if kind == "running" and publication_sha256 is not None:
        raise _LegacyMigrationError(code)
    phases = _migration_phases(value)
    if value.get("phase") not in phases:
        raise _LegacyMigrationError(code)
    identity_pairs = {
        prefix: _identity_pair(value, prefix)
        for prefix in (
            "transaction_anchor",
            "backup",
            "journal_previous",
            "target",
            "swap_replacement",
            "swap_expected",
            "swap_displaced",
            "artifact",
            "manifest_candidate",
            "manifest",
        )
    }
    for prefix, identity in identity_pairs.items():
        raw = (value.get(f"{prefix}_dev"), value.get(f"{prefix}_ino"))
        if (raw == (None, None)) != (identity is None):
            raise _LegacyMigrationError(code)
    anchor_basename = value.get("transaction_anchor_basename")
    anchor_expected_sha256 = hashlib.sha256(
        _transaction_anchor_content(str(value["transaction_id"]))
    ).hexdigest()
    if (
        anchor_basename != f".ocr-v1-v2-{value['transaction_id']}.anchor"
        or value.get("transaction_anchor_sha256") != anchor_expected_sha256
        or (_phase_at_least(value, "prepared"))
        != (identity_pairs["transaction_anchor"] is not None)
    ):
        raise _LegacyMigrationError(code)
    if _phase_at_least(value, "backup_published") != (identity_pairs["backup"] is not None):
        raise _LegacyMigrationError(code)
    previous_sha256 = value.get("journal_previous_sha256")
    has_previous = identity_pairs["journal_previous"] is not None
    if (previous_sha256 is not None) != has_previous or (
        has_previous and not _is_sha256(previous_sha256)
    ):
        raise _LegacyMigrationError(code)
    if (value.get("phase") != "intent") != has_previous:
        raise _LegacyMigrationError(code)
    swap_prepared = _phase_at_least(value, "source_swap_prepared")
    swap_basename = value.get("swap_path_basename")
    if swap_prepared:
        if (
            swap_basename != f".ocr-v1-v2-{value['transaction_id']}.swap"
            or identity_pairs["swap_replacement"] is None
            or identity_pairs["swap_expected"] is None
        ):
            raise _LegacyMigrationError(code)
    elif (
        swap_basename is not None
        or identity_pairs["swap_replacement"] is not None
        or identity_pairs["swap_expected"] is not None
    ):
        raise _LegacyMigrationError(code)
    target_phase = "final_published" if kind == "completed" else "state_published"
    if _phase_at_least(value, target_phase) != (identity_pairs["target"] is not None):
        raise _LegacyMigrationError(code)
    if _phase_at_least(value, target_phase) != (identity_pairs["swap_displaced"] is not None):
        raise _LegacyMigrationError(code)
    has_publication = publication_sha256 is not None
    candidate_basename = value.get("manifest_candidate_basename")
    if has_publication:
        transaction_id = str(value["transaction_id"])
        if (
            candidate_basename != f".ocr-v1-v2-{transaction_id}.manifest"
            or (_phase_at_least(value, "prepared"))
            != (identity_pairs["manifest_candidate"] is not None)
            or (_phase_at_least(value, "artifact_published"))
            != (identity_pairs["artifact"] is not None)
            or (_phase_at_least(value, "manifest_linked"))
            != (identity_pairs["manifest"] is not None)
        ):
            raise _LegacyMigrationError(code)
    elif (
        candidate_basename is not None
        or identity_pairs["artifact"] is not None
        or identity_pairs["manifest_candidate"] is not None
        or identity_pairs["manifest"] is not None
    ):
        raise _LegacyMigrationError(code)
    return value


def _migration_journal_stage_path(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    *,
    phase: str | None = None,
) -> Path:
    selected_phase = str(journal["phase"] if phase is None else phase)
    if selected_phase not in _migration_phases(journal):
        raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
    return paths.root / (f".ocr-v1-v2-{journal['transaction_id']}.journal-stage-{selected_phase}")


def _unbound_migration_journal(journal: dict[str, Any]) -> dict[str, Any]:
    unbound = dict(journal)
    unbound["journal_self_dev"] = None
    unbound["journal_self_ino"] = None
    unbound["journal_self_sha256"] = None
    return _validate_migration_journal(unbound, allow_unbound_self=True)


def _read_self_bound_migration_journal(
    path: Path,
    *,
    expected: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> tuple[dict[str, Any], _RegularFileSnapshot]:
    kind: object = None
    try:
        snapshot = _read_regular_snapshot(path)
        value = json.loads(snapshot.content.decode("utf-8"))
        kind = value.get("kind") if isinstance(value, dict) else None
        journal = _validate_migration_journal(value)
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        _LegacyMigrationError,
    ):
        raise _LegacyMigrationError(error_code or _migration_error_code(kind)) from None
    code = error_code or _migration_error_code(journal["kind"])
    if (
        snapshot.content != _canonical_json_bytes(journal)
        or snapshot.identity != _identity_pair(journal, "journal_self")
        or (expected is not None and journal != expected)
    ):
        raise _LegacyMigrationError(code)
    return journal, snapshot


def _write_self_bound_migration_journal_candidate(
    path: Path,
    journal: dict[str, Any],
    *,
    error_code: str,
) -> tuple[dict[str, Any], _RegularFileSnapshot]:
    draft = _unbound_migration_journal(journal)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing, snapshot = _read_self_bound_migration_journal(
            path,
            error_code=error_code,
        )
        if _unbound_migration_journal(existing) != draft:
            raise _LegacyMigrationError(error_code) from None
        try:
            _fsync_directory(path.parent)
        except OSError:
            raise _LegacyMigrationError(error_code) from None
        return existing, snapshot
    except OSError:
        raise _LegacyMigrationError(error_code) from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _LegacyMigrationError(error_code)
        bound = _bind_migration_journal_identity(draft, (info.st_dev, info.st_ino))
        content = _canonical_json_bytes(bound)
        _write_all(fd, content)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        raise
    else:
        os.close(fd)

    bound, snapshot = _read_self_bound_migration_journal(
        path,
        expected=bound,
        error_code=error_code,
    )
    try:
        _fsync_directory(path.parent)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    return bound, snapshot


def _unsealed_journal_stage_paths(paths: WorkflowPaths) -> tuple[Path, ...]:
    pattern = re.compile(r"\.ocr-v1-v2-([0-9a-f]{64})\.journal-stage-[a-z_]+")
    try:
        candidates = tuple(
            path
            for path in paths.root.iterdir()
            if (match := pattern.fullmatch(path.name)) is not None
            and not (paths.root / f".ocr-v1-v2-{match.group(1)}.sealed").exists()
        )
    except OSError:
        raise _LegacyMigrationError("ocr_state_invalid") from None
    return candidates


def _transaction_anchor_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    basename = journal.get("transaction_anchor_basename")
    if not isinstance(basename, str) or Path(basename).name != basename:
        raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
    return paths.root / basename


def _validate_transaction_anchor(paths: WorkflowPaths, journal: dict[str, Any]) -> None:
    code = _migration_error_code(journal["kind"])
    active = _transaction_anchor_path(paths, journal)
    retained = _migration_sidecar_path(paths, journal, "anchor-retired")
    try:
        active_anchor = _read_regular_snapshot(active)
    except FileNotFoundError:
        active_anchor = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    try:
        retained_anchor = _read_regular_snapshot(retained)
    except FileNotFoundError:
        retained_anchor = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if active_anchor is not None and retained_anchor is not None:
        raise _LegacyMigrationError(code)
    anchor = active_anchor if active_anchor is not None else retained_anchor
    if anchor is None or (active_anchor is None and not _phase_at_least(journal, "commit_ready")):
        raise _LegacyMigrationError(code)
    if (
        anchor.identity != _identity_pair(journal, "transaction_anchor")
        or hashlib.sha256(anchor.content).hexdigest() != journal["transaction_anchor_sha256"]
    ):
        raise _LegacyMigrationError(code)


def _validate_migration_lock(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> None:
    code = _migration_error_code(journal["kind"])
    try:
        publication_lock = _require_active_publication_lock(publication_lock)
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if (
        _identity_pair(journal, "lock")
        != (
            publication_lock.device,
            publication_lock.inode,
        )
        or journal["lock_token"] != publication_lock.token
    ):
        raise _LegacyMigrationError(code)
    if _phase_at_least(journal, "prepared"):
        _validate_transaction_anchor(paths, journal)


def _validate_journal_predecessor(paths: WorkflowPaths, journal: dict[str, Any]) -> None:
    expected_identity = _identity_pair(journal, "journal_previous")
    expected_sha256 = journal.get("journal_previous_sha256")
    if expected_identity is None:
        if expected_sha256 is not None:
            raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
        return
    code = _migration_error_code(journal["kind"])
    try:
        _previous, candidate = _read_self_bound_migration_journal(
            _migration_journal_stage_path(paths, journal),
            error_code=code,
        )
    except _LegacyMigrationError:
        raise
    if (
        candidate.identity != expected_identity
        or hashlib.sha256(candidate.content).hexdigest() != expected_sha256
    ):
        raise _LegacyMigrationError(code)


def _pending_commit_witness_path(paths: WorkflowPaths) -> Path | None:
    pattern = re.compile(r"\.ocr-v1-v2-([0-9a-f]{64})\.(?:commit|done)")
    try:
        candidates = tuple(path for path in paths.root.iterdir() if pattern.fullmatch(path.name))
    except OSError:
        raise _LegacyMigrationError("ocr_state_invalid") from None
    if len(candidates) > 1:
        raise _LegacyMigrationError("ocr_state_invalid")
    return candidates[0] if candidates else None


def _restore_exact_retired_path(
    active: Path,
    retired: Path,
    expected: _RegularFileSnapshot,
    *,
    error_code: str,
) -> _RegularFileSnapshot:
    try:
        active_snapshot = _read_regular_snapshot(active)
    except FileNotFoundError:
        active_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    try:
        retired_snapshot = _read_regular_snapshot(retired)
    except FileNotFoundError:
        retired_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if active_snapshot is not None and retired_snapshot is not None:
        raise _LegacyMigrationError(error_code)
    if active_snapshot is not None:
        if (
            active_snapshot.identity != expected.identity
            or active_snapshot.content != expected.content
        ):
            raise _LegacyMigrationError(error_code)
        return active_snapshot
    if retired_snapshot is None:
        raise _LegacyMigrationError(error_code)
    if (
        retired_snapshot.identity != expected.identity
        or retired_snapshot.content != expected.content
    ):
        _restore_retained_path(retired, active)
        raise _LegacyMigrationError(error_code)
    try:
        _rename_no_replace(retired, active)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    try:
        _fsync_directory(active.parent)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    return _require_bound_snapshot(
        active,
        expected.content,
        expected.identity,
        error_code=error_code,
    )


def _recover_pending_commit(
    paths: WorkflowPaths,
    publication_lock: _PublicationLock,
) -> None:
    witness_path = _pending_commit_witness_path(paths)
    if witness_path is None:
        return
    try:
        witness_snapshot = _read_regular_snapshot(witness_path)
        witness_value = json.loads(witness_snapshot.content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise _LegacyMigrationError("ocr_state_invalid") from None
    witness = _validate_commit_witness(witness_value)
    journal = witness["journal"]
    code = _migration_error_code(journal["kind"])
    witness_active = _commit_witness_path(paths, journal)
    witness_done = _commit_done_path(paths, journal)
    if witness_path not in {witness_active, witness_done}:
        raise _LegacyMigrationError(code)
    _validate_migration_lock(paths, journal, publication_lock)
    _validate_journal_predecessor(paths, journal)
    expected_content = _canonical_json_bytes(journal)
    expected_identity = (witness["journal_dev"], witness["journal_ino"])
    if witness_path == witness_done:
        _restore_exact_retired_path(
            witness_active,
            witness_done,
            witness_snapshot,
            error_code=code,
        )
    _restore_exact_retired_path(
        _migration_journal_path(paths),
        _migration_sidecar_path(paths, journal, "journal-retired"),
        _RegularFileSnapshot(
            content=expected_content,
            device=expected_identity[0],
            inode=expected_identity[1],
        ),
        error_code=code,
    )
    _read_self_bound_migration_journal(
        _migration_journal_path(paths),
        expected=journal,
        error_code=code,
    )


def _read_migration_journal(
    paths: WorkflowPaths, publication_lock: _PublicationLock
) -> dict[str, Any] | None:
    try:
        journal, _snapshot = _read_self_bound_migration_journal(_migration_journal_path(paths))
    except FileNotFoundError:
        journal = None
    except _LegacyMigrationError:
        try:
            _migration_journal_path(paths).lstat()
        except FileNotFoundError:
            journal = None
        except OSError:
            raise _LegacyMigrationError("ocr_state_invalid") from None
        else:
            raise
    if journal is None:
        candidates = _unsealed_journal_stage_paths(paths)
        if not candidates:
            return None
        kind: object = None
        if len(candidates) == 1:
            try:
                candidate = _read_regular_json(candidates[0])
            except (OSError, ValueError):
                candidate = None
            if isinstance(candidate, dict):
                kind = candidate.get("kind")
        raise _LegacyMigrationError(_migration_error_code(kind))
    _validate_migration_lock(paths, journal, publication_lock)
    _validate_journal_predecessor(paths, journal)
    return journal


def _write_new_migration_journal(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> dict[str, Any]:
    journal = _validate_migration_journal(journal, allow_unbound_self=True)
    _validate_migration_lock(paths, journal, publication_lock)
    path = _migration_journal_path(paths)
    code = _migration_error_code(journal["kind"])
    stage = _migration_journal_stage_path(paths, journal)
    journal, candidate = _write_self_bound_migration_journal_candidate(
        stage,
        journal,
        error_code=code,
    )
    content = candidate.content
    _migration_checkpoint(f"{journal['kind']}_journal_identity_published")
    try:
        _rename_no_replace(stage, path)
    except OSError:
        raise _LegacyMigrationError(code) from None
    try:
        _fsync_directory(path.parent)
    except OSError:
        _require_bound_snapshot(
            path,
            content,
            candidate.identity,
            error_code=code,
        )
        _require_bound_snapshot(
            path,
            content,
            candidate.identity,
            error_code=code,
        )
        _validate_migration_lock(paths, journal, publication_lock)
        raise _MigrationJournalPublicationUncertain(code) from None
    published, published_snapshot = _read_self_bound_migration_journal(
        path,
        expected=journal,
        error_code=code,
    )
    if published_snapshot.identity != candidate.identity:
        raise _LegacyMigrationError(code)
    _validate_migration_lock(paths, journal, publication_lock)
    return published


def _advance_migration_journal(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    phase: str,
    publication_lock: _PublicationLock,
    **updates: object,
) -> dict[str, Any]:
    path = _migration_journal_path(paths)
    code = _migration_error_code(journal["kind"])
    journal = _validate_migration_journal(journal)
    _validate_migration_lock(paths, journal, publication_lock)
    current_journal, current = _read_self_bound_migration_journal(
        path,
        expected=journal,
        error_code=code,
    )
    phases = _migration_phases(journal)
    if phase not in phases:
        raise _LegacyMigrationError(code)
    current_index = phases.index(str(journal["phase"]))
    target_index = phases.index(phase)
    if current_index > target_index or (current_index == target_index and updates):
        raise _LegacyMigrationError(code)
    if current_index == target_index:
        return journal
    updated = dict(journal)
    updated["phase"] = phase
    updated.update(updates)
    updated.update(_snapshot_identity_updates("journal_previous", current))
    updated["journal_previous_sha256"] = hashlib.sha256(current.content).hexdigest()
    updated = _unbound_migration_journal(updated)
    stage = _migration_journal_stage_path(paths, updated)
    updated, candidate = _write_self_bound_migration_journal_candidate(
        stage,
        updated,
        error_code=code,
    )
    try:
        _rename_swap(stage, path)
    except OSError:
        raise _LegacyMigrationError(code) from None
    try:
        _fsync_directory(path.parent)
    except OSError:
        raise _LegacyMigrationError(code) from None
    try:
        committed_journal, committed = _read_self_bound_migration_journal(
            path,
            expected=updated,
            error_code=code,
        )
        displaced_journal, displaced = _read_self_bound_migration_journal(
            stage,
            expected=current_journal,
            error_code=code,
        )
    except _LegacyMigrationError:
        _rollback_swap(
            stage,
            path,
            committed_identity=candidate.identity,
        )
        raise
    if committed.identity != candidate.identity or displaced.identity != current.identity:
        _rollback_swap(
            stage,
            path,
            committed_identity=candidate.identity,
        )
        raise _LegacyMigrationError(code)
    _validate_journal_predecessor(paths, updated)
    _validate_migration_lock(paths, updated, publication_lock)
    return committed_journal


def _commit_witness_payload(
    journal: dict[str, Any], journal_snapshot: _RegularFileSnapshot
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "journal": journal,
        "journal_sha256": hashlib.sha256(journal_snapshot.content).hexdigest(),
        "journal_dev": journal_snapshot.device,
        "journal_ino": journal_snapshot.inode,
    }


def _validate_commit_witness(value: object) -> dict[str, Any]:
    kind = None
    if isinstance(value, dict) and isinstance(value.get("journal"), dict):
        kind = value["journal"].get("kind")
    code = _migration_error_code(kind)
    if not isinstance(value, dict) or set(value) != _COMMIT_WITNESS_FIELDS:
        raise _LegacyMigrationError(code)
    journal = _validate_migration_journal(value.get("journal"))
    if journal["phase"] != "commit_ready":
        raise _LegacyMigrationError(code)
    journal_bytes = _canonical_json_bytes(journal)
    if (
        value.get("schema_version") != 1
        or value.get("journal_sha256") != hashlib.sha256(journal_bytes).hexdigest()
        or not isinstance(value.get("journal_dev"), int)
        or isinstance(value.get("journal_dev"), bool)
        or value["journal_dev"] < 0
        or not isinstance(value.get("journal_ino"), int)
        or isinstance(value.get("journal_ino"), bool)
        or value["journal_ino"] <= 0
        or _identity_pair(journal, "journal_self") != (value["journal_dev"], value["journal_ino"])
    ):
        raise _LegacyMigrationError(code)
    return value


def _commit_witness_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    return _migration_sidecar_path(paths, journal, "commit")


def _commit_done_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    return _migration_sidecar_path(paths, journal, "done")


def _commit_sealed_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    return _migration_sidecar_path(paths, journal, "sealed")


def _finalized_migration_receipt_paths(paths: WorkflowPaths) -> tuple[Path, ...]:
    pattern = re.compile(r"\.ocr-v1-v2-[0-9a-f]{64}\.sealed")
    try:
        return tuple(path for path in paths.root.iterdir() if pattern.fullmatch(path.name))
    except FileNotFoundError:
        return ()
    except OSError:
        raise _LegacyMigrationError("ocr_state_invalid") from None


def _validate_migration_sidecar_receipt_chain(
    owner: _MigrationCandidateOwner,
) -> dict[str, Any]:
    try:
        receipt, snapshot = _read_migration_sidecar_receipt(_migration_sidecar_receipt_path(owner))
        current = receipt
        current_snapshot = snapshot
        while int(current["sequence"]) > 0:
            sequence = int(current["sequence"])
            previous, previous_snapshot = _read_migration_sidecar_receipt(
                _migration_sidecar_receipt_stage_path(owner, sequence)
            )
            if (
                previous_snapshot.identity != _identity_pair(current, "receipt_previous")
                or hashlib.sha256(previous_snapshot.content).hexdigest()
                != current["receipt_previous_sha256"]
                or int(previous["sequence"]) != sequence - 1
                or previous["transaction_id"] != current["transaction_id"]
                or previous["transaction_token"] != current["transaction_token"]
                or previous["label"] != current["label"]
            ):
                raise ValueError("migration sidecar receipt chain is invalid")
            current = previous
            current_snapshot = previous_snapshot
        if current["stage"] != "declared" or current_snapshot.identity != _identity_pair(
            current, "receipt_self"
        ):
            raise ValueError("migration sidecar receipt chain is invalid")
        return receipt
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(_migration_error_code(owner.kind)) from None


def _validate_finalized_migration_sidecars(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    sealed_path: Path,
    sealed_snapshot: _RegularFileSnapshot,
) -> None:
    code = _migration_error_code(journal["kind"])
    active_labels = {"anchor", "backup", "swap", "commit"}
    if journal["publication_sha256"] is not None:
        active_labels.update({"manifest", "artifact"})
    expected_targets = {
        "anchor": str(journal["transaction_anchor_basename"]),
        "backup": str(journal["backup_basename"]),
        "swap": str(journal["swap_path_basename"]),
        "commit": _commit_witness_path(paths, journal).name,
    }
    expected_hashes = {
        "anchor": str(journal["transaction_anchor_sha256"]),
        "backup": str(journal["source_sha256"]),
        "swap": str(journal["target_sha256"]),
        "commit": hashlib.sha256(sealed_snapshot.content).hexdigest(),
    }
    expected_identities = {
        "anchor": _identity_pair(journal, "transaction_anchor"),
        "backup": _identity_pair(journal, "backup"),
        "swap": _identity_pair(journal, "swap_replacement"),
        "commit": sealed_snapshot.identity,
    }
    final_paths = {
        "anchor": _migration_sidecar_path(paths, journal, "anchor-retired"),
        "backup": paths.root / str(journal["backup_basename"]),
        "swap": paths.final if journal["kind"] == "completed" else paths.state,
        "commit": sealed_path,
    }
    if journal["publication_sha256"] is not None:
        artifact_sha256 = str(journal["artifact_sha256"])
        expected_targets.update(
            {
                "manifest": str(journal["manifest_candidate_basename"]),
                "artifact": f"intensive-reading.{artifact_sha256}.md",
            }
        )
        expected_hashes.update(
            {
                "manifest": str(journal["publication_sha256"]),
                "artifact": artifact_sha256,
            }
        )
        expected_identities.update(
            {
                "manifest": _identity_pair(journal, "manifest_candidate"),
                "artifact": _identity_pair(journal, "artifact"),
            }
        )
        final_paths.update(
            {
                "manifest": _migration_sidecar_path(paths, journal, "manifest-retired"),
                "artifact": paths.root / f"intensive-reading.{artifact_sha256}.md",
            }
        )
    for label in _MIGRATION_SIDECAR_LABELS:
        owner = _migration_candidate_owner(paths, journal, label)
        if label not in active_labels:
            try:
                _migration_sidecar_receipt_path(owner).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise _LegacyMigrationError(code) from None
            raise _LegacyMigrationError(code)
        receipt = _validate_migration_sidecar_receipt_chain(owner)
        expected_identity = expected_identities[label]
        if (
            receipt["stage"] != "published"
            or receipt["target_basename"] != expected_targets[label]
            or receipt["content_sha256"] != expected_hashes[label]
            or _identity_pair(receipt, "published") != expected_identity
            or expected_identity is None
        ):
            raise _LegacyMigrationError(code)
        allowed_link_counts = (2,) if label == "manifest" else (1,)
        if label == "swap" and journal["kind"] == "running":
            try:
                current_swap = _read_regular_snapshot(final_paths[label])
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                raise _LegacyMigrationError(code) from None
            if current_swap.identity != expected_identity:
                continue
        try:
            published = _read_regular_snapshot(
                final_paths[label],
                allowed_link_counts=allowed_link_counts,
            )
            info = final_paths[label].lstat()
        except (FileNotFoundError, OSError, ValueError):
            raise _LegacyMigrationError(code) from None
        if (
            published.identity != expected_identity
            or hashlib.sha256(published.content).hexdigest() != expected_hashes[label]
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != _migration_owner_uid()
            or (info.st_dev, info.st_ino) != published.identity
        ):
            raise _LegacyMigrationError(code)


def _validate_finalized_migration_receipt(
    paths: WorkflowPaths,
    receipt_path: Path,
    receipt_snapshot: _RegularFileSnapshot,
    receipt: dict[str, Any],
) -> None:
    journal = receipt["journal"]
    code = _migration_error_code(journal["kind"])
    if receipt_path != _commit_sealed_path(paths, journal):
        raise _LegacyMigrationError(code)
    expected_receipt = _canonical_json_bytes(receipt)
    if receipt_snapshot.content != expected_receipt:
        raise _LegacyMigrationError(code)
    _validate_transaction_anchor(paths, journal)
    _validate_journal_predecessor(paths, journal)
    _retired_journal, retired_snapshot = _read_self_bound_migration_journal(
        _migration_sidecar_path(paths, journal, "journal-retired"),
        expected=journal,
        error_code=code,
    )
    if retired_snapshot.identity != (receipt["journal_dev"], receipt["journal_ino"]):
        raise _LegacyMigrationError(code)
    backup_identity = _identity_pair(journal, "backup")
    if backup_identity is None:
        raise _LegacyMigrationError(code)
    backup = _require_bound_snapshot(
        paths.root / str(journal["backup_basename"]),
        _read_regular_bytes(paths.root / str(journal["backup_basename"])),
        backup_identity,
        error_code=code,
    )
    if (
        len(backup.content) != journal["source_size"]
        or hashlib.sha256(backup.content).hexdigest() != journal["source_sha256"]
    ):
        raise _LegacyMigrationError(code)
    if journal["kind"] != "completed":
        _validate_finalized_migration_sidecars(
            paths,
            journal,
            receipt_path,
            receipt_snapshot,
        )
        return
    final_identity = _identity_pair(journal, "target")
    if final_identity is None:
        raise _LegacyMigrationError(code)
    final_snapshot = _read_regular_snapshot(paths.final)
    if final_snapshot.identity != final_identity:
        raise _LegacyMigrationError(code)
    try:
        final_value = json.loads(final_snapshot.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _LegacyMigrationError(code) from None
    if (
        not isinstance(final_value, dict)
        or _json_fingerprint(final_value) != journal["target_sha256"]
    ):
        raise _LegacyMigrationError(code)
    if journal["publication_sha256"] is None:
        _validate_finalized_migration_sidecars(
            paths,
            journal,
            receipt_path,
            receipt_snapshot,
        )
        return
    manifest_identity = _identity_pair(journal, "manifest")
    candidate_identity = _identity_pair(journal, "manifest_candidate")
    artifact_identity = _identity_pair(journal, "artifact")
    if manifest_identity is None or candidate_identity is None or artifact_identity is None:
        raise _LegacyMigrationError(code)
    try:
        manifest = _read_regular_snapshot(paths.publication, allowed_link_counts=(2,))
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if manifest.identity != manifest_identity:
        raise _LegacyMigrationError(code)
    try:
        publication = json.loads(manifest.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _LegacyMigrationError(code) from None
    if (
        not isinstance(publication, dict)
        or _json_fingerprint(publication) != journal["publication_sha256"]
    ):
        raise _LegacyMigrationError(code)
    candidate = _require_bound_snapshot(
        _migration_sidecar_path(paths, journal, "manifest-retired"),
        manifest.content,
        candidate_identity,
        error_code=code,
        allowed_link_counts=(2,),
    )
    if candidate.identity != manifest.identity:
        raise _LegacyMigrationError(code)
    artifact_sha256 = str(journal["artifact_sha256"])
    artifact = _require_bound_snapshot(
        paths.root / f"intensive-reading.{artifact_sha256}.md",
        _read_regular_bytes(paths.root / f"intensive-reading.{artifact_sha256}.md"),
        artifact_identity,
        error_code=code,
    )
    if hashlib.sha256(artifact.content).hexdigest() != artifact_sha256:
        raise _LegacyMigrationError(code)
    _validate_finalized_migration_sidecars(
        paths,
        journal,
        receipt_path,
        receipt_snapshot,
    )


def _validate_finalized_migration_receipts(paths: WorkflowPaths) -> None:
    for receipt_path in _finalized_migration_receipt_paths(paths):
        try:
            receipt_snapshot = _read_regular_snapshot(receipt_path)
            receipt_value = json.loads(receipt_snapshot.content.decode("utf-8"))
            receipt = _validate_commit_witness(receipt_value)
        except (
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            _LegacyMigrationError,
        ):
            raise _LegacyMigrationError("ocr_state_invalid") from None
        _validate_finalized_migration_receipt(
            paths,
            receipt_path,
            receipt_snapshot,
            receipt,
        )


def _ensure_commit_witness(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> _RegularFileSnapshot:
    code = _migration_error_code(journal["kind"])
    journal_path = _migration_journal_path(paths)
    _bound_journal, journal_snapshot = _read_self_bound_migration_journal(
        journal_path,
        expected=journal,
        error_code=code,
    )
    payload = _commit_witness_payload(journal, journal_snapshot)
    content = _canonical_json_bytes(payload)
    path = _commit_witness_path(paths, journal)
    owner = _migration_candidate_owner(paths, journal, "commit", publication_lock)
    try:
        existing = _write_regular_no_replace(
            path,
            content,
            error_code=code,
            candidate_owner=owner,
        )
    except (OSError, ValueError, _LegacyMigrationError):
        raise _LegacyMigrationError(code) from None
    if existing.content != content:
        raise _LegacyMigrationError(code)
    return existing


def _restore_unfinalized_commit(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    journal_snapshot: _RegularFileSnapshot,
    witness: _RegularFileSnapshot,
) -> None:
    code = _migration_error_code(journal["kind"])
    witness_active = _commit_witness_path(paths, journal)
    witness_done = _commit_done_path(paths, journal)
    witness_sealed = _commit_sealed_path(paths, journal)
    try:
        sealed_snapshot = _read_regular_snapshot(witness_sealed)
    except FileNotFoundError:
        sealed_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if sealed_snapshot is not None:
        _restore_exact_retired_path(
            witness_done,
            witness_sealed,
            witness,
            error_code=code,
        )
    _restore_exact_retired_path(
        witness_active,
        witness_done,
        witness,
        error_code=code,
    )
    _restore_exact_retired_path(
        _migration_journal_path(paths),
        _migration_sidecar_path(paths, journal, "journal-retired"),
        journal_snapshot,
        error_code=code,
    )


def _remove_migration_journal(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
    *,
    original: bytes,
    publication: dict[str, Any] | None,
    validate_bindings: Callable[[], None],
) -> None:
    code = _migration_error_code(journal["kind"])
    if journal["phase"] != "commit_ready":
        raise _LegacyMigrationError(code)
    _validate_migration_lock(paths, journal, publication_lock)
    _validate_journal_predecessor(paths, journal)
    _bound_journal, journal_snapshot = _read_self_bound_migration_journal(
        _migration_journal_path(paths),
        expected=journal,
        error_code=code,
    )
    witness = _ensure_commit_witness(paths, journal, publication_lock)

    displaced_identity = _identity_pair(journal, "swap_displaced")
    if displaced_identity is None:
        raise _LegacyMigrationError(code)
    swap_expected = _RegularFileSnapshot(
        content=original,
        device=displaced_identity[0],
        inode=displaced_identity[1],
    )
    _retire_bound_path(
        _source_swap_path(paths, journal),
        _migration_sidecar_path(paths, journal, "swap-retired"),
        swap_expected,
        error_code=code,
    )

    if publication is not None:
        candidate_identity = _identity_pair(journal, "manifest_candidate")
        if candidate_identity is None:
            raise _LegacyMigrationError(code)
        candidate_expected = _RegularFileSnapshot(
            content=_canonical_json_bytes(publication),
            device=candidate_identity[0],
            inode=candidate_identity[1],
        )
        _retire_bound_path(
            _manifest_candidate_path(paths, journal),
            _migration_sidecar_path(paths, journal, "manifest-retired"),
            candidate_expected,
            error_code=code,
            allowed_link_counts=(2,),
        )

    anchor_identity = _identity_pair(journal, "transaction_anchor")
    if anchor_identity is None:
        raise _LegacyMigrationError(code)
    anchor_path = _transaction_anchor_path(paths, journal)
    anchor_location, anchor = _snapshot_active_or_retained_by_identity(
        anchor_path,
        _migration_sidecar_path(paths, journal, "anchor-retired"),
        anchor_identity,
        error_code=code,
        allow_retained=True,
    )
    if hashlib.sha256(anchor.content).hexdigest() != journal["transaction_anchor_sha256"]:
        raise _LegacyMigrationError(code)
    if anchor_location == anchor_path:
        _retire_bound_path(
            anchor_path,
            _migration_sidecar_path(paths, journal, "anchor-retired"),
            anchor,
            error_code=code,
        )

    _validate_migration_lock(paths, journal, publication_lock)
    validate_bindings()
    _migration_checkpoint(f"{journal['kind']}_cleanup_ready")
    validate_bindings()
    _require_bound_snapshot(
        _migration_journal_path(paths),
        journal_snapshot.content,
        journal_snapshot.identity,
        error_code=code,
    )
    _require_bound_snapshot(
        _commit_witness_path(paths, journal),
        witness.content,
        witness.identity,
        error_code=code,
    )
    _migration_checkpoint(f"{journal['kind']}_before_journal_commit")
    commit_started = True
    try:
        _retire_bound_path(
            _migration_journal_path(paths),
            _migration_sidecar_path(paths, journal, "journal-retired"),
            journal_snapshot,
            error_code=code,
        )
        _migration_checkpoint(f"{journal['kind']}_journal_committed")
        validate_bindings()
        _require_bound_snapshot(
            _migration_sidecar_path(paths, journal, "journal-retired"),
            journal_snapshot.content,
            journal_snapshot.identity,
            error_code=code,
        )
        _require_bound_snapshot(
            _commit_witness_path(paths, journal),
            witness.content,
            witness.identity,
            error_code=code,
        )
        _retire_bound_path(
            _commit_witness_path(paths, journal),
            _commit_done_path(paths, journal),
            witness,
            error_code=code,
        )
        _migration_checkpoint(f"{journal['kind']}_witness_done")
        validate_bindings()
        _require_bound_snapshot(
            _migration_sidecar_path(paths, journal, "journal-retired"),
            journal_snapshot.content,
            journal_snapshot.identity,
            error_code=code,
        )
        _require_bound_snapshot(
            _commit_done_path(paths, journal),
            witness.content,
            witness.identity,
            error_code=code,
        )
        _retire_bound_path(
            _commit_done_path(paths, journal),
            _commit_sealed_path(paths, journal),
            witness,
            error_code=code,
        )
        _migration_checkpoint(f"{journal['kind']}_commit_sealed")
    except Exception:
        if commit_started:
            try:
                _restore_unfinalized_commit(
                    paths,
                    journal,
                    journal_snapshot,
                    witness,
                )
            except _LegacyMigrationError:
                pass
        raise _LegacyMigrationError(code) from None


def _write_legacy_backup(
    path: Path,
    content: bytes,
    *,
    error_code: str,
    candidate_owner: _MigrationCandidateOwner | None = None,
) -> _RegularFileSnapshot:
    if candidate_owner is not None:
        return _write_regular_no_replace(
            path,
            content,
            error_code=error_code,
            candidate_owner=candidate_owner,
        )
    try:
        existing = _read_regular_snapshot(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    else:
        if existing.content != content:
            raise _LegacyMigrationError(error_code)
        return existing
    temporary, candidate = _write_temporary_regular(
        path.parent,
        prefix=f".{path.name}.",
        content=content,
        error_code=error_code,
        candidate_owner=candidate_owner,
    )
    published_by_us = False
    try:
        try:
            _rename_no_replace(temporary, path)
        except FileExistsError:
            try:
                existing = _read_regular_snapshot(path)
            except (FileNotFoundError, OSError, ValueError):
                raise _LegacyMigrationError(error_code) from None
            if existing.content != content:
                raise _LegacyMigrationError(error_code) from None
            return existing
        except OSError:
            raise _LegacyMigrationError(error_code) from None
        published_by_us = True
        try:
            _fsync_directory(path.parent)
        except OSError:
            _require_bound_snapshot(
                path,
                content,
                candidate.identity,
                error_code=error_code,
            )
            raise _NoReplacePublicationUncertain(error_code, candidate.identity) from None
        try:
            existing = _read_regular_snapshot(path)
        except (FileNotFoundError, OSError, ValueError):
            raise _LegacyMigrationError(error_code) from None
        if existing.content != content or existing.identity != candidate.identity:
            raise _LegacyMigrationError(error_code) from None
        return existing
    finally:
        try:
            _retire_owned_temporary(
                temporary,
                candidate.identity,
                error_code=error_code,
            )
        except _LegacyMigrationError:
            if published_by_us:
                raise _NoReplacePublicationUncertain(
                    error_code,
                    candidate.identity,
                ) from None
            raise


def _decode_legacy_json(content: bytes, *, error_code: str) -> dict[str, Any]:
    try:
        value: object = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise _LegacyMigrationError(error_code) from None
    if not isinstance(value, dict):
        raise _LegacyMigrationError(error_code)
    return value


def _completed_migration_material(
    legacy: dict[str, Any], source_path: Path, paths: WorkflowPaths
) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None]:
    migrated = _migrate_legacy_completed_final(legacy, source_path=source_path)
    plan = _legacy_publication_plan(legacy, migrated, paths)
    if plan is None:
        return migrated, None, None
    content, metadata, tree = plan
    artifact_sha256 = hashlib.sha256(content).hexdigest()
    publication = {
        "schema_version": 1,
        "final_snapshot_sha256": _json_fingerprint(migrated),
        "artifact_basename": f"intensive-reading.{artifact_sha256}.md",
        "artifact_sha256": artifact_sha256,
        "proposal_fingerprint": tree["proposal_fingerprint"],
        "metadata": metadata,
    }
    return migrated, content, publication


def _legacy_running_target(legacy: dict[str, Any], source_path: Path) -> dict[str, Any]:
    if set(legacy) & _LEGACY_V1_PUBLICATION_FIELDS or not _legacy_v1_core_is_valid(
        legacy, source_path, expected_status=BatchStatus.RUNNING.value
    ):
        raise _LegacyMigrationError("ocr_state_invalid")
    try:
        run_config = _legacy_v1_run_config(legacy)
    except _LegacyMigrationError:
        raise _LegacyMigrationError("ocr_state_invalid") from None
    pages = list(run_config["pages"])
    target = {
        "schema_version": 2,
        "status": BatchStatus.RUNNING.value,
        "input_fingerprint": legacy["input_fingerprint"],
        "pdf_snapshot": legacy["pdf_snapshot"],
        "run_config": run_config,
        "pages": {str(page): {"status": "pending"} for page in pages},
        "updated_at": legacy["updated_at"],
    }
    if not _is_batch_state(target) or target["pdf_snapshot"] != _snapshot_pdf(source_path):
        raise _LegacyMigrationError("ocr_state_invalid")
    return dict(target)


def _create_transaction_anchor(
    paths: WorkflowPaths,
    transaction_id: str,
    *,
    error_code: str,
    candidate_owner: _MigrationCandidateOwner,
) -> tuple[Path, _RegularFileSnapshot]:
    if candidate_owner.transaction_id != transaction_id or candidate_owner.label != "anchor":
        raise _LegacyMigrationError(error_code)
    path = paths.root / f".ocr-v1-v2-{transaction_id}.anchor"
    content = _transaction_anchor_content(transaction_id)
    snapshot = _write_regular_no_replace(
        path,
        content,
        error_code=error_code,
        candidate_owner=candidate_owner,
    )
    return path, snapshot


def _create_manifest_candidate(
    paths: WorkflowPaths,
    transaction_id: str,
    publication: dict[str, Any],
    *,
    candidate_owner: _MigrationCandidateOwner,
) -> tuple[Path, _RegularFileSnapshot]:
    if candidate_owner.transaction_id != transaction_id or candidate_owner.label != "manifest":
        raise _LegacyMigrationError("ocr_evidence_invalid")
    path = paths.root / f".ocr-v1-v2-{transaction_id}.manifest"
    content = _canonical_json_bytes(publication)
    snapshot = _write_regular_no_replace(
        path,
        content,
        error_code="ocr_evidence_invalid",
        candidate_owner=candidate_owner,
    )
    return path, snapshot


def _build_migration_journal(
    *,
    publication_lock: _PublicationLock,
    transaction_id: str,
    kind: str,
    source_basename: str,
    backup_basename: str,
    original: bytes,
    target: dict[str, Any],
    content: bytes | None = None,
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_identity = publication_lock.journal_identity()
    anchor_content = _transaction_anchor_content(transaction_id)
    journal = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "transaction_anchor_basename": f".ocr-v1-v2-{transaction_id}.anchor",
        "transaction_anchor_dev": None,
        "transaction_anchor_ino": None,
        "transaction_anchor_sha256": hashlib.sha256(anchor_content).hexdigest(),
        "kind": kind,
        "phase": "intent",
        "journal_self_dev": None,
        "journal_self_ino": None,
        "journal_self_sha256": None,
        **lock_identity,
        "source_basename": source_basename,
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "source_size": len(original),
        "backup_basename": backup_basename,
        "backup_dev": None,
        "backup_ino": None,
        "journal_previous_sha256": None,
        "journal_previous_dev": None,
        "journal_previous_ino": None,
        "target_sha256": _json_fingerprint(target),
        "target_dev": None,
        "target_ino": None,
        "swap_path_basename": None,
        "swap_replacement_dev": None,
        "swap_replacement_ino": None,
        "swap_expected_dev": None,
        "swap_expected_ino": None,
        "swap_displaced_dev": None,
        "swap_displaced_ino": None,
        "artifact_sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
        "artifact_dev": None,
        "artifact_ino": None,
        "publication_sha256": (_json_fingerprint(publication) if publication is not None else None),
        "manifest_candidate_basename": (
            f".ocr-v1-v2-{transaction_id}.manifest" if publication is not None else None
        ),
        "manifest_candidate_dev": None,
        "manifest_candidate_ino": None,
        "manifest_dev": None,
        "manifest_ino": None,
        "sidecar_transaction_tokens": {
            label: secrets.token_hex(32) for label in _MIGRATION_SIDECAR_LABELS
        },
    }
    return _validate_migration_journal(journal, allow_unbound_self=True)


def _prepare_migration_intent(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
    *,
    publication: dict[str, Any] | None,
) -> dict[str, Any]:
    code = _migration_error_code(journal["kind"])
    if journal["phase"] != "intent":
        raise _LegacyMigrationError(code)
    _validate_migration_lock(paths, journal, publication_lock)
    if publication is not None:
        _require_missing(paths.publication, error_code=code)

    anchor_path, anchor = _create_transaction_anchor(
        paths,
        str(journal["transaction_id"]),
        error_code=code,
        candidate_owner=_migration_candidate_owner(
            paths,
            journal,
            "anchor",
            publication_lock,
        ),
    )
    if (
        anchor_path.name != journal["transaction_anchor_basename"]
        or hashlib.sha256(anchor.content).hexdigest() != journal["transaction_anchor_sha256"]
    ):
        raise _LegacyMigrationError(code)
    _migration_checkpoint(f"{journal['kind']}_intent_anchor_published")

    manifest_candidate: tuple[Path, _RegularFileSnapshot] | None = None
    if publication is not None:
        if _json_fingerprint(publication) != journal["publication_sha256"]:
            raise _LegacyMigrationError(code)
        manifest_candidate = _create_manifest_candidate(
            paths,
            str(journal["transaction_id"]),
            publication,
            candidate_owner=_migration_candidate_owner(
                paths,
                journal,
                "manifest",
                publication_lock,
            ),
        )
        if (
            manifest_candidate[0].name != journal["manifest_candidate_basename"]
            or hashlib.sha256(manifest_candidate[1].content).hexdigest()
            != journal["publication_sha256"]
        ):
            raise _LegacyMigrationError(code)
        _migration_checkpoint(f"{journal['kind']}_intent_manifest_published")
    elif journal["publication_sha256"] is not None:
        raise _LegacyMigrationError(code)

    updates: dict[str, object] = {
        **_snapshot_identity_updates("transaction_anchor", anchor),
    }
    if manifest_candidate is not None:
        updates.update(_snapshot_identity_updates("manifest_candidate", manifest_candidate[1]))
    _migration_checkpoint(f"{journal['kind']}_intent_prepared_cas")
    prepared = _advance_migration_journal(
        paths,
        journal,
        "prepared",
        publication_lock,
        **updates,
    )
    _migration_checkpoint(f"{journal['kind']}_journal_prepared")
    return prepared


def _require_missing(path: Path, *, error_code: str) -> None:
    try:
        _read_regular_bytes(path)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    raise _LegacyMigrationError(error_code)


def _migration_original_bytes(
    paths: WorkflowPaths,
    journal: dict[str, Any],
) -> tuple[Path, Path, bytes]:
    code = _migration_error_code(journal["kind"])
    source = paths.final if journal["kind"] == "completed" else paths.state
    backup = paths.root / str(journal["backup_basename"])
    original_path = backup if _phase_at_least(journal, "backup_published") else source
    expected_identity = (
        _identity_pair(journal, "backup") if _phase_at_least(journal, "backup_published") else None
    )
    try:
        original = _read_regular_snapshot(original_path)
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if (original.identity != expected_identity if expected_identity is not None else False) or (
        len(original.content) != journal["source_size"]
        or hashlib.sha256(original.content).hexdigest() != journal["source_sha256"]
    ):
        raise _LegacyMigrationError(code)
    return source, backup, original.content


def _require_bound_snapshot(
    path: Path,
    expected: bytes,
    expected_identity: tuple[int, int] | None,
    *,
    error_code: str,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> _RegularFileSnapshot:
    try:
        current = _read_regular_snapshot(path, allowed_link_counts=allowed_link_counts)
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if current.content != expected or (
        expected_identity is not None and current.identity != expected_identity
    ):
        raise _LegacyMigrationError(error_code)
    return current


def _migration_sidecar_path(paths: WorkflowPaths, journal: dict[str, Any], label: str) -> Path:
    return paths.root / f".ocr-v1-v2-{journal['transaction_id']}.{label}"


def _restore_retained_path(retained: Path, active: Path) -> None:
    try:
        _rename_no_replace(retained, active)
    except OSError:
        return
    try:
        _fsync_directory(active.parent)
    except OSError:
        pass


def _retire_bound_path(
    active: Path,
    retained: Path,
    expected: _RegularFileSnapshot,
    *,
    error_code: str,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> _RegularFileSnapshot:
    try:
        observed_identity = _path_identity(active)
    except FileNotFoundError:
        try:
            moved = _read_regular_snapshot(
                retained,
                allowed_link_counts=allowed_link_counts,
            )
        except (FileNotFoundError, OSError, ValueError):
            raise _LegacyMigrationError(error_code) from None
        if moved.identity != expected.identity or moved.content != expected.content:
            _restore_retained_path(retained, active)
            raise _LegacyMigrationError(error_code) from None
        try:
            _fsync_directory(active.parent)
        except OSError:
            raise _LegacyMigrationError(error_code) from None
        return moved
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    if observed_identity != expected.identity:
        raise _LegacyMigrationError(error_code)
    try:
        _rename_no_replace(active, retained)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    try:
        _fsync_directory(active.parent)
    except OSError:
        raise _LegacyMigrationError(error_code) from None
    try:
        moved = _read_regular_snapshot(retained, allowed_link_counts=allowed_link_counts)
    except (FileNotFoundError, OSError, ValueError):
        _restore_retained_path(retained, active)
        raise _LegacyMigrationError(error_code) from None
    if moved.identity != expected.identity or moved.content != expected.content:
        _restore_retained_path(retained, active)
        raise _LegacyMigrationError(error_code)
    return moved


def _bound_active_or_retained(
    active: Path,
    retained: Path,
    expected: bytes,
    expected_identity: tuple[int, int] | None,
    *,
    error_code: str,
    allow_retained: bool,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> tuple[Path, _RegularFileSnapshot]:
    try:
        active_snapshot = _read_regular_snapshot(
            active,
            allowed_link_counts=allowed_link_counts,
        )
    except FileNotFoundError:
        active_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    try:
        retained_snapshot = _read_regular_snapshot(
            retained,
            allowed_link_counts=allowed_link_counts,
        )
    except FileNotFoundError:
        retained_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if active_snapshot is not None and retained_snapshot is not None:
        raise _LegacyMigrationError(error_code)
    selected_path = active if active_snapshot is not None else retained
    selected = active_snapshot if active_snapshot is not None else retained_snapshot
    if selected is None or (selected_path == retained and not allow_retained):
        raise _LegacyMigrationError(error_code)
    if selected.content != expected or (
        expected_identity is not None and selected.identity != expected_identity
    ):
        raise _LegacyMigrationError(error_code)
    return selected_path, selected


def _snapshot_active_or_retained_by_identity(
    active: Path,
    retained: Path,
    expected_identity: tuple[int, int],
    *,
    error_code: str,
    allow_retained: bool,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> tuple[Path, _RegularFileSnapshot]:
    try:
        active_snapshot = _read_regular_snapshot(
            active,
            allowed_link_counts=allowed_link_counts,
        )
    except FileNotFoundError:
        active_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    try:
        retained_snapshot = _read_regular_snapshot(
            retained,
            allowed_link_counts=allowed_link_counts,
        )
    except FileNotFoundError:
        retained_snapshot = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if active_snapshot is not None and retained_snapshot is not None:
        raise _LegacyMigrationError(error_code)
    selected_path = active if active_snapshot is not None else retained
    selected = active_snapshot if active_snapshot is not None else retained_snapshot
    if selected is None or (selected_path == retained and not allow_retained):
        raise _LegacyMigrationError(error_code)
    if selected.identity != expected_identity:
        raise _LegacyMigrationError(error_code)
    return selected_path, selected


def _ensure_backup(
    backup: Path,
    original: bytes,
    *,
    source_is_target: bool,
    error_code: str,
    candidate_owner: _MigrationCandidateOwner,
) -> _RegularFileSnapshot:
    if not source_is_target:
        return _write_legacy_backup(
            backup,
            original,
            error_code=error_code,
            candidate_owner=candidate_owner,
        )
    try:
        existing = _read_regular_snapshot(backup)
    except FileNotFoundError:
        if source_is_target:
            raise _LegacyMigrationError(error_code) from None
        return _write_legacy_backup(
            backup,
            original,
            error_code=error_code,
            candidate_owner=candidate_owner,
        )
    except (OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if existing.content != original:
        raise _LegacyMigrationError(error_code)
    return existing


def _snapshot_identity_updates(prefix: str, snapshot: _RegularFileSnapshot) -> dict[str, int]:
    return {
        f"{prefix}_dev": snapshot.device,
        f"{prefix}_ino": snapshot.inode,
    }


def _source_swap_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    basename = journal.get("swap_path_basename")
    if basename != f".ocr-v1-v2-{journal['transaction_id']}.swap":
        raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
    return paths.root / str(basename)


def _prepare_source_swap(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
    *,
    source: Path,
    original: bytes,
    target: bytes,
) -> dict[str, Any]:
    if _phase_at_least(journal, "source_swap_prepared"):
        return journal
    code = _migration_error_code(journal["kind"])
    expected = _require_bound_snapshot(source, original, None, error_code=code)
    swap_path = paths.root / f".ocr-v1-v2-{journal['transaction_id']}.swap"
    replacement = _write_regular_no_replace(
        swap_path,
        target,
        error_code=code,
        candidate_owner=_migration_candidate_owner(
            paths,
            journal,
            "swap",
            publication_lock,
        ),
    )
    return _advance_migration_journal(
        paths,
        journal,
        "source_swap_prepared",
        publication_lock,
        swap_path_basename=swap_path.name,
        **_snapshot_identity_updates("swap_replacement", replacement),
        **_snapshot_identity_updates("swap_expected", expected),
    )


def _read_stable_source_swap_snapshot(
    path: Path,
    *,
    error_code: str,
) -> _RegularFileSnapshot:
    """Read a source/swap path while binding the pathname to the opened inode."""

    try:
        before = path.lstat()
        snapshot = _read_regular_snapshot(path)
        after = path.lstat()
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(error_code) from None
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino) != snapshot.identity
        or (after.st_dev, after.st_ino) != snapshot.identity
        or after.st_mode != before.st_mode
        or after.st_uid != before.st_uid
    ):
        raise _LegacyMigrationError(error_code)
    return snapshot


def _require_stable_source_swap_snapshot(
    path: Path,
    *,
    content: bytes,
    identity: tuple[int, int],
    error_code: str,
) -> _RegularFileSnapshot:
    snapshot = _read_stable_source_swap_snapshot(path, error_code=error_code)
    if snapshot.content != content or snapshot.identity != identity:
        raise _LegacyMigrationError(error_code)
    return snapshot


def _resolve_source_swap(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    *,
    source: Path,
    original: bytes,
    target: bytes,
    before_swap_checkpoint: str,
    after_swap_checkpoint: str,
) -> tuple[_RegularFileSnapshot, _RegularFileSnapshot]:
    code = _migration_error_code(journal["kind"])
    replacement_identity = _identity_pair(journal, "swap_replacement")
    expected_identity = _identity_pair(journal, "swap_expected")
    if replacement_identity is None or expected_identity is None:
        raise _LegacyMigrationError(code)
    swap_path = _source_swap_path(paths, journal)
    target_phase = "final_published" if journal["kind"] == "completed" else "state_published"
    if _phase_at_least(journal, target_phase):
        target_identity = _identity_pair(journal, "target")
        displaced_identity = _identity_pair(journal, "swap_displaced")
        if target_identity is None or displaced_identity is None:
            raise _LegacyMigrationError(code)
        committed = _require_stable_source_swap_snapshot(
            source,
            content=target,
            identity=target_identity,
            error_code=code,
        )
        displaced_path, displaced = _bound_active_or_retained(
            swap_path,
            _migration_sidecar_path(paths, journal, "swap-retired"),
            original,
            displaced_identity,
            error_code=code,
            allow_retained=_phase_at_least(journal, "commit_ready"),
        )
        displaced = _require_stable_source_swap_snapshot(
            displaced_path,
            content=original,
            identity=displaced_identity,
            error_code=code,
        )
        return committed, displaced

    current_source = _read_stable_source_swap_snapshot(source, error_code=code)
    current_swap = _read_stable_source_swap_snapshot(swap_path, error_code=code)

    if (
        current_source.identity == expected_identity
        and current_source.content == original
        and current_swap.identity == replacement_identity
        and current_swap.content == target
    ):
        _migration_checkpoint(before_swap_checkpoint)
        try:
            _rename_swap(swap_path, source)
        except OSError:
            raise _LegacyMigrationError(code) from None
        try:
            _fsync_directory(paths.root)
        except OSError:
            raise _LegacyMigrationError(code) from None
        current_source = _read_stable_source_swap_snapshot(source, error_code=code)
        current_swap = _read_stable_source_swap_snapshot(swap_path, error_code=code)

    source_is_target = (
        current_source.identity == replacement_identity and current_source.content == target
    )
    displaced_is_original = (
        current_swap.identity == expected_identity and current_swap.content == original
    )
    if source_is_target and displaced_is_original:
        _migration_checkpoint(after_swap_checkpoint)
        committed = _require_stable_source_swap_snapshot(
            source,
            content=target,
            identity=replacement_identity,
            error_code=code,
        )
        displaced = _require_stable_source_swap_snapshot(
            swap_path,
            content=original,
            identity=expected_identity,
            error_code=code,
        )
        return committed, displaced

    if source_is_target:
        _require_stable_source_swap_snapshot(
            source,
            content=target,
            identity=replacement_identity,
            error_code=code,
        )
        _require_stable_source_swap_snapshot(
            swap_path,
            content=current_swap.content,
            identity=current_swap.identity,
            error_code=code,
        )
        try:
            _rename_swap(swap_path, source)
        except OSError:
            raise _LegacyMigrationError(code) from None
        try:
            _fsync_directory(paths.root)
        except OSError:
            raise _LegacyMigrationError(code) from None
        raise _LegacyMigrationError(code)

    raise _LegacyMigrationError(code)


def _manifest_candidate_path(paths: WorkflowPaths, journal: dict[str, Any]) -> Path:
    basename = journal.get("manifest_candidate_basename")
    if not isinstance(basename, str) or Path(basename).name != basename:
        raise _LegacyMigrationError(_migration_error_code(journal["kind"]))
    return paths.root / basename


def _read_manifest_candidate(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication_bytes: bytes,
) -> _RegularFileSnapshot | None:
    code = _migration_error_code(journal["kind"])
    path = _manifest_candidate_path(paths, journal)
    retained = _migration_sidecar_path(paths, journal, "manifest-retired")
    try:
        active = _read_regular_snapshot(path, allowed_link_counts=(1, 2))
    except FileNotFoundError:
        active = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    try:
        retired = _read_regular_snapshot(retained, allowed_link_counts=(2,))
    except FileNotFoundError:
        retired = None
    except (OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if active is not None and retired is not None:
        raise _LegacyMigrationError(code)
    snapshot = active if active is not None else retired
    if snapshot is None:
        return None
    if retired is not None and not _phase_at_least(journal, "manifest_linked"):
        raise _LegacyMigrationError(code)
    if snapshot.content != publication_bytes or snapshot.identity != _identity_pair(
        journal, "manifest_candidate"
    ):
        raise _LegacyMigrationError(code)
    return snapshot


def _validate_completed_bindings(
    paths: WorkflowPaths,
    source_path: Path,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
    *,
    source: Path,
    backup: Path,
    original: bytes,
    migrated: dict[str, Any],
    artifact_path: Path | None,
    content: bytes | None,
    publication: dict[str, Any] | None,
    include_manifest: bool,
) -> _RegularFileSnapshot | None:
    code = "ocr_evidence_invalid"
    _validate_migration_lock(paths, journal, publication_lock)
    _bound_active_or_retained(
        _migration_journal_path(paths),
        _migration_sidecar_path(paths, journal, "journal-retired"),
        _canonical_json_bytes(journal),
        _identity_pair(journal, "journal_self"),
        error_code=code,
        allow_retained=_phase_at_least(journal, "commit_ready"),
    )
    _require_bound_snapshot(
        backup,
        original,
        _identity_pair(journal, "backup"),
        error_code=code,
    )
    _require_bound_snapshot(
        source,
        _canonical_json_bytes(migrated),
        _identity_pair(journal, "target"),
        error_code=code,
    )
    if not _completed_ocr_final_is_valid(migrated, source_path):
        raise _LegacyMigrationError(code)
    if content is not None and artifact_path is not None:
        _require_bound_snapshot(
            artifact_path,
            content,
            _identity_pair(journal, "artifact"),
            error_code=code,
        )
    elif content is not None or artifact_path is not None:
        raise _LegacyMigrationError(code)
    if publication is None:
        _require_missing(paths.publication, error_code=code)
        return None
    if not include_manifest:
        return None
    try:
        manifest = _read_regular_snapshot(paths.publication, allowed_link_counts=(2,))
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if manifest.content != _canonical_json_bytes(
        publication
    ) or manifest.identity != _identity_pair(journal, "manifest"):
        raise _LegacyMigrationError(code)
    return manifest


def _publish_migration_manifest(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication: dict[str, Any],
    publication_lock: _PublicationLock,
) -> tuple[dict[str, Any], _RegularFileSnapshot]:
    code = "ocr_evidence_invalid"
    publication_bytes = _canonical_json_bytes(publication)
    candidate_path = _manifest_candidate_path(paths, journal)
    candidate = _read_manifest_candidate(paths, journal, publication_bytes)
    manifest_identity = _identity_pair(journal, "manifest")
    linked = _phase_at_least(journal, "manifest_linked")

    if linked:
        if manifest_identity is None:
            raise _LegacyMigrationError(code)
        try:
            manifest = _read_regular_snapshot(
                paths.publication,
                allowed_link_counts=(1, 2) if candidate is not None else (1,),
            )
        except (FileNotFoundError, OSError, ValueError):
            raise _LegacyMigrationError(code) from None
        if manifest.content != publication_bytes or manifest.identity != manifest_identity:
            raise _LegacyMigrationError(code)
        if candidate is not None and manifest.identity != candidate.identity:
            raise _LegacyMigrationError(code)
    else:
        if candidate is None:
            raise _LegacyMigrationError(code)
        try:
            os.link(candidate_path, paths.publication, follow_symlinks=False)
        except FileExistsError:
            try:
                manifest = _read_regular_snapshot(
                    paths.publication,
                    allowed_link_counts=(2,),
                )
            except (FileNotFoundError, OSError, ValueError):
                raise _LegacyMigrationError(code) from None
            if manifest.identity != candidate.identity or manifest.content != publication_bytes:
                raise _LegacyMigrationError(code) from None
        except OSError:
            raise _LegacyMigrationError(code) from None
        else:
            _fsync_directory(paths.root)
            try:
                manifest = _read_regular_snapshot(
                    paths.publication,
                    allowed_link_counts=(2,),
                )
            except (OSError, ValueError):
                raise _LegacyMigrationError(code) from None
            if manifest.identity != candidate.identity or manifest.content != publication_bytes:
                raise _LegacyMigrationError(code)
        journal = _advance_migration_journal(
            paths,
            journal,
            "manifest_linked",
            publication_lock,
            **_snapshot_identity_updates("manifest", manifest),
        )
        _migration_checkpoint("completed_manifest_linked")

    candidate = _read_manifest_candidate(paths, journal, publication_bytes)
    if candidate is not None:
        _retire_bound_path(
            candidate_path,
            _migration_sidecar_path(paths, journal, "manifest-retired"),
            candidate,
            error_code=code,
            allowed_link_counts=(2,),
        )
    try:
        manifest = _read_regular_snapshot(paths.publication, allowed_link_counts=(2,))
    except (FileNotFoundError, OSError, ValueError):
        raise _LegacyMigrationError(code) from None
    if manifest.content != publication_bytes or manifest.identity != _identity_pair(
        journal, "manifest"
    ):
        raise _LegacyMigrationError(code)
    if not _phase_at_least(journal, "manifest_published"):
        journal = _advance_migration_journal(
            paths,
            journal,
            "manifest_published",
            publication_lock,
        )
        _migration_checkpoint("completed_manifest_published")
    return journal, manifest


def _remove_owned_migration_manifest(
    paths: WorkflowPaths,
    journal: dict[str, Any],
    publication: dict[str, Any],
) -> None:
    identity = _identity_pair(journal, "manifest")
    if identity is None:
        return
    try:
        manifest = _read_regular_snapshot(paths.publication, allowed_link_counts=(2,))
    except (FileNotFoundError, OSError, ValueError):
        return
    if manifest.identity != identity or manifest.content != _canonical_json_bytes(publication):
        return
    _retire_bound_path(
        paths.publication,
        _migration_sidecar_path(paths, journal, "manifest-aborted"),
        manifest,
        error_code=_migration_error_code(journal["kind"]),
        allowed_link_counts=(2,),
    )


def _resume_completed_migration(
    paths: WorkflowPaths,
    source_path: Path,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> None:
    code = "ocr_evidence_invalid"
    _validate_migration_lock(paths, journal, publication_lock)
    source, backup, original = _migration_original_bytes(paths, journal)
    legacy = _decode_legacy_json(original, error_code=code)
    migrated, content, publication = _completed_migration_material(legacy, source_path, paths)
    if _json_fingerprint(migrated) != journal["target_sha256"]:
        raise _LegacyMigrationError(code)
    expected_artifact_sha256 = hashlib.sha256(content).hexdigest() if content is not None else None
    expected_publication_sha256 = (
        _json_fingerprint(publication) if publication is not None else None
    )
    if (
        expected_artifact_sha256 != journal["artifact_sha256"]
        or expected_publication_sha256 != journal["publication_sha256"]
    ):
        raise _LegacyMigrationError(code)
    if journal["phase"] == "intent":
        journal = _prepare_migration_intent(
            paths,
            journal,
            publication_lock,
            publication=publication,
        )
    target_bytes = _canonical_json_bytes(migrated)
    if _phase_at_least(journal, "backup_published"):
        _require_bound_snapshot(
            backup,
            original,
            _identity_pair(journal, "backup"),
            error_code=code,
        )
    else:
        _require_bound_snapshot(source, original, None, error_code=code)
        _require_missing(paths.publication, error_code=code)
        backup_snapshot = _ensure_backup(
            backup,
            original,
            source_is_target=False,
            error_code=code,
            candidate_owner=_migration_candidate_owner(
                paths,
                journal,
                "backup",
                publication_lock,
            ),
        )
        journal = _advance_migration_journal(
            paths,
            journal,
            "backup_published",
            publication_lock,
            **_snapshot_identity_updates("backup", backup_snapshot),
        )
        _migration_checkpoint("completed_backup_published")
    _validate_migration_lock(paths, journal, publication_lock)

    artifact_path: Path | None = None
    if content is not None and publication is not None:
        artifact_path = paths.root / publication["artifact_basename"]
        if artifact_path.name != publication["artifact_basename"]:
            raise _LegacyMigrationError(code)
        if _phase_at_least(journal, "artifact_published"):
            _require_bound_snapshot(
                artifact_path,
                content,
                _identity_pair(journal, "artifact"),
                error_code=code,
            )
        else:
            artifact_path = _publish_immutable_artifact(
                paths.root,
                content,
                str(journal["artifact_sha256"]),
                candidate_owner=_migration_candidate_owner(
                    paths,
                    journal,
                    "artifact",
                    publication_lock,
                ),
            )
            artifact_snapshot = _require_bound_snapshot(
                artifact_path,
                content,
                None,
                error_code=code,
            )
            journal = _advance_migration_journal(
                paths,
                journal,
                "artifact_published",
                publication_lock,
                **_snapshot_identity_updates("artifact", artifact_snapshot),
            )
            _migration_checkpoint("completed_artifact_published")
    else:
        _require_missing(paths.publication, error_code=code)
    _validate_migration_lock(paths, journal, publication_lock)

    if not _phase_at_least(journal, "source_swap_prepared"):
        _require_missing(paths.publication, error_code=code)
        journal = _prepare_source_swap(
            paths,
            journal,
            publication_lock,
            source=source,
            original=original,
            target=target_bytes,
        )
    source_snapshot, displaced_snapshot = _resolve_source_swap(
        paths,
        journal,
        source=source,
        original=original,
        target=target_bytes,
        before_swap_checkpoint="completed_source_cas_ready",
        after_swap_checkpoint="completed_source_swapped",
    )
    if not _phase_at_least(journal, "final_published"):
        journal = _advance_migration_journal(
            paths,
            journal,
            "final_published",
            publication_lock,
            **_snapshot_identity_updates("target", source_snapshot),
            **_snapshot_identity_updates("swap_displaced", displaced_snapshot),
        )
        _migration_checkpoint("completed_final_published")

    _validate_completed_bindings(
        paths,
        source_path,
        journal,
        publication_lock,
        source=source,
        backup=backup,
        original=original,
        migrated=migrated,
        artifact_path=artifact_path,
        content=content,
        publication=publication,
        include_manifest=False,
    )

    if publication is not None:
        journal, _manifest = _publish_migration_manifest(
            paths,
            journal,
            publication,
            publication_lock,
        )
        try:
            _validate_completed_bindings(
                paths,
                source_path,
                journal,
                publication_lock,
                source=source,
                backup=backup,
                original=original,
                migrated=migrated,
                artifact_path=artifact_path,
                content=content,
                publication=publication,
                include_manifest=True,
            )
            published, error, published_path = _publication_manifest_status(
                migrated,
                paths,
                publication,
            )
            if not published or error is not None or published_path != artifact_path:
                raise _LegacyMigrationError(code)
        except _LegacyMigrationError:
            _remove_owned_migration_manifest(paths, journal, publication)
            raise
    else:
        _require_missing(paths.publication, error_code=code)
    _validate_completed_bindings(
        paths,
        source_path,
        journal,
        publication_lock,
        source=source,
        backup=backup,
        original=original,
        migrated=migrated,
        artifact_path=artifact_path,
        content=content,
        publication=publication,
        include_manifest=publication is not None,
    )
    if not _phase_at_least(journal, "commit_ready"):
        journal = _advance_migration_journal(
            paths,
            journal,
            "commit_ready",
            publication_lock,
        )
    _ensure_commit_witness(paths, journal, publication_lock)
    _migration_checkpoint("completed_commit_ready")
    _validate_completed_bindings(
        paths,
        source_path,
        journal,
        publication_lock,
        source=source,
        backup=backup,
        original=original,
        migrated=migrated,
        artifact_path=artifact_path,
        content=content,
        publication=publication,
        include_manifest=publication is not None,
    )

    def validate_commit_bindings() -> None:
        try:
            _validate_completed_bindings(
                paths,
                source_path,
                journal,
                publication_lock,
                source=source,
                backup=backup,
                original=original,
                migrated=migrated,
                artifact_path=artifact_path,
                content=content,
                publication=publication,
                include_manifest=publication is not None,
            )
        except _LegacyMigrationError:
            if publication is not None:
                _remove_owned_migration_manifest(paths, journal, publication)
            raise

    _remove_migration_journal(
        paths,
        journal,
        publication_lock,
        original=original,
        publication=publication,
        validate_bindings=validate_commit_bindings,
    )


def _resume_running_migration(
    paths: WorkflowPaths,
    source_path: Path,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> None:
    code = "ocr_state_invalid"
    _validate_migration_lock(paths, journal, publication_lock)
    source, backup, original = _migration_original_bytes(paths, journal)
    legacy = _decode_legacy_json(original, error_code=code)
    target = _legacy_running_target(legacy, source_path)
    if _json_fingerprint(target) != journal["target_sha256"]:
        raise _LegacyMigrationError(code)
    if journal["phase"] == "intent":
        journal = _prepare_migration_intent(
            paths,
            journal,
            publication_lock,
            publication=None,
        )
    target_bytes = _canonical_json_bytes(target)
    if _phase_at_least(journal, "backup_published"):
        _require_bound_snapshot(
            backup,
            original,
            _identity_pair(journal, "backup"),
            error_code=code,
        )
    else:
        _require_bound_snapshot(source, original, None, error_code=code)
        backup_snapshot = _ensure_backup(
            backup,
            original,
            source_is_target=False,
            error_code=code,
            candidate_owner=_migration_candidate_owner(
                paths,
                journal,
                "backup",
                publication_lock,
            ),
        )
        journal = _advance_migration_journal(
            paths,
            journal,
            "backup_published",
            publication_lock,
            **_snapshot_identity_updates("backup", backup_snapshot),
        )
        _migration_checkpoint("running_backup_published")
    _validate_migration_lock(paths, journal, publication_lock)

    if not _phase_at_least(journal, "source_swap_prepared"):
        journal = _prepare_source_swap(
            paths,
            journal,
            publication_lock,
            source=source,
            original=original,
            target=target_bytes,
        )
    source_snapshot, displaced_snapshot = _resolve_source_swap(
        paths,
        journal,
        source=source,
        original=original,
        target=target_bytes,
        before_swap_checkpoint="running_source_cas_ready",
        after_swap_checkpoint="running_source_swapped",
    )
    if not _phase_at_least(journal, "state_published"):
        journal = _advance_migration_journal(
            paths,
            journal,
            "state_published",
            publication_lock,
            **_snapshot_identity_updates("target", source_snapshot),
            **_snapshot_identity_updates("swap_displaced", displaced_snapshot),
        )
        _migration_checkpoint("running_state_published")
    _validate_migration_lock(paths, journal, publication_lock)
    _read_self_bound_migration_journal(
        _migration_journal_path(paths),
        expected=journal,
        error_code=code,
    )
    _require_bound_snapshot(
        backup,
        original,
        _identity_pair(journal, "backup"),
        error_code=code,
    )
    _require_bound_snapshot(
        source,
        target_bytes,
        _identity_pair(journal, "target"),
        error_code=code,
    )
    if _interrupted_run_config(source, source_path) != target["run_config"]:
        raise _LegacyMigrationError(code)
    if not _phase_at_least(journal, "commit_ready"):
        journal = _advance_migration_journal(
            paths,
            journal,
            "commit_ready",
            publication_lock,
        )
    _ensure_commit_witness(paths, journal, publication_lock)
    _migration_checkpoint("running_commit_ready")
    _validate_migration_lock(paths, journal, publication_lock)
    _require_bound_snapshot(
        backup,
        original,
        _identity_pair(journal, "backup"),
        error_code=code,
    )
    _require_bound_snapshot(
        source,
        target_bytes,
        _identity_pair(journal, "target"),
        error_code=code,
    )
    if _interrupted_run_config(source, source_path) != target["run_config"]:
        raise _LegacyMigrationError(code)

    def validate_running_commit_bindings() -> None:
        _validate_migration_lock(paths, journal, publication_lock)
        _require_bound_snapshot(
            backup,
            original,
            _identity_pair(journal, "backup"),
            error_code=code,
        )
        _require_bound_snapshot(
            source,
            target_bytes,
            _identity_pair(journal, "target"),
            error_code=code,
        )
        if _interrupted_run_config(source, source_path) != target["run_config"]:
            raise _LegacyMigrationError(code)

    _remove_migration_journal(
        paths,
        journal,
        publication_lock,
        original=original,
        publication=None,
        validate_bindings=validate_running_commit_bindings,
    )


def _resume_migration_journal(
    paths: WorkflowPaths,
    source_path: Path,
    journal: dict[str, Any],
    publication_lock: _PublicationLock,
) -> None:
    if journal["kind"] == "completed":
        _resume_completed_migration(paths, source_path, journal, publication_lock)
    else:
        _resume_running_migration(paths, source_path, journal, publication_lock)


def _prepare_completed_migration(
    paths: WorkflowPaths,
    source_path: Path,
    publication_lock: _PublicationLock,
) -> bool:
    try:
        original = _read_regular_bytes(paths.final)
    except FileNotFoundError:
        return False
    value = _decode_legacy_json(original, error_code="ocr_evidence_invalid")
    if value.get("schema_version") != 1:
        return True
    migrated, content, publication = _completed_migration_material(value, source_path, paths)
    _require_missing(paths.publication, error_code="ocr_evidence_invalid")
    _require_active_publication_lock(publication_lock)
    transaction_id = secrets.token_hex(32)
    journal = _build_migration_journal(
        publication_lock=publication_lock,
        transaction_id=transaction_id,
        kind="completed",
        source_basename=paths.final.name,
        backup_basename="batch-final.v1.backup.json",
        original=original,
        target=migrated,
        content=content,
        publication=publication,
    )
    journal = _write_new_migration_journal(paths, journal, publication_lock)
    _migration_checkpoint("completed_journal_intent")
    _resume_completed_migration(paths, source_path, journal, publication_lock)
    return True


def _prepare_running_migration(
    paths: WorkflowPaths,
    source_path: Path,
    publication_lock: _PublicationLock,
) -> None:
    try:
        original = _read_regular_bytes(paths.state)
    except FileNotFoundError:
        return
    value = _decode_legacy_json(original, error_code="ocr_state_invalid")
    if value.get("schema_version") != 1 or value.get("status") != BatchStatus.RUNNING.value:
        return
    target = _legacy_running_target(value, source_path)
    transaction_id = secrets.token_hex(32)
    journal = _build_migration_journal(
        publication_lock=publication_lock,
        transaction_id=transaction_id,
        kind="running",
        source_basename=paths.state.name,
        backup_basename="batch-state.v1.backup.json",
        original=original,
        target=target,
    )
    journal = _write_new_migration_journal(paths, journal, publication_lock)
    _migration_checkpoint("running_journal_intent")
    _resume_running_migration(paths, source_path, journal, publication_lock)


def _legacy_migration_may_be_needed(paths: WorkflowPaths) -> bool:
    if _pending_commit_witness_path(paths) is not None:
        return True
    try:
        _migration_journal_path(paths).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return True
    else:
        return True
    try:
        final_value = json.loads(_read_regular_snapshot(paths.final).content.decode("utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return True
    else:
        return not isinstance(final_value, dict) or final_value.get("schema_version") == 1
    try:
        state_value = json.loads(_read_regular_snapshot(paths.state).content.decode("utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return True
    return (
        not isinstance(state_value, dict)
        or state_value.get("schema_version") == 1
        and state_value.get("status") == BatchStatus.RUNNING.value
    )


def _migrate_legacy_artifacts(paths: WorkflowPaths, source_path: Path) -> None:
    if not _legacy_migration_may_be_needed(paths):
        return
    try:
        with _publication_transaction_lock(paths) as publication_lock:
            _require_active_publication_lock(publication_lock)
            _validate_finalized_migration_receipts(paths)
            _recover_pending_commit(paths, publication_lock)
            journal = _read_migration_journal(paths, publication_lock)
            if journal is not None:
                code = _migration_error_code(journal["kind"])
                try:
                    _resume_migration_journal(
                        paths,
                        source_path,
                        journal,
                        publication_lock,
                    )
                except _LegacyMigrationError:
                    raise
                except (OSError, ValueError):
                    raise _LegacyMigrationError(code) from None
            try:
                final_exists = _prepare_completed_migration(
                    paths,
                    source_path,
                    publication_lock,
                )
            except _LegacyMigrationError:
                raise
            except (OSError, ValueError):
                raise _LegacyMigrationError("ocr_evidence_invalid") from None
            if not final_exists:
                try:
                    _prepare_running_migration(
                        paths,
                        source_path,
                        publication_lock,
                    )
                except _LegacyMigrationError:
                    raise
                except (OSError, ValueError):
                    raise _LegacyMigrationError("ocr_state_invalid") from None
            _validate_finalized_migration_receipts(paths)
            _require_active_publication_lock(publication_lock)
    except _LegacyMigrationError:
        raise
    except (OSError, ValueError):
        raise _LegacyMigrationError("ocr_state_invalid") from None


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
        self._worker_claim: _OcrWorkerClaim | None = None
        self._status = WorkflowStatus.IDLE
        self._error: str | None = None
        self._lock = threading.RLock()
        self._migration_error: str | None = None
        try:
            _validate_finalized_migration_receipts(self.paths)
            _migrate_legacy_artifacts(self.paths, self.source_path)
        except _LegacyMigrationError as exc:
            self._migration_error = exc.code
        except (OSError, ValueError):
            self._migration_error = "ocr_state_invalid"
        if self._migration_error is not None:
            return
        self._resume_interrupted_state()

    def start(self, *, dpi: int = 300, languages: tuple[str, ...] = ("zh-Hans", "en-US")) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("OCR 任务正在运行")
            claim = _try_claim_ocr_worker(self.paths)
            if claim is None:
                raise ValueError("OCR 任务正在运行")
            self._worker_claim = claim
            try:
                self._cancel.clear()
                self._error = None
                self._status = WorkflowStatus.RUNNING
                self._launch_thread(dpi=dpi, languages=languages)
            except Exception:
                self._status = WorkflowStatus.IDLE
                self._release_worker_claim()
                raise

    def _resume_interrupted_state(self) -> None:
        try:
            config = _interrupted_run_config(self.paths.state, self.source_path)
        except (FileNotFoundError, OSError, ValueError):
            return
        if config is None:
            return
        try:
            claim = _try_claim_ocr_worker(self.paths)
        except (OSError, ValueError):
            self._migration_error = "ocr_state_invalid"
            return
        if claim is None:
            return
        try:
            current_config = _interrupted_run_config(self.paths.state, self.source_path)
            claim.validate()
            if current_config != config:
                return
            self._worker_claim = claim
            self._status = WorkflowStatus.RUNNING
            try:
                self._launch_thread(
                    dpi=config["dpi"],
                    languages=tuple(config["languages"]),
                    pages=tuple(config["pages"]),
                    sample_rate=config["sample_rate"],
                )
            except Exception:
                self._worker_claim = None
                self._status = WorkflowStatus.IDLE
                self._migration_error = "ocr_state_invalid"
                return
            claim = None
        except (FileNotFoundError, OSError, ValueError):
            self._migration_error = "ocr_state_invalid"
        finally:
            if claim is not None:
                claim.release()

    def _launch_thread(
        self,
        *,
        dpi: int,
        languages: tuple[str, ...],
        pages: tuple[int, ...] | None = None,
        sample_rate: float = 0.05,
    ) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(dpi, languages, pages, sample_rate),
            daemon=True,
            name="pdf2md-ocr",
        )
        thread.start()
        self._thread = thread

    def _release_worker_claim(self) -> None:
        with self._lock:
            claim = self._worker_claim
            self._worker_claim = None
        if claim is not None:
            claim.release()

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
            try:
                _validate_finalized_migration_receipts(self.paths)
            except _LegacyMigrationError as exc:
                return WorkflowStatus.BLOCKED, exc.code, None
            status = self._status
            error = self._error
            if (
                status
                in {
                    WorkflowStatus.COMPLETED,
                    WorkflowStatus.BLOCKED,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.CANCELLED,
                }
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return WorkflowStatus.RUNNING, None, None
            if status is WorkflowStatus.IDLE:
                return self._persisted_status()
            if status is WorkflowStatus.COMPLETED:
                try:
                    final = _read_completed_ocr_final(self.paths.final, self.source_path)
                except (OSError, ValueError):
                    return WorkflowStatus.BLOCKED, "ocr_evidence_invalid", None
                return WorkflowStatus.COMPLETED, None, final
            return status, error, None

    def _run(
        self,
        dpi: int,
        languages: tuple[str, ...],
        pages: tuple[int, ...] | None,
        sample_rate: float,
    ) -> None:
        try:
            page_numbers = (
                list(pages)
                if pages is not None
                else list(range(1, count_pdf_pages(self.source_path) + 1))
            )
            orchestrator = self._factory(self._cancel.is_set)
            result = orchestrator.run_batch(
                self.source_path,
                pages=page_numbers,
                dpi=dpi,
                languages=languages,
                sample_rate=sample_rate,
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
                cancelled = self._cancel.is_set()
                self._status = WorkflowStatus.CANCELLED if cancelled else WorkflowStatus.FAILED
                self._error = "ocr_cancelled" if cancelled else _safe_error(exc)
        finally:
            self._release_worker_claim()

    def detect_chapters(self) -> dict[str, Any]:
        with _publication_transaction_lock(self.paths) as publication_lock:
            final, pages = self.completed_evidence()
            fingerprint = _input_fingerprint(final)
            tree = detect_chapter_tree(pages, input_fingerprint=fingerprint)
            _require_active_publication_lock(publication_lock)
            _atomic_json(self.paths.chapter_tree, tree)
            _require_active_publication_lock(publication_lock)
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
        with _publication_transaction_lock(self.paths) as publication_lock:
            current_final, pages = self.completed_evidence()
            if current_final != expected_final:
                raise ValueError("OCR final changed during note generation")
            detected_tree = detect_chapter_tree(
                pages, input_fingerprint=_input_fingerprint(current_final)
            )
            if expected_tree != detected_tree:
                raise ValueError("published chapter tree fingerprint is invalid")
            validate_chapter_confirmation(confirmation, expected_tree)
            if confirmation.get("action") not in {"confirm", "edit"}:
                raise ValueError("published chapter confirmation is invalid")
            _read_matching_chapter_context(
                self.paths,
                expected_tree=expected_tree,
                expected_confirmation=confirmation,
            )
            with _temporary_note_path(self.paths.root) as temporary_path:
                note = generate(temporary_path)
                _require_active_publication_lock(publication_lock)
                _read_matching_chapter_context(
                    self.paths,
                    expected_tree=expected_tree,
                    expected_confirmation=confirmation,
                )
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
                    publication_lock=publication_lock,
                )
                return note, artifact_path

    def _persisted_status(
        self,
    ) -> tuple[WorkflowStatus, str | None, dict[str, Any] | None]:
        if self._migration_error is not None:
            return WorkflowStatus.BLOCKED, self._migration_error, None
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
        with _publication_transaction_lock(self.paths) as publication_lock:
            _final, _pages, tree = self.completed_chapter_context()
            confirmation = build_confirmation(tree, chapter_id)
            _require_active_publication_lock(publication_lock)
            persist_chapter_confirmation(self.paths.confirmation, confirmation)
            _require_active_publication_lock(publication_lock)
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
    _atomic_bytes(path, _canonical_json_bytes(value))


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_atomic_target(path)
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError("atomic artifact is too large")
    temporary, candidate = _write_temporary_regular(
        path.parent,
        prefix=f".{path.name}.",
        content=encoded,
    )
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        _retire_owned_temporary(
            temporary,
            candidate.identity,
            error_code="ocr_state_invalid",
        )


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, WorkflowBlockedError):
        return "ocr_provider_unavailable"
    if isinstance(exc, TimeoutError):
        return "ocr_timeout"
    return "ocr_workflow_failed"
