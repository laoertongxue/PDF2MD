from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Literal, NotRequired, TextIO, TypedDict

from parsing_core.workbench.models import Card, Chapter, NoteBlock, Source
from parsing_core.workbench.repository import (
    ChapterGenerationConflictError,
    FilePublicationReceipt,
    MarkdownPublicationClaim,
    MarkdownPublicationFence,
    WorkbenchRepository,
    _verified_file_publication_receipt,
    read_stable_source_markdown,
)
from parsing_core.workbench.topic_task_package import allocate_source_display_titles

MERMAID_FENCE_RE = re.compile(r"^\s*```mermaid\s*\n(.*?)```\s*$", re.DOTALL | re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)\b(?:proxy[-_]?authorization|authorization)\b"
    r"\s*[\"']?\s*[:=]\s*[\"']?\s*"
    r"(?:(?:bearer|basic|digest|token|apikey)\s+)?"
    r"[^\s,;\"'\}\]\)]+"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
MAX_NAME_BYTES = 180
JOURNAL_NAME = ".pdf2md-bundle-journal.json"
LOCK_NAME = ".pdf2md-bundle.lock"
OWNER_NAME = ".pdf2md-owner"
MAX_BUNDLE_JOURNAL_BYTES = 1024 * 1024
MAX_MARKER_LINE_BYTES = 512
MAX_MARKER_SCAN_ENTRIES = 4096
MAX_REDACTION_INPUT_BYTES = 16 * 1024 * 1024
MAX_REDACTION_OUTPUT_BYTES = 32 * 1024 * 1024


class _BundleThreadLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_BUNDLE_LOCKS: dict[tuple[int, int], _BundleThreadLock] = {}
_BUNDLE_LOCKS_GUARD = threading.Lock()


class ChapterMarkdownSyncError(RuntimeError):
    code = "CHAPTER_MARKDOWN_PUBLICATION_FAILED"
    message = "chapter Markdown publication failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class _BundleEntry(TypedDict):
    target: str
    temp: str
    backup: str | None
    existed: bool


class _BundleJournal(TypedDict):
    phase: str
    entries: list[_BundleEntry]
    transaction_id: NotRequired[str]


@contextmanager
def _owned_fdopen(
    fd: int,
    mode: Literal["r", "w"] = "r",
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Iterator[TextIO]:
    try:
        handle: TextIO = os.fdopen(fd, mode, encoding=encoding, newline=newline)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        yield handle


SAFE_COMPONENT_RE = re.compile(r"^[^/\\\x00]+$")


def safe_name(value: str, fallback: str = "untitled") -> str:
    value = unicodedata.normalize("NFC", value)
    value = CONTROL_RE.sub("", value).replace("/", "-").replace("\\", "-")
    value = value.strip().rstrip(". ")
    while value.startswith("."):
        value = value[1:]
    value = value.strip().rstrip(". ") or fallback
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return value
    suffix = "-" + hashlib.sha256(encoded).hexdigest()[:12]
    budget = MAX_NAME_BYTES - len(suffix)
    shortened = value
    while len(shortened.encode("utf-8")) > budget:
        shortened = shortened[:-1]
    return shortened.rstrip(". ") + suffix


def _normalized_safe_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def allocate_safe_source_names(sources: list[tuple[str, str]]) -> dict[str, str]:
    display_titles = allocate_source_display_titles(sources)
    safe_bases = {source_id: safe_name(display_titles[source_id]) for source_id, _ in sources}
    reserved = {_normalized_safe_name(value) for value in safe_bases.values()}
    assigned: set[str] = set()
    result = {}
    for source_id, _ in sources:
        base = safe_bases[source_id]
        normalized = _normalized_safe_name(base)
        if normalized not in assigned:
            candidate = base
        else:
            candidate = ""
            for suffix in range(2, 10_001):
                possible = safe_name(f"{base}（{suffix}）")
                normalized_possible = _normalized_safe_name(possible)
                if normalized_possible not in reserved and normalized_possible not in assigned:
                    candidate = possible
                    break
            if not candidate:
                raise ValueError("unable to allocate unique safe source directory")
        assigned.add(_normalized_safe_name(candidate))
        result[source_id] = candidate
    return result


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pdf2md-tmp", dir=path.parent)
    backup_name: str | None = None
    replaced = False
    try:
        with _owned_fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode & 0o777)
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".pdf2md-backup", dir=path.parent
            )
            os.close(backup_fd)
            os.unlink(backup_name)
            os.link(path, backup_name)
        else:
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        replaced = True
        _fsync_directory(path.parent)
    except Exception:
        backup_restored = False
        if replaced:
            try:
                if backup_name is not None and Path(backup_name).exists():
                    os.replace(backup_name, path)
                    backup_restored = True
                else:
                    path.unlink(missing_ok=True)
                try:
                    _fsync_directory(path.parent)
                except OSError:
                    pass
            except OSError:
                pass
        _unlink_if_generated(temp_name, ".pdf2md-tmp")
        if backup_name is not None and (not replaced or backup_restored):
            _unlink_if_generated(backup_name, ".pdf2md-backup")
        raise
    if backup_name is not None:
        try:
            os.unlink(backup_name)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise OSError("atomic write committed but backup cleanup failed") from exc


