from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_io import (
    AtomicCommitError,
    atomic_replace_file,
    atomic_write_bytes,
    rename_exclusive,
    sync_file_data,
)


class PageCacheError(RuntimeError):
    pass


_MIB = 1024 * 1024
_GIB = 1024 * _MIB


@dataclass(frozen=True)
class CacheLimits:
    max_source_file_bytes: int = 2 * _GIB
    max_source_total_bytes: int = 2 * _GIB
    max_page_image_bytes: int = 64 * _MIB
    max_page_metadata_bytes: int = 512 * 1024
    max_pages_total_bytes: int = 2 * _GIB
    max_job_file_bytes: int = 64 * _MIB
    max_job_bytes: int = 256 * _MIB
    max_jobs_total_bytes: int = 512 * _MIB
    max_page_cache_bytes: int = 4 * _GIB
    max_page_cache_entries: int = 100_000
    max_codex_result_bytes: int = 2 * _MIB
    max_codex_total_bytes: int = 512 * _MIB
    max_codex_cache_entries: int = 4096
    max_scan_entries: int = 100_000
    quota_maintenance_interval: int = 32
    snapshot_index_max_entries: int = 256

    def __post_init__(self) -> None:
        for value in self.__dict__.values():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("OCR cache limits are invalid")


@dataclass
class _CacheUsage:
    source: int = 0
    pages: int = 0
    jobs: int = 0
    entries: int = 0

    def total(self) -> int:
        return self.source + self.pages + self.jobs


@dataclass(frozen=True)
class _PersistedReservation:
    reservation_id: str
    slot: int
    category: str
    amount: int
    entries: int
    generation: int


@dataclass
class _QuotaState:
    committed: _CacheUsage
    reserved: _CacheUsage
    generation: int
    reservations: tuple[_PersistedReservation, ...]


@dataclass(frozen=True)
class _EvictionCandidate:
    category: str
    path: Path
    identity: tuple[int, int]
    size: int
    entries: int
    accessed_ns: int
    lock_key: str | None


@dataclass
class _QuotaReservation:
    category: str
    amount: int
    entries: int
    reservation_id: str
    slot: int
    generation: int
    lock_fd: int
    active: bool = True


@dataclass
class _ActiveJob:
    identity: tuple[int, int]
    reservation: _QuotaReservation
    lock_fd: int


def _write_file(fd: int, data: memoryview) -> int:
    return os.write(fd, data)


def _fsync_file(fd: int) -> None:
    sync_file_data(fd)


def _close_file(fd: int) -> None:
    os.close(fd)


