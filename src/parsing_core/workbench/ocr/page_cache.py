from __future__ import annotations

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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PageCacheError(RuntimeError):
    pass


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


@dataclass
class _ThreadLockEntry:
    lock: threading.Lock
    refcount: int = 0


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, _ThreadLockEntry] = {}

_MAX_CACHE_META_BYTES = 512 * 1024
_MAX_OBSERVATION_COUNT = 256
_MAX_OBSERVATION_METADATA_BYTES = 512 * 1024
_MAX_TEXT_LENGTH = 4096
_MAX_CANDIDATES = 5
_MAX_LANGUAGE_COUNT = 128
_MAX_LANGUAGE_LENGTH = 64


def canonical_language_config(languages: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(languages, list | tuple)
        or len(languages) > _MAX_LANGUAGE_COUNT
    ):
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
    def __init__(self, root: Path | str):
        requested_root = Path(root).expanduser()
        if not requested_root.is_absolute():
            requested_root = Path.cwd() / requested_root
        requested_root = _normalize_safe_system_path(requested_root)
        if ".." in requested_root.parts:
            raise PageCacheError("cache directory is not available")
        self.root = Path(os.path.normpath(os.fspath(requested_root)))
        if not self.root.is_absolute():
            raise PageCacheError("cache directory is not available")
        _ensure_directory_chain(Path("/"), tuple(self.root.parts[1:]))
        self.pages_dir = self.root / "pages"
        self.locks_dir = self.root / "locks"
        self.jobs_root = self.root / "jobs"
        self.source_snapshots_dir = self.root / "source_snapshots"
        for directory_name in ("pages", "locks", "jobs", "source_snapshots"):
            _ensure_directory_chain(self.root, (directory_name,))
            _chmod_directory(self.root, (directory_name,))

    @staticmethod
    def _prepare_root(root: Path) -> None:
        _ensure_directory_chain(Path("/"), tuple(root.parts[1:]))
        _chmod_directory(Path("/"), tuple(root.parts[1:]))

    @staticmethod
    def _assert_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise PageCacheError("cache directory is not available") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PageCacheError("cache directory is not available")

    def entry_dir(self, cache_key: str) -> Path:
        return self.pages_dir / cache_key[:2] / cache_key

    @contextmanager
    def lock(self, cache_key: str):
        with _THREAD_LOCKS_GUARD:
            entry = _THREAD_LOCKS.get(cache_key)
            if entry is None:
                entry = _ThreadLockEntry(threading.Lock())
                _THREAD_LOCKS[cache_key] = entry
            entry.refcount += 1
        try:
            thread_lock = entry.lock
            with thread_lock:
                _ensure_directory_chain(self.root, ("locks",))
                # Lock files are stable coordination inodes. Runtime cleanup must only
                # release flock and close the fd; unlinking can split waiting processes.
                lock_path = self.locks_dir / f"{cache_key}.lock"
                fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    yield
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
        finally:
            with _THREAD_LOCKS_GUARD:
                current = _THREAD_LOCKS.get(cache_key)
                if current is entry:
                    entry.refcount -= 1
                    if entry.refcount == 0:
                        del _THREAD_LOCKS[cache_key]

    def publish_source_snapshot(self, source_fd: int) -> SourceSnapshot:
        _assert_directory_chain(self.source_snapshots_dir)
        temporary = self.source_snapshots_dir / f".snapshot-{uuid.uuid4().hex}.tmp"
        digest = _copy_source_snapshot_and_hash(source_fd, temporary)
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o400)
            os.fsync(fd)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(fd)

        pdf_sha256 = digest
        target = self.source_snapshots_dir / f"{pdf_sha256}.pdf"
        try:
            for _attempt in range(2):
                linked_new_target = False
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
                else:
                    temporary.unlink()
                    _fsync_directory(self.source_snapshots_dir)
                    linked_new_target = True
                try:
                    snapshot = self.validate_source_snapshot(
                        target, pdf_sha256, verify_hash=True
                    )
                except Exception:
                    _quarantine_named_entry(self.source_snapshots_dir, target.name)
                    if linked_new_target:
                        raise
                    continue
                if not linked_new_target:
                    temporary.unlink()
                    _fsync_directory(self.source_snapshots_dir)
                return snapshot
            raise PageCacheError("source snapshot failed verification")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def validate_source_snapshot(
        self,
        snapshot: SourceSnapshot | Path,
        expected_sha256: str | None = None,
        *,
        verify_hash: bool,
    ) -> SourceSnapshot:
        if isinstance(snapshot, SourceSnapshot):
            path = snapshot.path
            pdf_sha256 = snapshot.pdf_sha256
            expected_identity = snapshot.identity
        else:
            path = snapshot
            if expected_sha256 is None:
                raise PageCacheError("source snapshot failed verification")
            pdf_sha256 = expected_sha256
            expected_identity = None
        if not _is_sha256(pdf_sha256) or path.name != f"{pdf_sha256}.pdf":
            raise PageCacheError("source snapshot failed verification")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            current = os.stat(path, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if identity != (current.st_dev, current.st_ino):
                raise PageCacheError("source snapshot failed verification")
            if expected_identity is not None and identity != expected_identity:
                raise PageCacheError("source snapshot failed verification")
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PageCacheError("source snapshot failed verification")
            if stat.S_IMODE(opened.st_mode) & 0o222:
                raise PageCacheError("source snapshot failed verification")
            if verify_hash and _hash_fd(fd) != pdf_sha256:
                raise PageCacheError("source snapshot failed verification")
            return SourceSnapshot(pdf_sha256=pdf_sha256, path=path, identity=identity)
        finally:
            os.close(fd)

    def make_job_dir(self, cache_key: str) -> tuple[str, Path, Path]:
        job_root = self.jobs_root / f"job-{cache_key}-{uuid.uuid4().hex}"
        _ensure_directory_chain(self.root, ("jobs", job_root.name))
        job_dir = job_root / "output"
        _ensure_directory_chain(job_root, ("output",))
        return "output", job_dir, job_root

    def cleanup_job_dir(self, job_dir: Path) -> None:
        shutil.rmtree(job_dir, ignore_errors=True)

    def load_valid(self, cache_key: str, inputs: CacheInputs) -> CachedPagePayload | None:
        entry_dir = self.entry_dir(cache_key)
        if not _directory_chain_is_safe(self.pages_dir, (cache_key[:2], cache_key)):
            return None
        meta_path = entry_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            payload = self._load_meta(meta_path)
            self._validate_meta(payload, cache_key, inputs)
            image_path = entry_dir / payload["image_name"]
            image_hash = hash_verified_regular_file(image_path)
            if image_hash != payload["image_sha256"]:
                raise PageCacheError("cache image failed verification")
            return _cached_payload_from_meta(payload, str(image_path))
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
    ) -> CachedPagePayload:
        entry_dir = self.entry_dir(cache_key)
        _ensure_directory_chain(self.pages_dir, (cache_key[:2], cache_key))
        self._assert_directory(entry_dir)
        image_name = f"{image_sha256}.image"
        image_target = entry_dir / image_name
        try:
            existing_hash = hash_verified_regular_file(image_target)
        except (OSError, PageCacheError):
            _quarantine_named_entry(entry_dir, image_name)
            existing_hash = None
        if existing_hash != image_sha256:
            os.replace(image_bytes_path, image_target)
            _fsync_directory(entry_dir)
        else:
            try:
                image_bytes_path.unlink()
            except FileNotFoundError:
                pass
        try:
            if hash_verified_regular_file(image_target) != image_sha256:
                raise PageCacheError("cache image failed verification")
        except (OSError, PageCacheError):
            _quarantine_named_entry(entry_dir, image_name)
            raise PageCacheError("cache image failed verification") from None
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
        self._write_meta_atomic(entry_dir, meta)
        return _cached_payload_from_meta(meta, str(image_target))

    def temporary_image_path(self, cache_key: str) -> Path:
        temp_dir = self.entry_dir(cache_key)
        _ensure_directory_chain(self.pages_dir, (cache_key[:2], cache_key))
        return temp_dir / f".{cache_key}.{uuid.uuid4().hex}.tmp"

    def quarantine(self, entry_dir: Path) -> None:
        if not entry_dir.exists():
            return
        target = entry_dir.with_name(
            f"{entry_dir.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex}"
        )
        try:
            os.replace(entry_dir, target)
        except OSError:
            shutil.rmtree(entry_dir, ignore_errors=True)

    def _write_meta_atomic(self, entry_dir: Path, meta: dict[str, Any]) -> None:
        _assert_directory_chain(entry_dir)
        temporary = entry_dir / f".meta.{uuid.uuid4().hex}.tmp"
        encoded = json.dumps(
            meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, entry_dir / "meta.json")
        _fsync_directory(entry_dir)

    @staticmethod
    def _load_meta(meta_path: Path) -> dict[str, Any]:
        fd = None
        try:
            fd = os.open(meta_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            current = os.stat(meta_path, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise PageCacheError("cache metadata failed verification")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PageCacheError("cache metadata failed verification")
            raw_bytes = os.read(fd, _MAX_CACHE_META_BYTES + 1)
            if len(raw_bytes) > _MAX_CACHE_META_BYTES:
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
) -> str:
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
            digest = _copy_from_fd_and_hash(fd, destination)
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


def hash_verified_regular_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise PageCacheError("cache image failed verification")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PageCacheError("cache image failed verification")
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _copy_from_fd_and_hash(source_fd: int, destination: Path) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    dest_fd = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            os.write(dest_fd, chunk)
        os.fsync(dest_fd)
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


def _copy_source_snapshot_and_hash(source_fd: int, destination: Path) -> str:
    return _copy_from_fd_and_hash(source_fd, destination)


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_directory_chain(base: Path, components: tuple[str, ...], *, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(base, flags)
    except OSError as exc:
        raise PageCacheError("cache directory is not available") from exc
    try:
        for component in components:
            if not component or component in {".", ".."} or "/" in component:
                raise PageCacheError("cache directory is not available")
            try:
                child_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise PageCacheError("cache directory is not available") from None
                os.mkdir(component, 0o700, dir_fd=fd)
                child_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                raise PageCacheError("cache directory is not available") from exc
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _ensure_directory_chain(base: Path, components: tuple[str, ...]) -> None:
    fd = _open_directory_chain(base, components, create=True)
    os.close(fd)


def _chmod_directory(base: Path, components: tuple[str, ...]) -> None:
    fd = _open_directory_chain(base, components, create=False)
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


def _quarantine_named_entry(directory: Path, name: str) -> None:
    _assert_directory_chain(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory, flags)
    quarantine_name = f".{name}.corrupt-{uuid.uuid4().hex}"
    try:
        try:
            os.rename(name, quarantine_name, src_dir_fd=fd, dst_dir_fd=fd)
        except FileNotFoundError:
            return
        os.fsync(fd)
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
        marker = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
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
    if not isinstance(value, int | float) or isinstance(value, bool):
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