def redact_sensitive_text(value: str) -> str:
    if type(value) is not str:
        raise ValueError("Markdown redaction input is invalid")
    try:
        input_size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Markdown redaction input is invalid") from None
    if input_size > MAX_REDACTION_INPUT_BYTES:
        raise ValueError("Markdown redaction input exceeds limit")
    value = AUTHORIZATION_HEADER_RE.sub("[REDACTED]", value)
    value = BEARER_TOKEN_RE.sub("Bearer [REDACTED]", value)
    patterns = (
        r"file://[^\s<>()]+",
        r"(?<![\w:/.])/(?!/)(?:[^\s/<>()[\]{}'\"`]+/)*[^\s/<>()[\]{}'\"`]+",
        r"[A-Za-z]:\\[^\s<>()]+",
        r"\bsk-[A-Za-z0-9_-]{12,}\b",
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s]+",
    )
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value)
    try:
        output_size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Markdown redaction output is invalid") from None
    if output_size > MAX_REDACTION_OUTPUT_BYTES:
        raise ValueError("Markdown redaction output exceeds limit")
    return value


def open_secure_directory(root: Path, parts: list[str], *, create: bool = True) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        fd = os.open(root, flags)
        stack.callback(os.close, fd)
        root_identity = os.fstat(fd).st_dev, os.fstat(fd).st_ino
        for part in parts:
            if not SAFE_COMPONENT_RE.fullmatch(part) or part in {".", ".."}:
                raise ValueError("unsafe directory component")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            stack.callback(os.close, child)
            fd = child
        verify_fd = os.open(root, flags)
        stack.callback(os.close, verify_fd)
        if (os.fstat(verify_fd).st_dev, os.fstat(verify_fd).st_ino) != root_identity:
            raise OSError("course root identity changed")
        return os.dup(fd)