def _replace_file(source: Path, target: Path, directory_fd: int) -> None:
    if source.parent != target.parent:
        raise ValueError("atomic source and target must share a directory")
    if target.suffix == ".image":
        rename_exclusive(source, target, directory_fd)
        return
    os.replace(
        source.name,
        target.name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _publish_file_exclusive(source: Path, target: Path, directory_fd: int) -> None:
    rename_exclusive(source, target, directory_fd)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _sync_directory(fd: int) -> None:
    os.fsync(fd)


def _unlink_temporary(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclass(frozen=True)
class CacheInputs:
    pdf_sha256: str
    page: int
    dpi: int
    helper_version: str
    language_config: tuple[str, ...]


@dataclass(frozen=True)
class CachedPagePayload:
    cache_key: str
    pdf_sha256: str
    page: int
    dpi: int
    helper_version: str
    language_config: tuple[str, ...]
    image_path: str
    image_sha256: str
    width: int
    height: int
    supported_languages: tuple[str, ...]
    observations: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SourceSnapshot:
    pdf_sha256: str
    path: Path
    identity: tuple[int, int]
    fingerprint: tuple[int, int, int]


@dataclass
class _ThreadLockEntry:
    lock: threading.Lock
    refcount: int = 0


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[tuple[tuple[int, int], int], _ThreadLockEntry] = {}
_THREAD_LOCKS_PID = os.getpid()
_THREAD_LOCK_STATE = threading.local()

_MAX_CACHE_META_BYTES = 512 * 1024
_MAX_OBSERVATION_COUNT = 256
_MAX_OBSERVATION_METADATA_BYTES = 512 * 1024
_MAX_TEXT_LENGTH = 4096
_MAX_CANDIDATES = 5
_MAX_LANGUAGE_COUNT = 128
_MAX_LANGUAGE_LENGTH = 64
_LOCK_POLL_SECONDS = 0.1
_QUOTA_LOCK_KEY = hashlib.sha256(b"page-cache-quota-v1").hexdigest()
_QUOTA_USAGE_NAME = ".quota-usage.json"
_QUOTA_USAGE_MAX_BYTES = 32 * 1024
_PAGE_CACHE_LOCK_SLOTS = 64
_ENTRY_LOCK_SLOTS = 32
_CANDIDATE_LOCK_SLOT_START = _ENTRY_LOCK_SLOTS
_CANDIDATE_LOCK_SLOTS = 16
_RESERVATION_SLOT_START = _CANDIDATE_LOCK_SLOT_START + _CANDIDATE_LOCK_SLOTS
_RESERVATION_SLOT_STOP = _PAGE_CACHE_LOCK_SLOTS - 1
_QUOTA_LOCK_SLOT = _PAGE_CACHE_LOCK_SLOTS - 1
_MAX_RESERVATIONS = _RESERVATION_SLOT_STOP - _RESERVATION_SLOT_START


def _reset_page_cache_locks_after_fork() -> None:
    global _THREAD_LOCKS_GUARD, _THREAD_LOCKS, _THREAD_LOCKS_PID, _THREAD_LOCK_STATE
    _THREAD_LOCKS_GUARD = threading.Lock()
    _THREAD_LOCKS = {}
    _THREAD_LOCKS_PID = os.getpid()
    _THREAD_LOCK_STATE = threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_page_cache_locks_after_fork)


def _lock_slot_name(slot: int) -> str:
    if not 0 <= slot < _PAGE_CACHE_LOCK_SLOTS:
        raise PageCacheError("cache lock is not available")
    return f"slot-{slot:02d}.lock"


def _entry_lock_slot(cache_key: str) -> int:
    encoded = str(cache_key).encode("utf-8", errors="strict")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") % _ENTRY_LOCK_SLOTS


def _candidate_lock_slot(cache_key: str) -> int:
    encoded = str(cache_key).encode("utf-8", errors="strict")
    offset = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")
    return _CANDIDATE_LOCK_SLOT_START + (offset % _CANDIDATE_LOCK_SLOTS)


def _lock_slot(cache_key: str) -> int:
    return _QUOTA_LOCK_SLOT if cache_key == _QUOTA_LOCK_KEY else _entry_lock_slot(cache_key)


def canonical_language_config(languages: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(languages, (list, tuple)) or len(languages) > _MAX_LANGUAGE_COUNT:
        raise PageCacheError("invalid language configuration")
    normalized = []
    for language in languages:
        if not isinstance(language, str) or len(language) > _MAX_LANGUAGE_LENGTH:
            raise PageCacheError("invalid language configuration")
        value = language.strip()
        if not value:
            raise PageCacheError("invalid language configuration")
        normalized.append(value)
    return tuple(sorted(dict.fromkeys(normalized)))


def cache_key_for(inputs: CacheInputs) -> str:
    payload = {
        "dpi": inputs.dpi,
        "helper_version": inputs.helper_version,
        "language_config": list(inputs.language_config),
        "page": inputs.page,
        "pdf_sha256": inputs.pdf_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PageCache:
    def __init__(self, root: Path | str, *, limits: CacheLimits | None = None):
        self._owner_pid = os.getpid()
        self.limits = limits or CacheLimits()
        requested_root = Path(root).expanduser()
        if not requested_root.is_absolute():
            requested_root = Path.cwd() / requested_root
        requested_root = _normalize_safe_system_path(requested_root)
        if ".." in requested_root.parts:
            raise PageCacheError("cache directory is not available")
        self.root = Path(os.path.normpath(os.fspath(requested_root)))
        if not self.root.is_absolute():
            raise PageCacheError("cache directory is not available")
        self._root_identity = self._prepare_root(self.root)
        self.pages_dir = self.root / "pages"
        self.locks_dir = self.root / "locks"
        self.jobs_root = self.root / "jobs"
        self.source_snapshots_dir = self.root / "source_snapshots"
        for directory_name in ("pages", "locks", "jobs", "source_snapshots"):
            _ensure_directory_chain(
                self.root,
                (directory_name,),
                expected_base=self._root_identity,
            )
            _chmod_directory(
                self.root,
                (directory_name,),
                expected_base=self._root_identity,
            )
        self._prepare_lock_slots()
        self._active_jobs_guard = threading.Lock()
        self._active_jobs: dict[Path, _ActiveJob] = {}
        self._initialize_quota_usage()

    @staticmethod
    def _prepare_root(root: Path) -> tuple[int, int]:
        return _prepare_private_root(root)

    @staticmethod
    def _assert_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PageCacheError("cache directory is not available")

    def _assert_root_current(self) -> None:
        self._assert_owner_process()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(self.root, flags)
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        try:
            info = os.fstat(fd)
            if (
                (info.st_dev, info.st_ino) != self._root_identity
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise PageCacheError("cache directory is not available")
        finally:
            os.close(fd)

    def _assert_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise PageCacheError("page cache cannot be reused after fork")

    def _prepare_lock_slots(self) -> None:
        for slot in range(_PAGE_CACHE_LOCK_SLOTS):
            fd = -1
            try:
                fd = os.open(
                    self.locks_dir / _lock_slot_name(slot),
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != 0
                ):
                    raise PageCacheError("cache lock is not available")
            except OSError as exc:
                raise PageCacheError("cache lock is not available") from exc
            finally:
                if fd >= 0:
                    os.close(fd)

    def _initialize_quota_usage(self) -> None:
        with self.lock(_QUOTA_LOCK_KEY):
            previous = self._read_usage_file_locked(missing_ok=True)
            if previous is not None:
                previous = self._recover_reservations_locked(previous)
            committed, _candidates = self._scan_cache_usage(collect_candidates=False)
            state = _QuotaState(
                committed=committed,
                reserved=previous.reserved if previous is not None else _CacheUsage(),
                generation=(previous.generation + 1) if previous is not None else 1,
                reservations=previous.reservations if previous is not None else (),
            )
            self._write_usage_locked(state)

    def _read_usage_locked(self) -> _QuotaState:
        state = self._read_usage_file_locked(missing_ok=False)
        assert state is not None
        return state

    def _read_usage_file_locked(self, *, missing_ok: bool) -> _QuotaState | None:
        path = self.root / _QUOTA_USAGE_NAME
        try:
            fd = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise PageCacheError("OCR cache capacity metadata is invalid") from None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or not 0 < info.st_size <= _QUOTA_USAGE_MAX_BYTES
            ):
                raise PageCacheError("OCR cache capacity metadata is invalid")
            raw = os.pread(fd, _QUOTA_USAGE_MAX_BYTES + 1, 0)
        finally:
            os.close(fd)
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except Exception as exc:
            raise PageCacheError("OCR cache capacity metadata is invalid") from exc
        if not isinstance(value, dict) or set(value) != {
            "version",
            "generation",
            "committed",
            "reserved",
            "reservations",
        }:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        if value.get("version") != 2:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        generation = value.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        committed = self._decode_usage(value.get("committed"))
        reserved = self._decode_usage(value.get("reserved"))
        raw_reservations = value.get("reservations")
        if not isinstance(raw_reservations, list) or len(raw_reservations) > _MAX_RESERVATIONS:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        reservations: list[_PersistedReservation] = []
        seen_ids: set[str] = set()
        seen_slots: set[int] = set()
        for item in raw_reservations:
            if not isinstance(item, list) or len(item) != 6:
                raise PageCacheError("OCR cache capacity metadata is invalid")
            reservation_id, slot, category, amount, entries, created_generation = item
            if (
                not isinstance(reservation_id, str)
                or len(reservation_id) != 32
                or any(character not in "0123456789abcdef" for character in reservation_id)
                or reservation_id in seen_ids
                or not isinstance(slot, int)
                or isinstance(slot, bool)
                or not _RESERVATION_SLOT_START <= slot < _RESERVATION_SLOT_STOP
                or slot in seen_slots
                or category not in {"source", "pages", "jobs"}
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount < 0
                or not isinstance(entries, int)
                or isinstance(entries, bool)
                or entries < 0
                or not isinstance(created_generation, int)
                or isinstance(created_generation, bool)
                or not 1 <= created_generation <= generation
            ):
                raise PageCacheError("OCR cache capacity metadata is invalid")
            seen_ids.add(reservation_id)
            seen_slots.add(slot)
            reservations.append(
                _PersistedReservation(
                    reservation_id,
                    slot,
                    category,
                    amount,
                    entries,
                    created_generation,
                )
            )
        expected_reserved = _CacheUsage()
        for reservation in reservations:
            setattr(
                expected_reserved,
                reservation.category,
                getattr(expected_reserved, reservation.category) + reservation.amount,
            )
            expected_reserved.entries += reservation.entries
        if expected_reserved != reserved:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        return _QuotaState(
            committed=committed,
            reserved=reserved,
            generation=generation,
            reservations=tuple(reservations),
        )

    @staticmethod
    def _decode_usage(value: object) -> _CacheUsage:
        if not isinstance(value, dict) or set(value) != {"source", "pages", "jobs", "entries"}:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        source = value.get("source")
        pages = value.get("pages")
        jobs = value.get("jobs")
        entries = value.get("entries")
        amounts = (source, pages, jobs, entries)
        if any(
            not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
            for amount in amounts
        ):
            raise PageCacheError("OCR cache capacity metadata is invalid")
        assert isinstance(source, int)
        assert isinstance(pages, int)
        assert isinstance(jobs, int)
        assert isinstance(entries, int)
        return _CacheUsage(
            source=source,
            pages=pages,
            jobs=jobs,
            entries=entries,
        )

    def _write_usage_locked(self, state: _QuotaState) -> None:
        def encode_usage(usage: _CacheUsage) -> dict[str, int]:
            return {
                "source": usage.source,
                "pages": usage.pages,
                "jobs": usage.jobs,
                "entries": usage.entries,
            }

        encoded = json.dumps(
            {
                "version": 2,
                "generation": state.generation,
                "committed": encode_usage(state.committed),
                "reserved": encode_usage(state.reserved),
                "reservations": [
                    [
                        reservation.reservation_id,
                        reservation.slot,
                        reservation.category,
                        reservation.amount,
                        reservation.entries,
                        reservation.generation,
                    ]
                    for reservation in state.reservations
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _QUOTA_USAGE_MAX_BYTES:
            raise PageCacheError("OCR cache capacity metadata is invalid")
        atomic_write_bytes(
            target=self.root / _QUOTA_USAGE_NAME,
            data=encoded,
            temporary_prefix=".quota-usage.",
            writer=lambda fd, data: os.write(fd, data),
            sync_file=os.fsync,
            close_file=os.close,
            open_directory=_open_directory,
            replace_file=_replace_file,
            sync_directory=os.fsync,
        )

    def _recover_reservations_locked(self, state: _QuotaState) -> _QuotaState:
        retained: list[_PersistedReservation] = []
        for reservation in state.reservations:
            fd = -1
            acquired = False
            try:
                fd = self._open_lock_slot(reservation.slot)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    retained.append(reservation)
            except OSError:
                retained.append(reservation)
            finally:
                if fd >= 0:
                    if acquired:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    os.close(fd)
        if len(retained) == len(state.reservations):
            return state
        reserved = _CacheUsage()
        for reservation in retained:
            setattr(
                reserved,
                reservation.category,
                getattr(reserved, reservation.category) + reservation.amount,
            )
            reserved.entries += reservation.entries
        return _QuotaState(
            committed=state.committed,
            reserved=reserved,
            generation=state.generation + 1,
            reservations=tuple(retained),
        )

    def _open_lock_slot(self, slot: int) -> int:
        fd = os.open(
            self.locks_dir / _lock_slot_name(slot),
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != 0
        ):
            os.close(fd)
            raise OSError
        return fd

    def _acquire_reservation_slot_locked(self, state: _QuotaState) -> tuple[int, int]:
        used = {reservation.slot for reservation in state.reservations}
        for slot in range(_RESERVATION_SLOT_START, _RESERVATION_SLOT_STOP):
            if slot in used:
                continue
            fd = -1
            try:
                fd = self._open_lock_slot(slot)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return slot, fd
            except (BlockingIOError, OSError):
                if fd >= 0:
                    os.close(fd)
        raise PageCacheError("OCR cache capacity exceeded")

    def _scan_cache_usage(
        self,
        *,
        collect_candidates: bool,
        excluded_paths: tuple[Path, ...] = (),
    ) -> tuple[_CacheUsage, list[_EvictionCandidate]]:
        usage = _CacheUsage()
        candidates: list[_EvictionCandidate] = []
        scanned = [0]
        excluded = {path.absolute() for path in excluded_paths}

        def count_entry() -> None:
            scanned[0] += 1
            if scanned[0] > self.limits.max_scan_entries:
                raise PageCacheError("OCR cache scan limit exceeded")

        def tree_size(root: Path) -> tuple[int, int]:
            total = 0
            total_entries = 0
            stack = [root]
            while stack:
                directory = stack.pop()
                try:
                    iterator = os.scandir(directory)
                except OSError:
                    continue
                with iterator:
                    for entry in iterator:
                        count_entry()
                        path = Path(entry.path)
                        if path.absolute() in excluded:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        total_entries += 1
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        elif stat.S_ISDIR(info.st_mode):
                            stack.append(path)
            return total, total_entries

        try:
            source_entries = os.scandir(self.source_snapshots_dir)
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        with source_entries:
            for entry in source_entries:
                count_entry()
                path = Path(entry.path)
                if path.absolute() in excluded:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                usage.entries += 1
                size = info.st_size if stat.S_ISREG(info.st_mode) else 0
                usage.source += size
                if collect_candidates and stat.S_ISREG(info.st_mode):
                    lock_key = None
                    if entry.name.endswith(".pdf") and _is_sha256(entry.name[:-4]):
                        lock_key = hashlib.sha256(
                            f"source-content:{entry.name[:-4]}".encode()
                        ).hexdigest()
                    candidates.append(
                        _EvictionCandidate(
                            "source",
                            path,
                            (info.st_dev, info.st_ino),
                            size,
                            1,
                            max(info.st_atime_ns, info.st_mtime_ns),
                            lock_key,
                        )
                    )

        try:
            prefix_entries = os.scandir(self.pages_dir)
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        with prefix_entries:
            for prefix in prefix_entries:
                count_entry()
                prefix_path = Path(prefix.path)
                if prefix_path.absolute() in excluded:
                    continue
                try:
                    prefix_info = prefix.stat(follow_symlinks=False)
                except OSError:
                    continue
                usage.entries += 1
                if not stat.S_ISDIR(prefix_info.st_mode):
                    if stat.S_ISREG(prefix_info.st_mode):
                        usage.pages += prefix_info.st_size
                    continue
                try:
                    entry_dirs = os.scandir(prefix.path)
                except OSError:
                    continue
                with entry_dirs:
                    for entry in entry_dirs:
                        count_entry()
                        path = Path(entry.path)
                        if path.absolute() in excluded:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        usage.entries += 1
                        if stat.S_ISDIR(info.st_mode):
                            size, child_entries = tree_size(path)
                            usage.pages += size
                            usage.entries += child_entries
                            if collect_candidates:
                                lock_key = entry.name if _is_sha256(entry.name) else None
                                candidates.append(
                                    _EvictionCandidate(
                                        "pages",
                                        path,
                                        (info.st_dev, info.st_ino),
                                        size,
                                        1 + child_entries,
                                        max(info.st_atime_ns, info.st_mtime_ns),
                                        lock_key,
                                    )
                                )
                        elif stat.S_ISREG(info.st_mode):
                            usage.pages += info.st_size

        try:
            job_entries = os.scandir(self.jobs_root)
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        with job_entries:
            for entry in job_entries:
                count_entry()
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                usage.entries += 1
                if stat.S_ISDIR(info.st_mode):
                    size, child_entries = tree_size(path)
                    usage.jobs += size
                    usage.entries += child_entries
                    if collect_candidates:
                        candidates.append(
                            _EvictionCandidate(
                                "jobs",
                                path,
                                (info.st_dev, info.st_ino),
                                size,
                                1 + child_entries,
                                max(info.st_atime_ns, info.st_mtime_ns),
                                _job_lock_key(entry.name),
                            )
                        )
                elif stat.S_ISREG(info.st_mode):
                    usage.jobs += info.st_size
        return usage, candidates

    def _reserve_capacity(
        self,
        category: str,
        amount: int,
        *,
        entries: int = 1,
        protected_paths: tuple[Path, ...] = (),
    ) -> _QuotaReservation:
        if (
            category not in {"source", "pages", "jobs"}
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount < 0
            or not isinstance(entries, int)
            or isinstance(entries, bool)
            or entries < 0
        ):
            raise PageCacheError("OCR cache capacity exceeded")
        reservation_fd = -1
        with self.lock(_QUOTA_LOCK_KEY):
            state = self._read_usage_locked()
            if (
                self._capacity_exceeded(state, category, amount, entries)
                or len(state.reservations) >= _MAX_RESERVATIONS
            ):
                state = self._recover_reservations_locked(state)
            if self._capacity_exceeded(state, category, amount, entries):
                committed, candidates = self._scan_cache_usage(
                    collect_candidates=True,
                    excluded_paths=protected_paths,
                )
                state = _QuotaState(
                    committed=committed,
                    reserved=state.reserved,
                    generation=state.generation + 1,
                    reservations=state.reservations,
                )
                try:
                    self._evict_until_fit(
                        state,
                        candidates,
                        category,
                        amount,
                        entries,
                        protected_paths=protected_paths,
                    )
                except PageCacheError:
                    self._write_usage_locked(state)
                    raise
            if len(state.reservations) >= _MAX_RESERVATIONS:
                self._write_usage_locked(state)
                raise PageCacheError("OCR cache capacity exceeded")
            slot, reservation_fd = self._acquire_reservation_slot_locked(state)
            reservation_id = uuid.uuid4().hex
            generation = state.generation + 1
            persisted = _PersistedReservation(
                reservation_id,
                slot,
                category,
                amount,
                entries,
                generation,
            )
            reserved = _CacheUsage(
                source=state.reserved.source,
                pages=state.reserved.pages,
                jobs=state.reserved.jobs,
                entries=state.reserved.entries,
            )
            setattr(reserved, category, getattr(reserved, category) + amount)
            reserved.entries += entries
            updated = _QuotaState(
                committed=state.committed,
                reserved=reserved,
                generation=generation,
                reservations=state.reservations + (persisted,),
            )
            try:
                self._write_usage_locked(updated)
            except BaseException:
                try:
                    fcntl.flock(reservation_fd, fcntl.LOCK_UN)
                finally:
                    os.close(reservation_fd)
                reservation_fd = -1
                raise
        return _QuotaReservation(
            category=category,
            amount=amount,
            entries=entries,
            reservation_id=reservation_id,
            slot=slot,
            generation=generation,
            lock_fd=reservation_fd,
        )

    def _release_capacity(self, reservation: _QuotaReservation) -> None:
        self._finish_capacity(reservation, commit=False)

    def _commit_capacity(self, reservation: _QuotaReservation) -> None:
        self._finish_capacity(reservation, commit=True)

    def _finish_capacity(self, reservation: _QuotaReservation, *, commit: bool) -> None:
        if not reservation.active:
            return
        with self.lock(_QUOTA_LOCK_KEY):
            state = self._read_usage_locked()
            match = next(
                (
                    item
                    for item in state.reservations
                    if item.reservation_id == reservation.reservation_id
                    and item.slot == reservation.slot
                    and item.category == reservation.category
                    and item.amount == reservation.amount
                    and item.entries == reservation.entries
                    and item.generation == reservation.generation
                ),
                None,
            )
            if match is not None:
                reserved = _CacheUsage(
                    source=state.reserved.source,
                    pages=state.reserved.pages,
                    jobs=state.reserved.jobs,
                    entries=state.reserved.entries,
                )
                setattr(
                    reserved,
                    reservation.category,
                    max(0, getattr(reserved, reservation.category) - reservation.amount),
                )
                reserved.entries = max(0, reserved.entries - reservation.entries)
                committed = _CacheUsage(
                    source=state.committed.source,
                    pages=state.committed.pages,
                    jobs=state.committed.jobs,
                    entries=state.committed.entries,
                )
                if commit:
                    setattr(
                        committed,
                        reservation.category,
                        getattr(committed, reservation.category) + reservation.amount,
                    )
                    committed.entries += reservation.entries
                updated = _QuotaState(
                    committed=committed,
                    reserved=reserved,
                    generation=state.generation + 1,
                    reservations=tuple(item for item in state.reservations if item is not match),
                )
                self._write_usage_locked(updated)
        self._close_reservation(reservation)

    @staticmethod
    def _close_reservation(reservation: _QuotaReservation) -> None:
        fd = reservation.lock_fd
        reservation.lock_fd = -1
        reservation.active = False
        if fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _capacity_exceeded(
        self,
        state: _QuotaState,
        category: str,
        incoming: int,
        incoming_entries: int,
    ) -> bool:
        category_limit = {
            "source": self.limits.max_source_total_bytes,
            "pages": self.limits.max_pages_total_bytes,
            "jobs": self.limits.max_jobs_total_bytes,
        }[category]
        return (
            getattr(state.committed, category) + getattr(state.reserved, category) + incoming
            > category_limit
            or state.committed.total() + state.reserved.total() + incoming
            > self.limits.max_page_cache_bytes
            or state.committed.entries + state.reserved.entries + incoming_entries
            > self.limits.max_page_cache_entries
        )

    def _evict_until_fit(
        self,
        state: _QuotaState,
        candidates: list[_EvictionCandidate],
        category: str,
        incoming: int,
        incoming_entries: int,
        *,
        protected_paths: tuple[Path, ...],
    ) -> None:
        ordered = sorted(
            candidates,
            key=lambda candidate: (candidate.accessed_ns, str(candidate.path)),
        )
        attempted: set[Path] = set()
        while self._capacity_exceeded(state, category, incoming, incoming_entries):
            category_over = (
                getattr(state.committed, category) + getattr(state.reserved, category) + incoming
                > {
                    "source": self.limits.max_source_total_bytes,
                    "pages": self.limits.max_pages_total_bytes,
                    "jobs": self.limits.max_jobs_total_bytes,
                }[category]
            )
            removed = False
            for candidate in ordered:
                if candidate.path in attempted or (
                    category_over and candidate.category != category
                ):
                    continue
                attempted.add(candidate.path)
                if any(
                    protected == candidate.path or protected.is_relative_to(candidate.path)
                    for protected in protected_paths
                ):
                    continue
                lock_fd = self._try_candidate_lock(
                    candidate.lock_key,
                    coordinate_entry=candidate.category == "pages",
                )
                if lock_fd is None:
                    continue
                try:
                    if not self._delete_candidate(candidate):
                        continue
                finally:
                    if lock_fd >= 0:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        finally:
                            os.close(lock_fd)
                setattr(
                    state.committed,
                    candidate.category,
                    max(
                        0,
                        getattr(state.committed, candidate.category) - candidate.size,
                    ),
                )
                state.committed.entries = max(0, state.committed.entries - candidate.entries)
                removed = True
                break
            if not removed:
                raise PageCacheError("OCR cache capacity exceeded")

    def _try_candidate_lock(
        self,
        lock_key: str | None,
        *,
        coordinate_entry: bool = False,
    ) -> int | None:
        if lock_key is None:
            return -1
        try:
            slot = (
                _entry_lock_slot(lock_key) if coordinate_entry else _candidate_lock_slot(lock_key)
            )
            fd = self._open_lock_slot(slot)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            return fd
        except OSError:
            return None

    def _delete_candidate(self, candidate: _EvictionCandidate) -> bool:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(candidate.path.parent, flags)
        except OSError:
            return False
        quarantine_name = f".{candidate.path.name}.evict-{uuid.uuid4().hex}"
        try:
            try:
                current = os.stat(
                    candidate.path.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return True
            if (current.st_dev, current.st_ino) != candidate.identity:
                return False
            try:
                rename_exclusive(
                    candidate.path,
                    candidate.path.with_name(quarantine_name),
                    directory_fd,
                )
            except OSError:
                return False
            moved = os.stat(quarantine_name, dir_fd=directory_fd, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != candidate.identity:
                try:
                    rename_exclusive(
                        candidate.path.with_name(quarantine_name),
                        candidate.path,
                        directory_fd,
                    )
                except OSError:
                    pass
                return False
            moved_path = candidate.path.with_name(quarantine_name)
            try:
                if stat.S_ISDIR(moved.st_mode):
                    shutil.rmtree(moved_path)
                else:
                    os.unlink(quarantine_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                return False
            return True
        finally:
            os.close(directory_fd)

    def entry_dir(self, cache_key: str) -> Path:
        return self.pages_dir / cache_key[:2] / cache_key

    @contextmanager
    def lock(
        self,
        cache_key: str,
        *,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> Iterator[None]:
        self._assert_root_current()
        if _THREAD_LOCKS_PID != os.getpid():
            raise PageCacheError("page cache lock state is invalid")
        slot = _lock_slot(cache_key)
        thread_key = (self._root_identity, slot)
        held_locks = getattr(_THREAD_LOCK_STATE, "held_locks", None)
        if held_locks is None:
            held_locks = {}
            _THREAD_LOCK_STATE.held_locks = held_locks
        nested_depth = held_locks.get(thread_key, 0)
        if nested_depth:
            held_locks[thread_key] = nested_depth + 1
            try:
                yield
            finally:
                remaining = held_locks[thread_key] - 1
                if remaining:
                    held_locks[thread_key] = remaining
                else:
                    del held_locks[thread_key]
            return
        with _THREAD_LOCKS_GUARD:
            entry = _THREAD_LOCKS.get(thread_key)
            if entry is None:
                entry = _ThreadLockEntry(threading.Lock())
                _THREAD_LOCKS[thread_key] = entry
            entry.refcount += 1
        try:
            thread_lock = entry.lock
            thread_acquired = False
            file_locked = False
            legacy_locked = False
            fd = None
            legacy_fd = None
            try:
                while not thread_acquired:
                    _check_control(deadline, cancel_event)
                    thread_acquired = thread_lock.acquire(timeout=_control_poll_timeout(deadline))
                _check_control(deadline, cancel_event)
                _ensure_directory_chain(
                    self.root,
                    ("locks",),
                    expected_base=self._root_identity,
                )
                fd = self._open_lock_slot(slot)
                try:
                    while not file_locked:
                        _check_control(deadline, cancel_event)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            file_locked = True
                        except OSError as exc:
                            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                                raise
                            time.sleep(_control_poll_timeout(deadline))
                    legacy_path = self.locks_dir / f"{cache_key}.lock"
                    try:
                        legacy_fd = os.open(
                            legacy_path,
                            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        )
                    except FileNotFoundError:
                        legacy_fd = None
                    if legacy_fd is not None:
                        legacy_info = os.fstat(legacy_fd)
                        if (
                            not stat.S_ISREG(legacy_info.st_mode)
                            or legacy_info.st_nlink != 1
                            or legacy_info.st_uid != os.geteuid()
                            or stat.S_IMODE(legacy_info.st_mode) & 0o077
                        ):
                            raise PageCacheError("cache lock is not available")
                        while not legacy_locked:
                            _check_control(deadline, cancel_event)
                            try:
                                fcntl.flock(legacy_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                legacy_locked = True
                            except OSError as exc:
                                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                                    raise
                                time.sleep(_control_poll_timeout(deadline))
                    _check_control(deadline, cancel_event)
                    held_locks[thread_key] = 1
                    try:
                        yield
                    finally:
                        del held_locks[thread_key]
                finally:
                    if legacy_fd is not None:
                        try:
                            if legacy_locked:
                                fcntl.flock(legacy_fd, fcntl.LOCK_UN)
                        finally:
                            os.close(legacy_fd)
                    if fd is not None:
                        try:
                            if file_locked:
                                fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)
            finally:
                if thread_acquired:
                    thread_lock.release()
        finally:
            with _THREAD_LOCKS_GUARD:
                current = _THREAD_LOCKS.get(thread_key)
                if current is entry:
                    entry.refcount -= 1
                    if entry.refcount == 0:
                        del _THREAD_LOCKS[thread_key]

    def publish_source_snapshot(
        self,
        source_fd: int,
        *,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> SourceSnapshot:
        _check_control(deadline, cancel_event)
        self._assert_root_current()
        _assert_directory_chain(self.source_snapshots_dir)
        temporary = self.source_snapshots_dir / f".snapshot-{uuid.uuid4().hex}.tmp"
        reservation: _QuotaReservation | None = None
        try:
            source_before = os.fstat(source_fd)
            if (
                not stat.S_ISREG(source_before.st_mode)
                or source_before.st_size <= 0
                or source_before.st_size > self.limits.max_source_file_bytes
            ):
                raise PageCacheError("OCR cache capacity exceeded")
            reservation = self._reserve_capacity(
                "source",
                source_before.st_size,
                protected_paths=(temporary,),
            )
            digest = _copy_source_snapshot_and_hash(
                source_fd,
                temporary,
                max_bytes=self.limits.max_source_file_bytes,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            source_after = os.fstat(source_fd)
            if _file_identity(source_before) != _file_identity(source_after):
                raise PageCacheError("source snapshot failed verification")
            _check_control(deadline, cancel_event)
            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fchmod(fd, 0o400)
                sync_file_data(fd)
            finally:
                os.close(fd)
            _check_control(deadline, cancel_event)

            pdf_sha256 = digest
            target = self.source_snapshots_dir / f"{pdf_sha256}.pdf"
            digest_lock = hashlib.sha256(f"source-content:{pdf_sha256}".encode()).hexdigest()
            with self.lock(
                digest_lock,
                deadline=deadline,
                cancel_event=cancel_event,
            ):
                for _attempt in range(2):
                    _check_control(deadline, cancel_event)
                    linked_new_target = False
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        pass
                    else:
                        linked_new_target = True
                        assert reservation is not None
                        self._commit_capacity(reservation)
                        try:
                            temporary.unlink()
                            _fsync_directory(self.source_snapshots_dir)
                        except BaseException as exc:
                            try:
                                temporary.unlink(missing_ok=True)
                            except BaseException:
                                pass
                            raise AtomicCommitError(
                                target,
                                durability_uncertain=True,
                            ) from exc
                    candidate_identity = _path_entry_identity(target)
                    try:
                        snapshot = self.validate_source_snapshot(
                            target,
                            pdf_sha256,
                            verify_hash=True,
                            deadline=deadline,
                            cancel_event=cancel_event,
                        )
                    except (InterruptedError, TimeoutError):
                        raise
                    except Exception:
                        quarantined = _quarantine_named_entry(
                            self.source_snapshots_dir,
                            target.name,
                            expected_identity=candidate_identity,
                            valid_sha256=pdf_sha256,
                        )
                        if linked_new_target:
                            raise
                        if not quarantined:
                            continue
                        continue
                    if not linked_new_target:
                        temporary.unlink()
                        _fsync_directory(self.source_snapshots_dir)
                        assert reservation is not None
                        self._release_capacity(reservation)
                    _check_control(deadline, cancel_event)
                    return snapshot
            raise PageCacheError("source snapshot failed verification")
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except BaseException:
                pass
            if reservation is not None and reservation.active:
                self._release_capacity(reservation)
            raise

    def validate_source_snapshot(
        self,
        snapshot: SourceSnapshot | Path,
        expected_sha256: str | None = None,
        *,
        verify_hash: bool,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> SourceSnapshot:
        with self.open_source_snapshot(
            snapshot,
            expected_sha256,
            verify_hash=verify_hash,
            deadline=deadline,
            cancel_event=cancel_event,
        ) as (validated, _fd):
            return validated

    @contextmanager
    def open_source_snapshot(
        self,
        snapshot: SourceSnapshot | Path,
        expected_sha256: str | None = None,
        *,
        verify_hash: bool,
        record_access: bool = True,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> Iterator[tuple[SourceSnapshot, int]]:
        _check_control(deadline, cancel_event)
        self._assert_root_current()
        if isinstance(snapshot, SourceSnapshot):
            path = snapshot.path
            pdf_sha256 = snapshot.pdf_sha256
            expected_identity = snapshot.identity
            expected_fingerprint = snapshot.fingerprint
        else:
            path = snapshot
            if expected_sha256 is None:
                raise PageCacheError("source snapshot failed verification")
            pdf_sha256 = expected_sha256
            expected_identity = None
            expected_fingerprint = None
        if not _is_sha256(pdf_sha256) or path.name != f"{pdf_sha256}.pdf":
            raise PageCacheError("source snapshot failed verification")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(fd)
            current = os.stat(path, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            fingerprint = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            if identity != (current.st_dev, current.st_ino):
                raise PageCacheError("source snapshot failed verification")
            if expected_identity is not None and identity != expected_identity:
                raise PageCacheError("source snapshot failed verification")
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
            ):
                raise PageCacheError("source snapshot failed verification")
            if stat.S_IMODE(opened.st_mode) & 0o222:
                raise PageCacheError("source snapshot failed verification")
            if (
                verify_hash
                and (expected_fingerprint is None or fingerprint != expected_fingerprint)
                and _hash_fd(fd, deadline=deadline, cancel_event=cancel_event) != pdf_sha256
            ):
                raise PageCacheError("source snapshot failed verification")
            if record_access:
                os.utime(fd, None)
                refreshed = os.fstat(fd)
                fingerprint = (
                    refreshed.st_size,
                    refreshed.st_mtime_ns,
                    refreshed.st_ctime_ns,
                )
            os.lseek(fd, 0, os.SEEK_SET)
            _check_control(deadline, cancel_event)
            yield (
                SourceSnapshot(
                    pdf_sha256=pdf_sha256,
                    path=path,
                    identity=identity,
                    fingerprint=fingerprint,
                ),
                fd,
            )
        finally:
            os.close(fd)

    def make_job_dir(self, cache_key: str) -> tuple[str, Path, Path]:
        self._assert_root_current()
        job_root = self.jobs_root / f"job-{cache_key}-{uuid.uuid4().hex}"
        reservation = self._reserve_capacity(
            "jobs",
            self.limits.max_job_bytes,
            protected_paths=(job_root,),
        )
        lock_fd: int | None = None
        try:
            lock_fd = self._try_candidate_lock(_job_lock_key(job_root.name))
            if lock_fd is None or lock_fd < 0:
                raise PageCacheError("OCR cache capacity exceeded")
            _ensure_directory_chain(self.root, ("jobs", job_root.name))
            job_dir = job_root / "output"
            _ensure_directory_chain(job_root, ("output",))
            info = job_root.stat(follow_symlinks=False)
            with self._active_jobs_guard:
                self._active_jobs[job_root] = _ActiveJob(
                    identity=(info.st_dev, info.st_ino),
                    reservation=reservation,
                    lock_fd=lock_fd,
                )
            lock_fd = None
            return "output", job_dir, job_root
        except BaseException:
            if lock_fd is not None and lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            self._release_capacity(reservation)
            raise

    def cleanup_job_dir(self, job_dir: Path) -> None:
        self._assert_root_current()
        with self._active_jobs_guard:
            active = self._active_jobs.pop(job_dir, None)
        if active is None:
            raise PageCacheError("job cache cleanup is not available")
        deleted = False
        try:
            deleted = self._delete_candidate(
                _EvictionCandidate(
                    category="jobs",
                    path=job_dir,
                    identity=active.identity,
                    size=0,
                    entries=0,
                    accessed_ns=0,
                    lock_key=None,
                )
            )
            if not deleted:
                raise PageCacheError("job cache cleanup is not available")
        finally:
            try:
                fcntl.flock(active.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(active.lock_fd)
            if deleted:
                self._release_capacity(active.reservation)

    def validate_job_usage(self, job_root: Path) -> int:
        self._assert_root_current()
        total = 0
        scanned = 0
        stack = [job_root]
        while stack:
            directory = stack.pop()
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise PageCacheError("OCR cache capacity exceeded") from exc
            with iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > self.limits.max_scan_entries:
                        raise PageCacheError("OCR cache scan limit exceeded")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise PageCacheError("OCR cache capacity exceeded") from exc
                    if stat.S_ISREG(info.st_mode):
                        if info.st_size > self.limits.max_job_file_bytes:
                            raise PageCacheError("OCR cache capacity exceeded")
                        total += info.st_size
                        if total > self.limits.max_job_bytes:
                            raise PageCacheError("OCR cache capacity exceeded")
                    elif stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    else:
                        raise PageCacheError("OCR cache capacity exceeded")
        return total

    def load_valid(
        self,
        cache_key: str,
        inputs: CacheInputs,
        *,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> CachedPagePayload | None:
        self._assert_root_current()
        _check_control(deadline, cancel_event)
        entry_dir = self.entry_dir(cache_key)
        if not _directory_chain_is_safe(self.pages_dir, (cache_key[:2], cache_key)):
            return None
        meta_path = entry_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            payload = self._load_meta(meta_path, max_bytes=self.limits.max_page_metadata_bytes)
            self._validate_meta(payload, cache_key, inputs)
            image_path = entry_dir / payload["image_name"]
            image_hash = hash_verified_regular_file(
                image_path,
                max_bytes=self.limits.max_page_image_bytes,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            if image_hash != payload["image_sha256"]:
                raise PageCacheError("cache image failed verification")
            _check_control(deadline, cancel_event)
            _touch_directory(entry_dir)
            return _cached_payload_from_meta(payload, str(image_path))
        except (InterruptedError, TimeoutError):
            raise
        except Exception as exc:
            self.quarantine(entry_dir)
            if isinstance(exc, PageCacheError):
                return None
            return None

    def publish(
        self,
        *,
        cache_key: str,
        inputs: CacheInputs,
        image_bytes_path: Path,
        image_sha256: str,
        width: int,
        height: int,
        supported_languages: tuple[str, ...],
        observations: tuple[dict[str, Any], ...],
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> CachedPagePayload:
        self._assert_root_current()
        _check_control(deadline, cancel_event)
        entry_dir = self.entry_dir(cache_key)
        _ensure_directory_chain(self.pages_dir, (cache_key[:2], cache_key))
        self._assert_directory(entry_dir)
        image_name = f"{image_sha256}.image"
        image_target = entry_dir / image_name
        meta = {
            "schema_version": 1,
            "cache_key": cache_key,
            "pdf_sha256": inputs.pdf_sha256,
            "page": inputs.page,
            "dpi": inputs.dpi,
            "helper_version": inputs.helper_version,
            "language_config": list(inputs.language_config),
            "image_name": image_name,
            "image_sha256": image_sha256,
            "width": width,
            "height": height,
            "supported_languages": list(supported_languages),
            "observations": list(observations),
        }
        encoded_meta = json.dumps(
            meta,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded_meta) > self.limits.max_page_metadata_bytes:
            image_bytes_path.unlink(missing_ok=True)
            raise PageCacheError("OCR cache capacity exceeded")
        try:
            image_fd = os.open(
                image_bytes_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                image_info = os.fstat(image_fd)
                if (
                    not stat.S_ISREG(image_info.st_mode)
                    or image_info.st_nlink != 1
                    or image_info.st_uid != os.geteuid()
                    or image_info.st_size <= 0
                    or image_info.st_size > self.limits.max_page_image_bytes
                ):
                    raise PageCacheError("OCR cache capacity exceeded")
            finally:
                os.close(image_fd)
        except BaseException:
            image_bytes_path.unlink(missing_ok=True)
            raise
        try:
            reservation = self._reserve_capacity(
                "pages",
                image_info.st_size + len(encoded_meta),
                protected_paths=(entry_dir, image_bytes_path),
            )
        except BaseException:
            image_bytes_path.unlink(missing_ok=True)
            raise
        published_image = False
        try:
            try:
                candidate_identity = _path_entry_identity(image_target)
                existing_hash = hash_verified_regular_file(
                    image_target,
                    max_bytes=self.limits.max_page_image_bytes,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            except (InterruptedError, TimeoutError):
                raise
            except (OSError, PageCacheError):
                _quarantine_named_entry(
                    entry_dir,
                    image_name,
                    expected_identity=candidate_identity,
                    valid_sha256=image_sha256,
                )
                existing_hash = None
            if existing_hash is not None and existing_hash != image_sha256:
                quarantined = _quarantine_named_entry(
                    entry_dir,
                    image_name,
                    expected_identity=candidate_identity,
                    valid_sha256=image_sha256,
                )
                if quarantined:
                    existing_hash = None
                else:
                    try:
                        existing_hash = hash_verified_regular_file(
                            image_target,
                            max_bytes=self.limits.max_page_image_bytes,
                            deadline=deadline,
                            cancel_event=cancel_event,
                        )
                    except (OSError, PageCacheError):
                        existing_hash = None
            if existing_hash != image_sha256:
                _check_control(deadline, cancel_event)
                try:
                    atomic_replace_file(
                        temporary=image_bytes_path,
                        target=image_target,
                        close_file=_close_file,
                        open_directory=_open_directory,
                        replace_file=_replace_file,
                        sync_directory=_sync_directory,
                        unlink_temporary=_unlink_temporary,
                    )
                except AtomicCommitError:
                    published_image = True
                    raise
                published_image = True
            else:
                image_bytes_path.unlink(missing_ok=True)
            try:
                if (
                    hash_verified_regular_file(
                        image_target,
                        max_bytes=self.limits.max_page_image_bytes,
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                    != image_sha256
                ):
                    raise PageCacheError("cache image failed verification")
                _check_control(deadline, cancel_event)
            except (InterruptedError, TimeoutError):
                raise
            except (OSError, PageCacheError):
                _quarantine_named_entry(
                    entry_dir,
                    image_name,
                    expected_identity=_path_entry_identity(image_target),
                    valid_sha256=image_sha256,
                )
                raise PageCacheError("cache image failed verification") from None
            self._write_meta_atomic(entry_dir, meta, encoded=encoded_meta)
            _touch_directory(entry_dir)
            self._commit_capacity(reservation)
            return _cached_payload_from_meta(meta, str(image_target))
        except BaseException:
            if reservation.active:
                if published_image or image_target.exists():
                    self._commit_capacity(reservation)
                else:
                    self._release_capacity(reservation)
            raise

    def temporary_image_path(self, cache_key: str) -> Path:
        self._assert_root_current()
        temp_dir = self.entry_dir(cache_key)
        _ensure_directory_chain(self.pages_dir, (cache_key[:2], cache_key))
        return temp_dir / f".{cache_key}.{uuid.uuid4().hex}.tmp"

    def quarantine(self, entry_dir: Path) -> None:
        self._assert_root_current()
        identity = _path_entry_identity(entry_dir)
        if identity is None:
            return
        _quarantine_named_entry(
            entry_dir.parent,
            entry_dir.name,
            expected_identity=identity,
        )

    def _write_meta_atomic(
        self,
        entry_dir: Path,
        meta: dict[str, Any],
        *,
        encoded: bytes | None = None,
    ) -> None:
        _assert_directory_chain(entry_dir)
        if encoded is None:
            encoded = json.dumps(
                meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        if len(encoded) > self.limits.max_page_metadata_bytes:
            raise PageCacheError("OCR cache capacity exceeded")
        atomic_write_bytes(
            target=entry_dir / "meta.json",
            data=encoded,
            temporary_prefix=".meta.",
            writer=_write_file,
            sync_file=_fsync_file,
            close_file=_close_file,
            open_directory=_open_directory,
            replace_file=_replace_file,
            sync_directory=_sync_directory,
        )

    @staticmethod
    def _load_meta(meta_path: Path, *, max_bytes: int = _MAX_CACHE_META_BYTES) -> dict[str, Any]:
        fd = None
        try:
            fd = os.open(meta_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            current = os.stat(meta_path, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise PageCacheError("cache metadata failed verification")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PageCacheError("cache metadata failed verification")
            raw_bytes = os.read(fd, max_bytes + 1)
            if len(raw_bytes) > max_bytes:
                raise PageCacheError("cache metadata failed verification")
            raw = raw_bytes.decode("utf-8")
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except Exception as exc:
            raise PageCacheError("cache metadata failed verification") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(value, dict):
            raise PageCacheError("cache metadata failed verification")
        return value

    @staticmethod
    def _validate_meta(payload: dict[str, Any], cache_key: str, inputs: CacheInputs) -> None:
        expected_fields = {
            "schema_version",
            "cache_key",
            "pdf_sha256",
            "page",
            "dpi",
            "helper_version",
            "language_config",
            "image_name",
            "image_sha256",
            "width",
            "height",
            "supported_languages",
            "observations",
        }
        if set(payload) != expected_fields:
            raise PageCacheError("cache metadata failed verification")
        expected = {
            "cache_key": cache_key,
            "pdf_sha256": inputs.pdf_sha256,
            "page": inputs.page,
            "dpi": inputs.dpi,
            "helper_version": inputs.helper_version,
            "language_config": list(inputs.language_config),
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                raise PageCacheError("cache metadata failed verification")
        if payload.get("schema_version") != 1:
            raise PageCacheError("cache metadata failed verification")
        if not _is_sha256(payload.get("image_sha256")):
            raise PageCacheError("cache metadata failed verification")
        if not isinstance(payload.get("image_name"), str) or "/" in payload["image_name"]:
            raise PageCacheError("cache metadata failed verification")
        if payload["image_name"] != f"{payload['image_sha256']}.image":
            raise PageCacheError("cache metadata failed verification")
        _validate_positive_int(payload.get("width"))
        _validate_positive_int(payload.get("height"))
        _validate_string_list(payload.get("supported_languages"))
        _validate_observations(payload.get("observations"))


def copy_verified_helper_image(
    *,
    jobs_root: Path,
    job_dir: Path,
    relative_image_path: str,
    expected_sha256: str,
    destination: Path,
    max_bytes: int | None = None,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> str:
    _check_control(deadline, cancel_event)
    if not _is_sha256(expected_sha256):
        raise PageCacheError("helper image failed verification")
    if not isinstance(relative_image_path, str) or not relative_image_path:
        raise PageCacheError("helper image failed verification")
    candidate_path = Path(relative_image_path)
    try:
        verified_job_dir = job_dir.resolve(strict=True)
    except OSError as exc:
        raise PageCacheError("helper image failed verification") from exc
    if candidate_path.is_absolute():
        try:
            if candidate_path.parent.resolve(strict=True) != verified_job_dir:
                raise PageCacheError("helper image failed verification")
        except OSError as exc:
            raise PageCacheError("helper image failed verification") from exc
        image_name = candidate_path.name
    else:
        if ".." in candidate_path.parts:
            raise PageCacheError("helper image failed verification")
        candidate = jobs_root / candidate_path
        try:
            candidate.relative_to(job_dir)
        except ValueError as exc:
            raise PageCacheError("helper image failed verification") from exc
        if candidate.parent != job_dir:
            raise PageCacheError("helper image failed verification")
        image_name = candidate.name
    if not image_name or image_name in {".", ".."} or "/" in image_name:
        raise PageCacheError("helper image failed verification")

    job_fd = os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(
            image_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=job_fd,
        )
        try:
            opened = os.fstat(fd)
            current = os.stat(image_name, dir_fd=job_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise PageCacheError("helper image failed verification")
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PageCacheError("helper image failed verification")
            digest = _copy_from_fd_and_hash(
                fd,
                destination,
                max_bytes=max_bytes,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        finally:
            os.close(fd)
    finally:
        os.close(job_fd)
    if digest != expected_sha256:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise PageCacheError("helper image failed verification")
    return digest


def hash_verified_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> str:
    _check_control(deadline, cancel_event)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise PageCacheError("cache image failed verification")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PageCacheError("cache image failed verification")
        if max_bytes is not None and (info.st_size <= 0 or info.st_size > max_bytes):
            raise PageCacheError("OCR cache capacity exceeded")
        return _hash_fd(fd, deadline=deadline, cancel_event=cancel_event)
    finally:
        os.close(fd)


def _copy_from_fd_and_hash(
    source_fd: int,
    destination: Path,
    *,
    max_bytes: int | None = None,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    dest_fd = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            _check_control(deadline, cancel_event)
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise PageCacheError("OCR cache capacity exceeded")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                _check_control(deadline, cancel_event)
                written = os.write(dest_fd, view)
                if written <= 0:
                    raise OSError("snapshot write failed")
                view = view[written:]
            _check_control(deadline, cancel_event)
        sync_file_data(dest_fd)
        _check_control(deadline, cancel_event)
    except Exception:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(dest_fd)
    _fsync_directory(destination.parent)
    return digest.hexdigest()


def _copy_source_snapshot_and_hash(
    source_fd: int,
    destination: Path,
    *,
    max_bytes: int | None = None,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> str:
    return _copy_from_fd_and_hash(
        source_fd,
        destination,
        max_bytes=max_bytes,
        deadline=deadline,
        cancel_event=cancel_event,
    )


def _hash_fd(
    fd: int,
    *,
    deadline: float | None = None,
    cancel_event: Any | None = None,
) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        _check_control(deadline, cancel_event)
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    _check_control(deadline, cancel_event)
    return digest.hexdigest()


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _job_lock_key(name: str) -> str:
    return hashlib.sha256(f"job:{name}".encode()).hexdigest()


def _touch_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.utime(fd, None)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_control(deadline: float | None, cancel_event: Any | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("OCR cache operation cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("OCR cache operation timed out")


def _control_poll_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _LOCK_POLL_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OCR cache operation timed out")
    return min(_LOCK_POLL_SECONDS, remaining)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_private_root(root: Path) -> tuple[int, int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd: int | None = os.open(Path("/"), flags)
    except OSError as exc:
        raise PageCacheError("cache directory is not available") from exc
    try:
        for component in root.parts[1:]:
            assert fd is not None
            if not component or component in {".", ".."} or "/" in component:
                raise PageCacheError("cache directory is not available")
            parent_info = os.fstat(fd)
            parent_mode = stat.S_IMODE(parent_info.st_mode)
            writable_by_others = bool(parent_mode & 0o022)
            trusted_sticky = bool(parent_mode & stat.S_ISVTX) and parent_info.st_uid in {
                0,
                os.geteuid(),
            }
            if not stat.S_ISDIR(parent_info.st_mode) or (writable_by_others and not trusted_sticky):
                raise PageCacheError("cache directory is not available")
            try:
                child_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(component, flags, dir_fd=fd)
                except OSError as exc:
                    raise PageCacheError("cache directory is not available") from exc
            except OSError as exc:
                raise PageCacheError("cache directory is not available") from exc
            parent_fd = fd
            fd = child_fd
            os.close(parent_fd)
        assert fd is not None
        root_info = os.fstat(fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
            raise PageCacheError("cache directory is not available")
        try:
            os.fchmod(fd, 0o700)
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        hardened = os.fstat(fd)
        if stat.S_IMODE(hardened.st_mode) != 0o700 or hardened.st_uid != os.geteuid():
            raise PageCacheError("cache directory is not available")
        return hardened.st_dev, hardened.st_ino
    finally:
        if fd is not None:
            os.close(fd)


def _open_directory_chain(
    base: Path,
    components: tuple[str, ...],
    *,
    create: bool,
    expected_base: tuple[int, int] | None = None,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd: int | None = os.open(base, flags)
    except OSError as exc:
        raise PageCacheError("cache directory is not available") from exc
    try:
        if expected_base is not None:
            assert fd is not None
            opened_base = os.fstat(fd)
            if (opened_base.st_dev, opened_base.st_ino) != expected_base:
                raise PageCacheError("cache directory is not available")
        for component in components:
            assert fd is not None
            if not component or component in {".", ".."} or "/" in component:
                raise PageCacheError("cache directory is not available")
            try:
                child_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise PageCacheError("cache directory is not available") from None
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(component, flags, dir_fd=fd)
                except OSError as exc:
                    raise PageCacheError("cache directory is not available") from exc
            except OSError as exc:
                raise PageCacheError("cache directory is not available") from exc
            parent_fd = fd
            fd = child_fd
            _close_file(parent_fd)
        assert fd is not None
        result = fd
        fd = None
        return result
    except BaseException:
        if fd is not None:
            closing_fd = fd
            fd = None
            try:
                _close_file(closing_fd)
            except BaseException:
                pass
        raise


def _ensure_directory_chain(
    base: Path,
    components: tuple[str, ...],
    *,
    expected_base: tuple[int, int] | None = None,
) -> None:
    fd = _open_directory_chain(
        base,
        components,
        create=True,
        expected_base=expected_base,
    )
    os.close(fd)


def _chmod_directory(
    base: Path,
    components: tuple[str, ...],
    *,
    expected_base: tuple[int, int] | None = None,
) -> None:
    fd = _open_directory_chain(
        base,
        components,
        create=False,
        expected_base=expected_base,
    )
    try:
        os.fchmod(fd, 0o700)
    except OSError as exc:
        raise PageCacheError("cache directory is not available") from exc
    finally:
        os.close(fd)


def _assert_directory_chain(path: Path) -> None:
    relative = path.relative_to(path.anchor if path.is_absolute() else Path("."))
    parts = tuple(part for part in relative.parts if part not in {path.anchor})
    if not parts:
        return
    base = Path(path.anchor or "/")
    fd = _open_directory_chain(base, parts, create=False)
    os.close(fd)


def _directory_chain_is_safe(base: Path, components: tuple[str, ...]) -> bool:
    try:
        fd = _open_directory_chain(base, components, create=False)
    except (OSError, PageCacheError):
        return False
    os.close(fd)
    return True


def _path_entry_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _quarantine_named_entry(
    directory: Path,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    valid_sha256: str | None = None,
) -> bool:
    _assert_directory_chain(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory, flags)
    quarantine_name = f".{name}.corrupt-{uuid.uuid4().hex}"
    try:
        try:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        before_identity = (before.st_dev, before.st_ino)
        if expected_identity is not None and before_identity != expected_identity:
            return False
        rename_exclusive(directory / name, directory / quarantine_name, fd)
        try:
            moved = os.stat(quarantine_name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            raise PageCacheError("cache entry failed quarantine") from None
        moved_identity = (moved.st_dev, moved.st_ino)
        restore = expected_identity is not None and moved_identity != expected_identity
        if not restore and valid_sha256 is not None and stat.S_ISREG(moved.st_mode):
            moved_fd = os.open(
                quarantine_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=fd,
            )
            try:
                restore = _hash_fd(moved_fd) == valid_sha256
            finally:
                os.close(moved_fd)
        if restore:
            try:
                rename_exclusive(directory / quarantine_name, directory / name, fd)
            except OSError as exc:
                raise PageCacheError("cache entry failed quarantine") from exc
            restored = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (restored.st_dev, restored.st_ino) != moved_identity:
                raise PageCacheError("cache entry failed quarantine")
            os.fsync(fd)
            return False
        os.fsync(fd)
        return True
    finally:
        os.close(fd)


def _normalize_safe_system_path(path: Path) -> Path:
    if path.parts[:2] != ("/", "var"):
        return path
    try:
        var_info = Path("/var").lstat()
        target = os.readlink("/var")
        private_var = Path("/private/var")
        private_info = private_var.lstat()
    except OSError:
        return path
    if (
        stat.S_ISLNK(var_info.st_mode)
        and var_info.st_uid == 0
        and target in {"private/var", "/private/var"}
        and stat.S_ISDIR(private_info.st_mode)
        and private_info.st_uid == 0
    ):
        return private_var.joinpath(*path.parts[2:])
    return path


def _cached_payload_from_meta(payload: dict[str, Any], image_path: str) -> CachedPagePayload:
    return CachedPagePayload(
        cache_key=payload["cache_key"],
        pdf_sha256=payload["pdf_sha256"],
        page=payload["page"],
        dpi=payload["dpi"],
        helper_version=payload["helper_version"],
        language_config=tuple(payload["language_config"]),
        image_path=image_path,
        image_sha256=payload["image_sha256"],
        width=payload["width"],
        height=payload["height"],
        supported_languages=tuple(payload["supported_languages"]),
        observations=tuple(payload["observations"]),
    )


def _validate_positive_int(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PageCacheError("invalid helper response")


def _validate_string_list(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_LANGUAGE_COUNT
        or not all(isinstance(item, str) and len(item) <= _MAX_LANGUAGE_LENGTH for item in value)
    ):
        raise PageCacheError("invalid helper response")
    return tuple(value)


def _validate_observations(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) > _MAX_OBSERVATION_COUNT:
        raise PageCacheError("invalid helper response")
    seen = set()
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise PageCacheError("invalid helper response")
        if set(item) != {"text", "confidence", "bounding_box", "candidates"}:
            raise PageCacheError("invalid helper response")
        if not _valid_text(item.get("text")):
            raise PageCacheError("invalid helper response")
        confidence = _validate_unit_interval_number(item.get("confidence"))
        bounding_box = _validate_bounding_box(item.get("bounding_box"))
        candidates = _validate_candidates(item.get("candidates"))
        normalized = {
            "text": item["text"],
            "confidence": confidence,
            "bounding_box": bounding_box,
            "candidates": candidates,
        }
        marker = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if marker in seen:
            raise PageCacheError("invalid helper response")
        seen.add(marker)
        result.append(normalized)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    if len(encoded) > _MAX_OBSERVATION_METADATA_BYTES:
        raise PageCacheError("invalid helper response")
    return tuple(result)


def _validate_bounding_box(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise PageCacheError("invalid helper response")
    if set(value) != {"x", "y", "width", "height"}:
        raise PageCacheError("invalid helper response")
    normalized = {
        field: _validate_unit_interval_number(value.get(field))
        for field in ("x", "y", "width", "height")
    }
    if normalized["x"] + normalized["width"] > 1 or normalized["y"] + normalized["height"] > 1:
        raise PageCacheError("invalid helper response")
    return normalized


def _validate_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_CANDIDATES:
        raise PageCacheError("invalid helper response")
    result = []
    for candidate in value:
        if not isinstance(candidate, dict) or not _valid_text(candidate.get("text")):
            raise PageCacheError("invalid helper response")
        if set(candidate) != {"text", "confidence"}:
            raise PageCacheError("invalid helper response")
        result.append(
            {
                "text": candidate["text"],
                "confidence": _validate_unit_interval_number(candidate.get("confidence")),
            }
        )
    return result


def _valid_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= _MAX_TEXT_LENGTH


def _validate_unit_interval_number(value: Any) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PageCacheError("invalid helper response")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise PageCacheError("invalid helper response")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)