def _write_fd_file(dir_fd: int, name: str, content: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
    try:
        with _owned_fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _unlink_at(dir_fd, name)
        raise


def _unlink_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _validate_journal_name(name: str, suffix: str) -> None:
    if Path(name).name != name or not name.startswith(".pdf2md-") or not name.endswith(suffix):
        raise ValueError("unsafe bundle journal entry")


def _write_journal(dir_fd: int, journal: _BundleJournal) -> None:
    temp = f".pdf2md-{os.urandom(8).hex()}.journal-tmp"
    try:
        _write_fd_file(dir_fd, temp, json.dumps(journal, ensure_ascii=False, sort_keys=True))
        os.replace(temp, JOURNAL_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        _unlink_at(dir_fd, temp)


def _recover_atomic_bundle_locked(
    dir_fd: int,
    *,
    transaction_committed: Callable[[str], bool] | None = None,
) -> None:
    try:
        fd = os.open(
            JOURNAL_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
    except FileNotFoundError:
        return
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE_JOURNAL_BYTES:
        os.close(fd)
        raise ValueError("invalid bundle journal")
    with _owned_fdopen(fd, encoding="utf-8") as handle:
        raw_journal: object = json.loads(handle.read(MAX_BUNDLE_JOURNAL_BYTES + 1))
    if not isinstance(raw_journal, dict):
        raise ValueError("invalid bundle journal")
    phase: object = raw_journal.get("phase")
    if phase not in {"PREPARED", "COMMITTED"}:
        raise ValueError("invalid bundle journal phase")
    raw_entries: object = raw_journal.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("invalid bundle journal")
    entries: list[_BundleEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("invalid bundle journal entry")
        target: object = raw_entry.get("target")
        temp: object = raw_entry.get("temp")
        backup: object = raw_entry.get("backup")
        existed: object = raw_entry.get("existed")
        if (
            not isinstance(target, str)
            or not isinstance(temp, str)
            or (backup is not None and not isinstance(backup, str))
            or not isinstance(existed, bool)
        ):
            raise ValueError("invalid bundle journal entry")
        entries.append({"target": target, "temp": temp, "backup": backup, "existed": existed})
    transaction_id_value: object = raw_journal.get("transaction_id")
    if transaction_id_value is not None and (
        not isinstance(transaction_id_value, str)
        or re.fullmatch(r"[0-9a-f]{32}", transaction_id_value) is None
    ):
        raise ValueError("invalid bundle journal transaction")
    journal: _BundleJournal = {"phase": phase, "entries": entries}
    if isinstance(transaction_id_value, str):
        journal["transaction_id"] = transaction_id_value
        if journal["phase"] == "PREPARED":
            if transaction_committed is None:
                raise RuntimeError("bundle publication recovery requires transaction state")
            if transaction_committed(transaction_id_value):
                journal["phase"] = "COMMITTED"
    for entry in entries:
        target, temp, backup = entry["target"], entry["temp"], entry.get("backup")
        if Path(target).name != target:
            raise ValueError("unsafe bundle target")
        _validate_journal_name(temp, ".bundle-tmp")
        if backup is not None:
            _validate_journal_name(backup, ".bundle-backup")
    for entry in entries:
        target, temp, backup = entry["target"], entry["temp"], entry.get("backup")
        if journal["phase"] == "PREPARED":
            if backup is not None:
                try:
                    os.replace(backup, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            elif not entry["existed"]:
                _unlink_at(dir_fd, target)
        _unlink_at(dir_fd, temp)
        if backup is not None:
            _unlink_at(dir_fd, backup)
    _unlink_at(dir_fd, JOURNAL_NAME)
    os.fsync(dir_fd)


@contextmanager
def _locked_bundle_directories(
    dir_fds: list[int],
) -> Iterator[dict[tuple[int, int], int]]:
    owned: dict[tuple[int, int], int] = {}
    ordered: list[tuple[int, int]] = []
    thread_locks: list[_BundleThreadLock] = []
    acquired_thread_locks: list[threading.Lock] = []
    locked_identities: list[tuple[int, int]] = []
    marker_fds: dict[tuple[int, int], int] = {}
    marker_identities: dict[tuple[int, int], tuple[int, int]] = {}
    try:
        for dir_fd in dir_fds:
            owned_fd = os.dup(dir_fd)
            info = os.fstat(owned_fd)
            identity = (info.st_dev, info.st_ino)
            if identity in owned:
                os.close(owned_fd)
            else:
                owned[identity] = owned_fd
        ordered = sorted(owned)
        with _BUNDLE_LOCKS_GUARD:
            for identity in ordered:
                thread_lock = _BUNDLE_LOCKS.get(identity)
                if thread_lock is None:
                    thread_lock = _BundleThreadLock()
                    _BUNDLE_LOCKS[identity] = thread_lock
                thread_lock.users += 1
                thread_locks.append(thread_lock)
        for thread_lock in thread_locks:
            thread_lock.lock.acquire()
            acquired_thread_locks.append(thread_lock.lock)
        for identity in ordered:
            owned_fd = owned[identity]
            current = os.fstat(owned_fd)
            if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
                raise OSError("publication lock directory identity changed")
            fcntl.flock(owned_fd, fcntl.LOCK_EX)
            locked = os.fstat(owned_fd)
            if (locked.st_dev, locked.st_ino) != identity or not stat.S_ISDIR(locked.st_mode):
                raise OSError("publication lock directory identity changed")
            locked_identities.append(identity)
            marker_fd = os.open(
                LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=owned_fd,
            )
            marker = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(marker.st_mode)
                or marker.st_nlink != 1
                or marker.st_uid != os.getuid()
            ):
                os.close(marker_fd)
                raise OSError("publication lock identity is invalid")
            marker_fds[identity] = marker_fd
            marker_identities[identity] = (marker.st_dev, marker.st_ino)
        yield owned
        for identity in ordered:
            current = os.fstat(owned[identity])
            if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
                raise OSError("publication lock directory identity changed")
            verify_fd = os.open(LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=owned[identity])
            try:
                marker = os.fstat(verify_fd)
                if (
                    not stat.S_ISREG(marker.st_mode)
                    or marker.st_nlink != 1
                    or marker.st_uid != os.getuid()
                    or (marker.st_dev, marker.st_ino) != marker_identities[identity]
                ):
                    raise OSError("publication lock identity changed")
            finally:
                os.close(verify_fd)
    finally:
        for marker_fd in marker_fds.values():
            try:
                os.close(marker_fd)
            except OSError:
                pass
        for identity in reversed(locked_identities):
            try:
                fcntl.flock(owned[identity], fcntl.LOCK_UN)
            except OSError:
                pass
        for acquired_lock in reversed(acquired_thread_locks):
            acquired_lock.release()
        with _BUNDLE_LOCKS_GUARD:
            for index, identity in enumerate(ordered):
                thread_lock = thread_locks[index]
                thread_lock.users -= 1
                if thread_lock.users == 0 and _BUNDLE_LOCKS.get(identity) is thread_lock:
                    del _BUNDLE_LOCKS[identity]
        for owned_fd in owned.values():
            os.close(owned_fd)


def recover_atomic_bundle(
    dir_fd: int,
    *,
    transaction_committed: Callable[[str], bool] | None = None,
) -> None:
    info = os.fstat(dir_fd)
    identity = (info.st_dev, info.st_ino)
    with _locked_bundle_directories([dir_fd]) as owned:
        _recover_atomic_bundle_locked(
            owned[identity],
            transaction_committed=transaction_committed,
        )


def atomic_write_bundle_fd(
    dir_fd: int,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None = None,
) -> None:
    info = os.fstat(dir_fd)
    identity = (info.st_dev, info.st_ino)
    with _locked_bundle_directories([dir_fd]) as owned:
        owned_fd = owned[identity]
        if fence is not None:
            fence()
        _atomic_write_bundle_locked(owned_fd, contents, fence=fence)


@contextmanager
def staged_atomic_write_bundle_fd(
    dir_fd: int,
    contents: dict[str, str],
    *,
    transaction_id: str,
    transaction_committed: Callable[[str], bool],
) -> Iterator[FilePublicationReceipt]:
    with staged_atomic_write_bundles_fd(
        [(dir_fd, contents)],
        transaction_id=transaction_id,
        transaction_committed=transaction_committed,
    ) as receipt:
        yield receipt


def _bundle_file_fingerprint(
    grouped: dict[tuple[int, int], dict[str, str]],
) -> str:
    manifest = [
        {
            "directory": identity,
            "files": [
                (name, hashlib.sha256(content.encode("utf-8")).hexdigest())
                for name, content in sorted(contents.items())
            ],
        }
        for identity, contents in sorted(grouped.items())
    ]
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def publication_bundle_fingerprint(bundles: list[tuple[int, dict[str, str]]]) -> str:
    grouped: dict[tuple[int, int], dict[str, str]] = {}
    for dir_fd, contents in bundles:
        info = os.fstat(dir_fd)
        identity = (info.st_dev, info.st_ino)
        target = grouped.setdefault(identity, {})
        for name, content in contents.items():
            if name in target and target[name] != content:
                raise ValueError("publication contains conflicting bundle targets")
            target[name] = content
    if not grouped:
        raise ValueError("publication must contain at least one file bundle")
    return _bundle_file_fingerprint(grouped)


@contextmanager
def _locked_atomic_write_bundles_fd(
    bundles: list[tuple[int, dict[str, str]]],
    *,
    transaction_id: str,
    transaction_committed: Callable[[str], bool],
) -> Iterator[Callable[[], FilePublicationReceipt]]:
    if re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise ValueError("invalid bundle publication transaction")
    if not bundles:
        raise ValueError("publication must contain at least one file bundle")
    grouped: dict[tuple[int, int], dict[str, str]] = {}
    fds: dict[tuple[int, int], int] = {}
    for dir_fd, contents in bundles:
        info = os.fstat(dir_fd)
        identity = (info.st_dev, info.st_ino)
        target = grouped.setdefault(identity, {})
        for name, content in contents.items():
            if name in target and target[name] != content:
                raise ValueError("publication contains conflicting bundle targets")
            target[name] = content
        fds.setdefault(identity, dir_fd)
    receipt = _verified_file_publication_receipt(
        transaction_id,
        _bundle_file_fingerprint(grouped),
    )
    prepared: dict[tuple[int, int], list[_BundleEntry]] = {}
    published = False

    with _locked_bundle_directories(list(fds.values())) as owned:
        try:
            for identity in sorted(grouped):
                _recover_atomic_bundle_locked(
                    owned[identity],
                    transaction_committed=transaction_committed,
                )

            def publish() -> FilePublicationReceipt:
                nonlocal published
                if published:
                    raise RuntimeError("file publication receipt is already bound")
                for identity in sorted(grouped):
                    prepared[identity] = _prepare_atomic_bundle_locked(
                        owned[identity],
                        grouped[identity],
                        transaction_id=transaction_id,
                    )
                published = True
                return receipt

            yield publish
            if not published:
                raise RuntimeError("file publication receipt is required")
            for identity in sorted(grouped):
                _recover_atomic_bundle_locked(
                    owned[identity],
                    transaction_committed=transaction_committed,
                )
        except BaseException:
            try:
                for identity in sorted(grouped):
                    _recover_atomic_bundle_locked(
                        owned[identity],
                        transaction_committed=transaction_committed,
                    )
            finally:
                for identity, entries in prepared.items():
                    for entry in entries:
                        _unlink_at(owned[identity], entry["temp"])
            raise


@contextmanager
def staged_atomic_write_bundles_fd(
    bundles: list[tuple[int, dict[str, str]]],
    *,
    transaction_id: str,
    transaction_committed: Callable[[str], bool],
) -> Iterator[FilePublicationReceipt]:
    if re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise ValueError("invalid bundle publication transaction")
    if not bundles:
        raise ValueError("publication must contain at least one file bundle")
    grouped: dict[tuple[int, int], dict[str, str]] = {}
    fds: dict[tuple[int, int], int] = {}
    for dir_fd, contents in bundles:
        info = os.fstat(dir_fd)
        identity = (info.st_dev, info.st_ino)
        target = grouped.setdefault(identity, {})
        for name, content in contents.items():
            if name in target and target[name] != content:
                raise ValueError("publication contains conflicting bundle targets")
            target[name] = content
        fds.setdefault(identity, dir_fd)
    receipt = _verified_file_publication_receipt(
        transaction_id,
        _bundle_file_fingerprint(grouped),
    )
    prepared: dict[tuple[int, int], list[_BundleEntry]] = {}
    with _locked_bundle_directories(list(fds.values())) as owned:
        try:
            for identity in sorted(grouped):
                _recover_atomic_bundle_locked(
                    owned[identity],
                    transaction_committed=transaction_committed,
                )
            for identity in sorted(grouped):
                prepared[identity] = _prepare_atomic_bundle_locked(
                    owned[identity],
                    grouped[identity],
                    transaction_id=transaction_id,
                )
            yield receipt
            for identity in sorted(grouped):
                _recover_atomic_bundle_locked(
                    owned[identity],
                    transaction_committed=transaction_committed,
                )
        except BaseException:
            try:
                for identity in sorted(grouped):
                    _recover_atomic_bundle_locked(
                        owned[identity],
                        transaction_committed=transaction_committed,
                    )
            finally:
                for identity, entries in prepared.items():
                    for entry in entries:
                        _unlink_at(owned[identity], entry["temp"])
            raise


def _publication_race_hook(entity_type: str, entity_id: str) -> None:
    pass


def commit_markdown_publication(
    repo: WorkbenchRepository,
    claim: MarkdownPublicationClaim,
    bundles: list[tuple[int, dict[str, str]]],
    *,
    fence_context: Callable[[], AbstractContextManager[MarkdownPublicationFence]] | None = None,
) -> None:
    def committed(token: str) -> bool:
        return repo.markdown_publication_committed(
            claim.entity_type,
            claim.entity_id,
            token,
        )

    context = repo.fence_markdown_publication(claim) if fence_context is None else fence_context()
    try:
        with _locked_atomic_write_bundles_fd(
            bundles,
            transaction_id=claim.publication_id,
            transaction_committed=committed,
        ) as publish:
            with context as fence:
                receipt = publish()
                fence.bind_file_publication(receipt)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if not committed(claim.publication_id):
            raise


def _atomic_write_bundle_locked(
    dir_fd: int,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None,
) -> None:
    _recover_atomic_bundle_locked(dir_fd)
    entries: list[_BundleEntry] = []
    try:
        entries = _prepare_atomic_bundle_locked(
            dir_fd,
            contents,
            before_replace=fence,
        )
        journal: _BundleJournal = {"phase": "COMMITTED", "entries": entries}
        _write_journal(dir_fd, journal)
        _recover_atomic_bundle_locked(dir_fd)
    except BaseException:
        try:
            _recover_atomic_bundle_locked(dir_fd)
        finally:
            for entry in entries:
                _unlink_at(dir_fd, entry["temp"])
                if entry["backup"] is not None:
                    _unlink_at(dir_fd, entry["backup"])
        raise


def _prepare_atomic_bundle_locked(
    dir_fd: int,
    contents: dict[str, str],
    *,
    transaction_id: str | None = None,
    before_replace: Callable[[], object] | None = None,
) -> list[_BundleEntry]:
    entries: list[_BundleEntry] = []
    current_temp: str | None = None
    journal_written = False
    try:
        for target, content in contents.items():
            if Path(target).name != target:
                raise ValueError("bundle targets must be basenames")
            token = os.urandom(8).hex()
            temp = f".pdf2md-{token}.bundle-tmp"
            current_temp = temp
            backup_name = f".pdf2md-{token}.bundle-backup"
            backup: str | None = backup_name
            _write_fd_file(dir_fd, temp, content)
            existed = True
            try:
                os.link(
                    target,
                    backup_name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existed = False
                backup = None
            entries.append({"target": target, "temp": temp, "backup": backup, "existed": existed})
            current_temp = None
        journal: _BundleJournal = {"phase": "PREPARED", "entries": entries}
        if transaction_id is not None:
            journal["transaction_id"] = transaction_id
        _write_journal(dir_fd, journal)
        journal_written = True
        if before_replace is not None:
            before_replace()
        for entry in entries:
            os.replace(entry["temp"], entry["target"], src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
        return entries
    except BaseException:
        if not journal_written:
            if current_temp is not None:
                _unlink_at(dir_fd, current_temp)
            for entry in entries:
                _unlink_at(dir_fd, entry["temp"])
                if entry["backup"] is not None:
                    _unlink_at(dir_fd, entry["backup"])
        raise


def atomic_write_bundle(
    directory: Path,
    contents: dict[str, str],
    *,
    fence: Callable[[], object] | None = None,
) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        atomic_write_bundle_fd(fd, contents, fence=fence)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _unlink_if_generated(name: str, suffix: str) -> None:
    path = Path(name)
    if path.name.startswith(".") and path.name.endswith(suffix):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def textbook_dir(repo: WorkbenchRepository, source: Source) -> Path:
    course = repo.get_course(source.course_id)
    if course is None:
        raise ValueError("course not found")
    sources = repo.list_sources(source.course_id)
    names = allocate_safe_source_names([(item.id, item.title) for item in sources])
    return Path(course.root_dir) / "教材" / names[source.id]


def _first_line_regular_file(path: Path) -> str | None:
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return _bounded_first_line(fd)
    finally:
        os.close(fd)


def _bounded_first_line(fd: int) -> str | None:
    data = os.read(fd, MAX_MARKER_LINE_BYTES + 1)
    newline = data.find(b"\n")
    first_line = data if newline < 0 else data[:newline]
    if first_line.endswith(b"\r"):
        first_line = first_line[:-1]
    if len(first_line) > MAX_MARKER_LINE_BYTES or (
        newline < 0 and len(data) > MAX_MARKER_LINE_BYTES
    ):
        return None
    try:
        return first_line.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _owner_value(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


def _has_owner(path: Path, entity_type: str, entity_id: str) -> bool:
    return _first_line_regular_file(path / OWNER_NAME) == _owner_value(entity_type, entity_id)


def ensure_directory_owner(
    dir_fd: int,
    entity_type: str,
    entity_id: str,
    *,
    allow_create_or_replace: bool,
) -> None:
    expected = _owner_value(entity_type, entity_id)
    try:
        fd = os.open(OWNER_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        current = None
    else:
        try:
            current = _bounded_first_line(fd)
        finally:
            os.close(fd)
    if current == expected:
        return
    if not allow_create_or_replace:
        raise FileExistsError("directory ownership marker does not match")
    temp = f".pdf2md-{os.urandom(8).hex()}.owner-tmp"
    _write_fd_file(dir_fd, temp, expected + "\n")
    os.replace(temp, OWNER_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def _directory_with_marker(root: Path, marker: str) -> Path | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise OSError("marker root cannot be opened safely")
    matches = []
    levels = 2 if marker.startswith("<!-- chapter-id:") else 1
    candidates = [root]
    scanned = 0
    for _ in range(levels):
        next_candidates = []
        for directory in candidates:
            for child in directory.iterdir():
                scanned += 1
                if scanned > MAX_MARKER_SCAN_ENTRIES:
                    raise ValueError("marker scan limit exceeded")
                if child.is_symlink():
                    raise OSError("symlink directory rejected")
                if child.is_dir():
                    next_candidates.append(child)
        candidates = next_candidates
    marker_file = "intensive-note.md" if levels == 2 else "topic-map.md"
    for directory in candidates:
        if _first_line_regular_file(directory / marker_file) == marker:
            matches.append(directory)
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise ValueError("multiple generated directories contain the same marker")
    return unique[0] if unique else None


def migrate_generated_directory(
    root: Path,
    target: Path,
    marker: str,
    legacy: Path | None = None,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> Path:
    current = _directory_with_marker(root, marker)
    if current is None and legacy is not None and legacy.exists() and not target.exists():
        current = legacy
    if current is None and target.exists():
        if entity_type and entity_id and _has_owner(target, entity_type, entity_id):
            return target
        raise FileExistsError(f"target directory already exists: {target.name}")
    if current is not None and current != target:
        if target.exists():
            raise FileExistsError(f"target directory already exists: {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            source_parent_fd = os.open(current.parent, flags)
            stack.callback(os.close, source_parent_fd)
            target_parent_fd = os.open(target.parent, flags)
            stack.callback(os.close, target_parent_fd)
            os.replace(
                current.name,
                target.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
            )
    return target


def sync_chapter_markdown(repo: WorkbenchRepository, chapter_id: str) -> dict[str, str]:
    failed = False
    result: dict[str, str] | None = None
    try:
        result = _sync_chapter_markdown_entry(repo, chapter_id)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed:
        raise ChapterMarkdownSyncError()
    if result is None:
        raise ChapterMarkdownSyncError()
    return result


def _sync_chapter_markdown_entry(repo: WorkbenchRepository, chapter_id: str) -> dict[str, str]:
    state_fingerprint, state_revision = repo.markdown_publication_state_snapshot(
        "chapter", chapter_id
    )
    with ExitStack() as stack:
        bundles: list[tuple[int, dict[str, str]]] = []
        result = _sync_chapter_markdown(
            repo,
            chapter_id,
            stack,
            bundle_writer=lambda dir_fd, contents: bundles.append((dir_fd, contents)),
        )
        claim = repo.claim_markdown_publication(
            "chapter",
            chapter_id,
            state_fingerprint,
            publication_bundle_fingerprint(bundles),
            state_revision=state_revision,
        )
        _publication_race_hook("chapter", chapter_id)
        commit_markdown_publication(repo, claim, bundles)
        return result


def publish_chapter_markdown(
    repo: WorkbenchRepository,
    chapter_id: str,
    owner_id: str,
    review_run_id: str,
    publication_id: str,
    *,
    clock: Callable[[], int],
) -> dict[str, str]:
    failed = False
    result: dict[str, str] | None = None
    try:
        result = _publish_chapter_markdown_entry(
            repo,
            chapter_id,
            owner_id,
            review_run_id,
            publication_id,
            clock=clock,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        failed = True
    if failed:
        raise ChapterMarkdownSyncError()
    if result is None:
        raise ChapterMarkdownSyncError()
    return result


def _publish_chapter_markdown_entry(
    repo: WorkbenchRepository,
    chapter_id: str,
    owner_id: str,
    review_run_id: str,
    publication_id: str,
    *,
    clock: Callable[[], int],
) -> dict[str, str]:
    pending = repo.pending_chapter_markdown_sync(chapter_id)
    if (
        pending is None
        or pending["owner_id"] != owner_id
        or pending["review_run_id"] != review_run_id
        or pending["publication_id"] != publication_id
    ):
        raise ChapterGenerationConflictError(
            "run_not_running",
            "chapter generation run is not staged",
        )
    state_fingerprint, state_revision = repo.markdown_publication_state_snapshot(
        "chapter", chapter_id
    )
    with ExitStack() as stack:
        bundles: list[tuple[int, dict[str, str]]] = []
        result = _sync_chapter_markdown(
            repo,
            chapter_id,
            stack,
            bundle_writer=lambda dir_fd, contents: bundles.append((dir_fd, contents)),
            expected_input_fingerprint=pending["input_fingerprint"],
        )
        claimed_at = clock()
        lease = repo.get_chapter_generation_lease(chapter_id)
        if lease is None or lease.owner_id != owner_id or lease.expires_at <= claimed_at:
            raise ChapterGenerationConflictError(
                "lease_lost",
                "chapter generation lease lost",
            )
        claim = repo.claim_markdown_publication(
            "chapter",
            chapter_id,
            state_fingerprint,
            publication_bundle_fingerprint(bundles),
            state_revision=state_revision,
            owner_id=owner_id,
            publication_id=publication_id,
            now=claimed_at,
            lease_ttl=lease.expires_at - claimed_at,
        )
        _publication_race_hook("chapter", chapter_id)
        commit_markdown_publication(
            repo,
            claim,
            bundles,
            fence_context=lambda: repo.fence_chapter_markdown_publication(
                chapter_id,
                owner_id,
                review_run_id,
                publication_id,
                claim=claim,
                clock=clock,
            ),
        )
        return result


def recover_chapter_publication_journals(
    repo: WorkbenchRepository,
    chapter_id: str,
) -> None:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")
    source = repo.get_source(chapter.source_id)
    course = repo.get_course(chapter.course_id)
    if source is None or course is None:
        raise ValueError("chapter dependencies not found")
    course_root = Path(course.root_dir)
    if course_root.is_symlink():
        raise OSError("course root cannot be opened safely")
    if not course_root.exists():
        return
    if not course_root.is_dir():
        raise OSError("course root cannot be opened safely")
    source_root = textbook_dir(repo, source)
    chapter_name = f"{chapter.seq + 1:02d}-{safe_name(chapter.title)}"
    chapter_dir = source_root / chapter_name
    if not chapter_dir.is_dir() or chapter_dir.is_symlink():
        return
    relative = chapter_dir.relative_to(course_root)

    def committed(token: str) -> bool:
        return repo.chapter_publication_committed(chapter_id, token)

    with ExitStack() as stack:
        chapter_fd = open_secure_directory(
            course_root,
            list(relative.parts),
            create=False,
        )
        stack.callback(os.close, chapter_fd)
        recover_atomic_bundle(
            chapter_fd,
            transaction_committed=committed,
        )
        try:
            runs_fd = open_secure_directory(
                course_root,
                [*relative.parts, "runs"],
                create=False,
            )
        except FileNotFoundError:
            return
        stack.callback(os.close, runs_fd)
        recover_atomic_bundle(
            runs_fd,
            transaction_committed=committed,
        )


def _sync_chapter_markdown(
    repo: WorkbenchRepository,
    chapter_id: str,
    stack: ExitStack,
    *,
    bundle_writer: Callable[[int, dict[str, str]], None],
    expected_input_fingerprint: str | None = None,
) -> dict[str, str]:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError("chapter not found")
    source = repo.get_source(chapter.source_id)
    course = repo.get_course(chapter.course_id)
    if source is None or course is None:
        raise ValueError("chapter dependencies not found")
    course_root = Path(course.root_dir)
    course_root.mkdir(parents=True, exist_ok=True)
    if course_root.is_symlink():
        raise OSError("course root symlink rejected")
    source_root = textbook_dir(repo, source)
    chapter_name = f"{chapter.seq + 1:02d}-{safe_name(chapter.title)}"
    target = source_root / chapter_name
    legacy = Path(course.root_dir) / chapter_name
    textbooks_fd = open_secure_directory(course_root, ["教材"])
    stack.callback(os.close, textbooks_fd)
    source_fd = open_secure_directory(course_root, ["教材", source_root.name])
    stack.callback(os.close, source_fd)
    target_existed = target.exists()
    chapter_dir = migrate_generated_directory(
        course_root / "教材",
        target,
        f"<!-- chapter-id: {chapter.id} -->",
        legacy,
        entity_type="chapter",
        entity_id=chapter.id,
    )
    relative = chapter_dir.relative_to(course_root)
    chapter_fd = open_secure_directory(course_root, list(relative.parts))
    stack.callback(os.close, chapter_fd)
    formal_owner = _first_line_regular_file(chapter_dir / "intensive-note.md") == (
        f"<!-- chapter-id: {chapter.id} -->"
    )
    ensure_directory_owner(
        chapter_fd,
        "chapter",
        chapter.id,
        allow_create_or_replace=not target_existed or formal_owner,
    )
    attachments_fd = open_secure_directory(course_root, [*relative.parts, "attachments"])
    stack.callback(os.close, attachments_fd)
    runs_fd = open_secure_directory(course_root, [*relative.parts, "runs"])
    stack.callback(os.close, runs_fd)

    source_path = chapter_dir / "source.md"
    source_md_path = Path(chapter.source_md_path)
    source_bytes, source_hash = read_stable_source_markdown(source_md_path)
    if (
        expected_input_fingerprint is not None
        and repo.chapter_input_snapshot(
            chapter_id,
            source_content_hash=source_hash,
        )[1]
        != expected_input_fingerprint
    ):
        raise ChapterGenerationConflictError(
            "input_changed",
            "staged chapter input changed",
        )
    try:
        source_content = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("chapter source must be UTF-8") from exc
    note_path = chapter_dir / "intensive-note.md"
    cards_path = chapter_dir / "cards.md"
    chapter_contents = {
        "source.md": source_content,
        "intensive-note.md": _render_note(chapter, repo.list_note_blocks(chapter.id)),
        "cards.md": _render_cards(chapter, repo.list_cards_by_chapter(chapter.id)),
    }
    chapter_contents = {
        name: redact_sensitive_text(content) for name, content in chapter_contents.items()
    }
    bundle_writer(chapter_fd, chapter_contents)
    run_contents = {}
    for run in repo.list_runs(chapter.id):
        run_contents[f"{safe_name(run.round_key)}.md"] = redact_sensitive_text(
            "\n".join(
                [
                    f"# {run.round_key}",
                    "",
                    f"状态：{run.status}",
                    f"过期：{'是' if run.stale else '否'}",
                    f"执行器：{run.executor}",
                    "",
                    "## 输出",
                    "",
                    run.output,
                    "",
                ]
            )
        )
    if run_contents:
        bundle_writer(runs_fd, run_contents)
    return {"source": str(source_path), "note": str(note_path), "cards": str(cards_path)}


def _render_note(chapter: Chapter, blocks: list[NoteBlock]) -> str:
    lines = [f"<!-- chapter-id: {chapter.id} -->", f"# {chapter.title}", ""]
    for block in blocks:
        lines.extend([f"## {block.title}", ""])
        if block.kind.endswith("_mermaid"):
            lines.extend(["```mermaid", _pure_mermaid(block.body), "```", ""])
        else:
            lines.extend([block.body, ""])
    return "\n".join(lines)


def _pure_mermaid(body: str) -> str:
    match = MERMAID_FENCE_RE.match(body)
    return (match.group(1) if match else body).strip()


def _render_cards(chapter: Chapter, cards: list[Card]) -> str:
    lines = [f"# {chapter.title} 写作卡片", ""]
    for card in cards:
        lines.extend(
            [
                f"## {card.title}",
                "",
                f"类型：{card.kind}",
                f"收藏：{'是' if card.favorite else '否'}",
                "",
                card.body,
                "",
            ]
        )
    return "\n".join(lines)
