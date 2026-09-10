#!/usr/bin/python3 -I
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import NamedTuple

SYSTEM_CURL = Path("/usr/bin/curl")
SYSTEM_FILE = Path("/usr/bin/file")
SYSTEM_XATTR = Path("/usr/bin/xattr")
SAFE_PATH = "/usr/bin:/bin"
DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000
DEFAULT_MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MAX_WHEEL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_WHEELHOUSE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_WHEELHOUSE_FILES = 2_048
COPY_CHUNK_SIZE = 1024 * 1024
RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004
PINNED_PYTHON_VERSION = "3.12.13"
PINNED_UV_VERSION = "0.12.3"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple"
WHEEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl")
TRANSACTION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
CLEANUP_TOMBSTONE_PATTERN = re.compile(r"\.sidecar-runtime\.tombstone\.([0-9a-f]{32})\Z")
CLEANUP_CLAIM_PATTERN = re.compile(
    r"\.sidecar-runtime\.claim\.([0-9a-f]{32})\.([cd])\."
    r"([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{8})\Z"
)
INSTALL_TRANSACTION_MEMBER_PATTERN = re.compile(
    r"([0-9a-f]{32})\.(json|committed|aborted|finished)\Z"
)
TRANSACTION_CLAIM_PATTERN = re.compile(
    r"\.sidecar-runtime\.transaction-claim\.([0-9a-f]{32})\."
    r"(json|committed|aborted|finished)\."
    r"([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{8})\Z"
)
PREFETCH_INTENT_PATTERN = re.compile(r"\.prefetch\.([0-9a-f]{32})\.intent\Z")
PREFETCH_WORKSPACE_PATTERN = re.compile(r"\.prefetch\.([0-9a-f]{32})\.workspace\Z")
PREFETCH_RECEIPT_CANDIDATE_PATTERN = re.compile(
    r"\.prefetch-workspace\.([0-9a-f]{32})\.receipt\.candidate\."
    r"([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{8})\Z"
)
PUBLISHED_WHEELHOUSE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_CLEANUP_NAMESPACE_ENTRIES = 4_096
COMPLETED_TRANSACTION_RETENTION = 2
ALLOWED_XATTRS = frozenset({"com.apple.provenance"})
LIBC = ctypes.CDLL(None, use_errno=True)


class FileIdentity(NamedTuple):
    device: int
    inode: int
    file_type: int


class FileObservation(NamedTuple):
    identity: FileIdentity
    mode: int
    link_count: int
    owner: int
    group: int
    size: int
    modified_ns: int
    changed_ns: int
    flags: int


class SealedTool(NamedTuple):
    label: str
    path: Path
    descriptor: int
    observation: FileObservation
    sha256: str


class Wheelhouse(NamedTuple):
    path: Path
    manifest_sha256: str
    requirements_sha256: str
    lock_sha256: str


class CleanupLockOwnership(NamedTuple):
    descriptors: tuple[int, int]
    lock_path: Path
    lock_parent_identity: FileIdentity
    lock_identity: FileIdentity
    protected_root: Path
    protected_root_identity: FileIdentity
    process_id: int
    thread_id: int


class TransactionMember(NamedTuple):
    path: Path
    identity: FileIdentity
    claimed: bool


class PrefetchReceiptState(NamedTuple):
    final: Path | None
    allocation: Path | None
    candidate: Path | None
    candidate_identity: FileIdentity | None
    other_entries: tuple[str, ...]


_ACTIVE_CLEANUP_LOCKS: dict[int, CleanupLockOwnership] = {}


def file_identity(path: Path) -> FileIdentity:
    metadata = path.lstat()
    return FileIdentity(metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _identity_from_stat(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _file_observation(metadata: os.stat_result) -> FileObservation:
    return FileObservation(
        identity=_identity_from_stat(metadata),
        mode=metadata.st_mode,
        link_count=metadata.st_nlink,
        owner=metadata.st_uid,
        group=metadata.st_gid,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        flags=int(getattr(metadata, "st_flags", 0)),
    )


def _identity_or_none(path: Path) -> FileIdentity | None:
    try:
        return file_identity(path)
    except FileNotFoundError:
        return None


def _require_identity(path: Path, expected: FileIdentity, message: str) -> None:
    if _identity_or_none(path) != expected:
        raise ValueError(message)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fd_sha256(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        block = os.read(descriptor, COPY_CHUNK_SIZE)
        if not block:
            break
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular file: {path}")
        digest = _fd_sha256(descriptor)
        after = os.fstat(descriptor)
        if _file_observation(before) != _file_observation(after):
            raise ValueError(f"file changed while hashing: {path}")
        _require_identity(path, _identity_from_stat(before), f"file path changed: {path}")
        return digest
    finally:
        os.close(descriptor)


def _xattrs(path: Path) -> list[str]:
    if hasattr(os, "listxattr"):
        try:
            names = list(os.listxattr(path, follow_symlinks=False))
        except OSError as error:
            if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                return []
            raise
    else:
        _require_system_tool(SYSTEM_XATTR)
        result = subprocess.run(
            [str(SYSTEM_XATTR), "-s", str(path)],
            env={"LC_ALL": "C", "PATH": SAFE_PATH},
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise OSError(f"failed to inspect extended attributes for {path}: {detail}")
        names = [line.decode(errors="surrogateescape") for line in result.stdout.splitlines()]
    return sorted(name for name in names if name not in ALLOWED_XATTRS)


def _normal_member_path(raw_name: str) -> PurePosixPath:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        raise ValueError(f"unsafe archive member: {raw_name}")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_name.split("/")):
        raise ValueError(f"unsafe archive member: {raw_name}")
    if not path.parts or path.parts[0] != "python":
        raise ValueError(f"unsafe archive member: {raw_name}")
    return path


def _normal_link_target(member_path: PurePosixPath, raw_target: str) -> PurePosixPath:
    target = PurePosixPath(raw_target)
    if not raw_target or "\x00" in raw_target or "\\" in raw_target or target.is_absolute():
        raise ValueError(f"unsafe archive member: {member_path}")
    parts = list(member_path.parent.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"unsafe archive member: {member_path}")
            parts.pop()
        else:
            parts.append(part)
    resolved = PurePosixPath(*parts)
    if not resolved.parts or resolved.parts[0] != "python":
        raise ValueError(f"unsafe archive member: {member_path}")
    return resolved


def _archive_members(
    archive: tarfile.TarFile,
    *,
    max_members: int,
    max_expanded_bytes: int,
) -> list[tuple[tarfile.TarInfo, PurePosixPath]]:
    members = archive.getmembers()
    if len(members) > max_members:
        raise ValueError("archive resource limit exceeded: member count")
    expanded_bytes = 0
    seen: dict[str, str] = {}
    kinds: dict[PurePosixPath, str] = {}
    result: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    for member in members:
        path = _normal_member_path(member.name)
        collision_key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        previous = seen.get(collision_key)
        if previous is not None:
            raise ValueError(f"archive member collision: {previous} and {member.name}")
        seen[collision_key] = member.name
        for header in member.pax_headers:
            if "xattr" in header.casefold():
                raise ValueError(f"unsafe archive member extended attribute: {member.name}")
        if member.islnk():
            raise ValueError(f"unsafe archive member hard link: {member.name}")
        if not (member.isfile() or member.isdir() or member.issym()):
            raise ValueError(f"unsafe archive member: {member.name}")
        if member.mode & 0o7000:
            raise ValueError(f"unsafe archive member mode: {member.name}")
        if member.isfile():
            if member.size < 0:
                raise ValueError(f"unsafe archive member: {member.name}")
            expanded_bytes += member.size
            if expanded_bytes > max_expanded_bytes:
                raise ValueError("archive resource limit exceeded: expanded size")
            kinds[path] = "file"
        elif member.isdir():
            kinds[path] = "directory"
        else:
            _normal_link_target(path, member.linkname)
            kinds[path] = "symlink"
        result.append((member, path))

    for path in kinds:
        for parent in path.parents:
            if parent == PurePosixPath("."):
                break
            if kinds.get(parent) in {"file", "symlink"}:
                raise ValueError(f"unsafe archive member parent: {path}")
    return result


def _copy_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, output: Path) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"cannot read archive member: {member.name}")
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        member.mode & 0o777,
    )
    written = 0
    try:
        while True:
            block = source.read(COPY_CHUNK_SIZE)
            if not block:
                break
            written += len(block)
            if written > member.size:
                raise ValueError(f"archive member exceeded declared size: {member.name}")
            offset = 0
            while offset < len(block):
                offset += os.write(descriptor, block[offset:])
        if written != member.size:
            raise ValueError(f"archive member size mismatch: {member.name}")
        os.fchmod(descriptor, member.mode & 0o777)
        os.fsync(descriptor)
    finally:
        source.close()
        os.close(descriptor)


def extract_verified_archive_fd(
    archive_fd: int,
    destination: Path,
    expected_sha256: str,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
    expected_destination: FileIdentity | None = None,
) -> None:
    before = os.fstat(archive_fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("archive must be a regular file")
    if before.st_size > max_archive_bytes:
        raise ValueError("archive resource limit exceeded: compressed size")
    actual_sha256 = _fd_sha256(archive_fd)
    if actual_sha256 != expected_sha256:
        raise ValueError("archive checksum mismatch")
    if expected_destination is None:
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"archive destination already exists: {destination}")
        destination.mkdir(mode=0o700)
    else:
        _require_identity(
            destination,
            expected_destination,
            "archive destination identity changed",
        )
        if expected_destination.file_type != stat.S_IFDIR or any(destination.iterdir()):
            raise ValueError("archive destination is not an empty directory")
    try:
        with os.fdopen(os.dup(archive_fd), "rb", closefd=True) as stream:
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                members = _archive_members(
                    archive,
                    max_members=max_members,
                    max_expanded_bytes=max_expanded_bytes,
                )
                directories: list[tuple[Path, int]] = []
                symlinks: list[tuple[Path, str]] = []
                for member, relative in members:
                    output = destination.joinpath(*relative.parts)
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    if member.isdir():
                        output.mkdir(exist_ok=True)
                        directories.append((output, member.mode & 0o777))
                    elif member.isfile():
                        _copy_tar_member(archive, member, output)
                    else:
                        symlinks.append((output, member.linkname))
                for output, target in symlinks:
                    os.symlink(target, output)
                for output, mode in reversed(directories):
                    output.chmod(mode)

        root = destination.resolve(strict=True)
        for output, _ in symlinks:
            resolved = output.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as error:
                raise ValueError(f"unsafe extracted symlink: {output}") from error
        for current, directory_names, _ in os.walk(destination, topdown=False):
            for name in directory_names:
                _fsync_directory(Path(current) / name)
            _fsync_directory(Path(current))
        after = os.fstat(archive_fd)
        if (
            _identity_from_stat(before) != _identity_from_stat(after)
            or before.st_size != after.st_size
        ):
            raise ValueError("archive inode changed during extraction")
        if _fd_sha256(archive_fd) != expected_sha256:
            raise ValueError("archive content changed during extraction")
    except Exception:
        # The caller owns cleanup. Protected build roots require its active
        # transaction guard; the standalone validator uses a standard temp root.
        raise


def _runtime_records(root: Path) -> list[dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"runtime root must be a directory: {root}")
    root_resolved = root.resolve(strict=True)
    records: list[dict[str, object]] = []
    collision_keys: set[str] = set()
    paths = [root, *sorted(root.rglob("*"), key=lambda value: os.fsencode(value.relative_to(root)))]
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        collision_key = unicodedata.normalize("NFC", relative).casefold()
        if collision_key in collision_keys:
            raise ValueError(f"runtime path collision: {relative}")
        collision_keys.add(collision_key)
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError(f"runtime owner is not trusted: {relative}")
        attributes = _xattrs(path)
        if attributes:
            raise ValueError(f"runtime extended attribute is not allowed: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if mode & 0o022:
                raise ValueError(f"runtime directory is group/world writable: {relative}")
            records.append({"mode": mode, "path": relative, "type": "directory"})
            continue
        if stat.S_ISLNK(metadata.st_mode):
            identity = _identity_from_stat(metadata)
            target = os.readlink(path)
            if os.path.isabs(target):
                raise ValueError(f"unsafe runtime symlink: {relative}")
            try:
                path.resolve(strict=True).relative_to(root_resolved)
            except (FileNotFoundError, ValueError) as error:
                raise ValueError(f"unsafe runtime symlink: {relative}") from error
            _require_identity(path, identity, f"runtime symlink changed: {relative}")
            records.append({"mode": mode, "path": relative, "target": target, "type": "symlink"})
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"runtime special node is not allowed: {relative}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if _identity_from_stat(metadata) != _identity_from_stat(opened):
                raise ValueError(f"runtime path changed while opening: {relative}")
            if opened.st_nlink != 1:
                raise ValueError(f"runtime hard link is not allowed: {relative}")
            mode = stat.S_IMODE(opened.st_mode)
            if mode & 0o022:
                raise ValueError(f"runtime file is group/world writable: {relative}")
            digest = _fd_sha256(descriptor)
            after = os.fstat(descriptor)
            if _file_observation(opened) != _file_observation(after):
                raise ValueError(f"runtime file changed while hashing: {relative}")
            _require_identity(
                path,
                _identity_from_stat(opened),
                f"runtime path changed while hashing: {relative}",
            )
            records.append(
                {
                    "mode": mode,
                    "path": relative,
                    "sha256": digest,
                    "size": opened.st_size,
                    "type": "file",
                }
            )
        finally:
            os.close(descriptor)
    return records


def runtime_manifest_bytes(root: Path) -> bytes:
    document = {"entries": _runtime_records(root), "schema": 1}
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def verify_runtime_manifest_bytes(root: Path, expected: bytes) -> None:
    try:
        actual = runtime_manifest_bytes(root)
    except (OSError, ValueError) as error:
        raise ValueError(f"runtime manifest mismatch: {error}") from error
    if (
        not hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()
        or actual != expected
    ):
        raise ValueError("runtime manifest mismatch")


def _source_tree_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda value: os.fsencode(value.relative_to(root))):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        metadata = path.lstat()
        if metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError(f"source owner is not trusted: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ValueError(f"source directory is group/world writable: {relative}")
            records.append(
                {"mode": stat.S_IMODE(metadata.st_mode), "path": relative.as_posix(), "type": "d"}
            )
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ValueError(f"source hard link is not allowed: {relative}")
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ValueError(f"source file is group/world writable: {relative}")
            records.append(
                {
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "path": relative.as_posix(),
                    "sha256": _file_sha256(path),
                    "size": metadata.st_size,
                    "type": "f",
                }
            )
        else:
            raise ValueError(f"source special node is not allowed: {relative}")
    return records


def build_input_digest(repo: Path, prepare_script: Path, runtime_helper: Path) -> str:
    package = repo / "src/parsing_core"
    if not package.is_dir():
        raise ValueError(f"missing project package: {package}")
    files = []
    for label, path in (
        ("pyproject.toml", repo / "pyproject.toml"),
        ("uv.lock", repo / "uv.lock"),
        ("prepare-sidecar-python.sh", prepare_script),
        ("sidecar_runtime.py", runtime_helper),
    ):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError(f"invalid build input: {path}")
        files.append(
            {
                "label": label,
                "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": _file_sha256(path),
                "size": metadata.st_size,
            }
        )
    document = {"files": files, "package": _source_tree_records(package), "schema": 1}
    encoded = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def sanitized_build_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = home / "tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": SAFE_PATH,
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(temporary),
        "UV_NO_CONFIG": "1",
        "UV_OFFLINE": "1",
    }


def sanitized_prefetch_environment(home: Path) -> dict[str, str]:
    environment = sanitized_build_environment(home)
    environment.pop("PIP_NO_INDEX")
    environment.update(
        {
            "PIP_NO_INPUT": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return environment


def _validate_secure_tool(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"tool must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"tool must be a singly-linked file: {path}")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise ValueError(f"tool owner is not trusted: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ValueError(f"tool is group/world writable: {path}")
    if not stat.S_IMODE(metadata.st_mode) & 0o111:
        raise ValueError(f"tool is not executable: {path}")


def _observe_regular_descriptor(descriptor: int, path: Path) -> tuple[FileObservation, str]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"tool must be a regular file: {path}")
    digest = _fd_sha256(descriptor)
    after = os.fstat(descriptor)
    if _file_observation(before) != _file_observation(after):
        raise ValueError(f"tool changed while hashing: {path}")
    return _file_observation(after), digest


def _validate_sealed_tool(tool: SealedTool, message: str) -> None:
    descriptor_observation, descriptor_digest = _observe_regular_descriptor(
        tool.descriptor, tool.path
    )
    if descriptor_observation != tool.observation or descriptor_digest != tool.sha256:
        raise ValueError(message)
    path_descriptor = os.open(tool.path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        path_observation, path_digest = _observe_regular_descriptor(path_descriptor, tool.path)
    finally:
        os.close(path_descriptor)
    if path_observation != tool.observation or path_digest != tool.sha256:
        raise ValueError(message)


def _bind_existing_tool(path: Path, label: str) -> SealedTool:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        _validate_secure_tool(metadata, path)
        observation, digest = _observe_regular_descriptor(descriptor, path)
        _require_identity(path, observation.identity, f"{label} path identity changed")
        return SealedTool(label, path, descriptor, observation, digest)
    except Exception:
        os.close(descriptor)
        raise


def _harden_executable_mode(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        identity = _identity_from_stat(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or not stat.S_IMODE(before.st_mode) & 0o111
        ):
            raise ValueError(f"untrusted executable: {path}")
        os.fchmod(descriptor, 0o755)
        after = os.fstat(descriptor)
        if _identity_from_stat(after) != identity or stat.S_IMODE(after.st_mode) != 0o755:
            raise ValueError(f"failed to harden executable mode: {path}")
        _require_identity(path, identity, f"executable path identity changed: {path}")
    finally:
        os.close(descriptor)


def _run_sealed_tool(
    tool: SealedTool,
    arguments: list[str],
    *,
    environment: dict[str, str],
    lock_descriptors: tuple[int, ...],
    phase: str,
    timeout: int = 300,
) -> str:
    # macOS exposes neither fexecve nor executable /dev/fd handles here. The tool lives in a
    # private local-build directory; complete inode/content checks bracket every path execution.
    _validate_sealed_tool(tool, f"sealed {tool.label} changed before {phase}")
    result = subprocess.run(
        [str(tool.path), *arguments],
        env=environment,
        pass_fds=lock_descriptors,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    _validate_sealed_tool(tool, f"sealed {tool.label} changed after {phase}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OSError(f"command failed ({result.returncode}): {tool.path}: {detail}")
    return result.stdout


def seal_uv_tool(
    source: Path,
    destination: Path,
    expected_version: str,
    *,
    pass_fds: tuple[int, ...] = (),
) -> SealedTool:
    if not source.is_absolute():
        raise ValueError("uv path must be absolute")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    sealed_descriptor = -1
    try:
        before = os.fstat(source_fd)
        _validate_secure_tool(before, source)
        source_observation = _file_observation(before)
        destination.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o500,
        )
        digest = hashlib.sha256()
        try:
            while True:
                block = os.read(source_fd, COPY_CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
                offset = 0
                while offset < len(block):
                    offset += os.write(destination_fd, block[offset:])
            os.fchmod(destination_fd, 0o500)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        after = os.fstat(source_fd)
        if source_observation != _file_observation(after):
            raise ValueError("uv source changed while sealing")
        _require_identity(
            source, _identity_from_stat(before), "uv path identity changed while sealing"
        )
        _fsync_directory(destination.parent)
        sealed_descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        sealed_observation, sealed_digest = _observe_regular_descriptor(
            sealed_descriptor, destination
        )
        _validate_secure_tool(os.fstat(sealed_descriptor), destination)
        if sealed_digest != digest.hexdigest():
            raise ValueError("sealed uv content differs from copied source")
        tool = SealedTool("uv", destination, sealed_descriptor, sealed_observation, sealed_digest)
    except Exception:
        if sealed_descriptor >= 0:
            os.close(sealed_descriptor)
            sealed_descriptor = -1
        raise
    finally:
        os.close(source_fd)

    try:
        output = _run_sealed_tool(
            tool,
            ["--version"],
            environment=sanitized_build_environment(destination.parent / "home"),
            lock_descriptors=pass_fds,
            phase="version check",
            timeout=30,
        )
        first_line = output.splitlines()[0] if output.splitlines() else ""
        if not re.fullmatch(rf"uv {re.escape(expected_version)}(?: .*)?", first_line):
            raise ValueError(f"uv {expected_version} is required, got: {first_line}")
        return tool
    except Exception:
        if sealed_descriptor >= 0:
            os.close(sealed_descriptor)
        raise


def _canonical_executable(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    canonical = path.resolve(strict=True)
    if path != canonical:
        raise ValueError(f"{label} path must be canonical")
    return canonical


def bind_prefetch_python(
    path: Path,
    expected_version: str,
    *,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> SealedTool:
    canonical = _canonical_executable(path, "Python")
    if _xattrs(canonical):
        raise ValueError("Python executable has untrusted extended attributes")
    tool = _bind_existing_tool(canonical, "Python")
    probe = (
        "import json,os,platform,sys;"
        "print(json.dumps({"
        "'executable':os.path.realpath(sys.executable),"
        "'machine':platform.machine(),"
        "'version':platform.python_version()"
        "},separators=(',',':'),sort_keys=True))"
    )
    try:
        output = _run_sealed_tool(
            tool,
            ["-I", "-S", "-B", "-c", probe],
            environment=environment,
            lock_descriptors=pass_fds,
            phase="version check",
            timeout=30,
        )
        document = json.loads(output)
        if not isinstance(document, dict) or set(document) != {
            "executable",
            "machine",
            "version",
        }:
            raise ValueError("invalid Python identity response")
        if document.get("version") != expected_version:
            raise ValueError(
                f"Python {expected_version} is required, got: {document.get('version')}"
            )
        if document.get("machine") != "arm64":
            raise ValueError(f"arm64 Python is required, got: {document.get('machine')}")
        if document.get("executable") != str(canonical):
            raise ValueError("Python executable identity response does not match its path")
        return tool
    except Exception:
        os.close(tool.descriptor)
        raise


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _trusted_cleanup_root(path: Path) -> FileIdentity:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError(f"cleanup root owner/mode is not trusted: {path}")
        identity = _identity_from_stat(metadata)
    finally:
        os.close(descriptor)
    _require_identity(path, identity, f"cleanup root identity changed: {path}")
    return identity


def _require_active_cleanup_lock(
    cleanup_guard: tuple[int, int] | None,
    protected_root: Path | None = None,
) -> CleanupLockOwnership:
    if cleanup_guard is None:
        raise ValueError("destructive cleanup requires an active exclusive cleanup lock")
    ownership = _ACTIVE_CLEANUP_LOCKS.get(id(cleanup_guard))
    if ownership is None or ownership.descriptors is not cleanup_guard:
        raise ValueError("destructive cleanup requires an active exclusive cleanup lock")
    if ownership.process_id != os.getpid() or ownership.thread_id != threading.get_ident():
        raise ValueError("cleanup lock ownership does not belong to this execution context")
    requested_root = (
        ownership.protected_root if protected_root is None else _absolute_path(protected_root)
    )
    if requested_root != ownership.protected_root:
        raise ValueError("cleanup lock does not protect the requested root")
    parent_metadata = os.fstat(cleanup_guard[0])
    lock_metadata = os.fstat(cleanup_guard[1])
    if (
        _identity_from_stat(parent_metadata) != ownership.lock_parent_identity
        or _identity_from_stat(lock_metadata) != ownership.lock_identity
    ):
        raise ValueError("cleanup lock descriptor identity changed")
    try:
        fcntl.flock(cleanup_guard[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise ValueError("exclusive cleanup lock is no longer held") from error
    _require_identity(
        ownership.lock_path.parent,
        ownership.lock_parent_identity,
        "cleanup lock directory identity changed",
    )
    _require_identity(
        ownership.lock_path,
        ownership.lock_identity,
        "cleanup lock inode changed",
    )
    if _trusted_cleanup_root(requested_root) != ownership.protected_root_identity:
        raise ValueError("cleanup root identity changed while locked")
    return ownership


def _close_owned_lock_descriptor(
    descriptor: int,
    action: str,
    cleanup_errors: list[tuple[str, Exception]],
) -> None:
    try:
        os.close(descriptor)
    except Exception as error:
        cleanup_errors.append((action, error))


def _report_lock_cleanup_errors(
    active_error: BaseException | None,
    cleanup_errors: list[tuple[str, Exception]],
) -> None:
    if not cleanup_errors:
        return
    if active_error is not None:
        for action, cleanup_error in cleanup_errors:
            active_error.add_note(f"secure build lock cleanup {action} failed: {cleanup_error}")
        return

    primary_action, primary_error = cleanup_errors[0]
    primary_error.add_note(f"secure build lock cleanup failed first at: {primary_action}")
    for action, cleanup_error in cleanup_errors[1:]:
        primary_error.add_note(f"secure build lock cleanup {action} failed: {cleanup_error}")
    raise primary_error


@contextlib.contextmanager
def secure_build_lock(
    lock: Path,
    *,
    cleanup_root: Path | None = None,
) -> Iterator[tuple[int, int]]:
    lock = _absolute_path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_fd = os.open(lock.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = -1
    descriptors: tuple[int, int] | None = None
    parent_locked = False
    lock_locked = False
    try:
        parent_metadata = os.fstat(parent_fd)
        if parent_metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError(f"lock directory owner is not trusted: {lock.parent}")
        if stat.S_IMODE(parent_metadata.st_mode) & 0o022:
            raise ValueError(f"lock directory is group/world writable: {lock.parent}")
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        parent_locked = True
        lock_fd = os.open(
            lock.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(lock_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"lock must be a singly-linked regular file: {lock}")
        if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"lock owner/mode is not trusted: {lock}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_locked = True
        _require_identity(lock, _identity_from_stat(metadata), "lock inode changed before entry")
        descriptors = (parent_fd, lock_fd)
        if cleanup_root is not None:
            protected_root = _absolute_path(cleanup_root)
            ownership = CleanupLockOwnership(
                descriptors=descriptors,
                lock_path=lock,
                lock_parent_identity=_identity_from_stat(parent_metadata),
                lock_identity=_identity_from_stat(metadata),
                protected_root=protected_root,
                protected_root_identity=_trusted_cleanup_root(protected_root),
                process_id=os.getpid(),
                thread_id=threading.get_ident(),
            )
            _ACTIVE_CLEANUP_LOCKS[id(descriptors)] = ownership
        yield descriptors
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[tuple[str, Exception]] = []
        if descriptors is not None:
            _ACTIVE_CLEANUP_LOCKS.pop(id(descriptors), None)
        if lock_fd >= 0 and lock_locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception as error:
                cleanup_errors.append(("lock unlock", error))
        if lock_fd >= 0:
            _close_owned_lock_descriptor(
                lock_fd,
                "lock close",
                cleanup_errors,
            )
        if parent_locked:
            try:
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
            except Exception as error:
                cleanup_errors.append(("parent unlock", error))
        _close_owned_lock_descriptor(
            parent_fd,
            "parent close",
            cleanup_errors,
        )
        _report_lock_cleanup_errors(active_error, cleanup_errors)


def _install_transaction_directory(parent: Path, *, create: bool) -> Path | None:
    directory = parent / ".sidecar-runtime-transactions"
    if create:
        try:
            directory.mkdir(mode=0o700)
            _fsync_directory(parent)
        except FileExistsError:
            pass
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _xattrs(directory)
    ):
        raise ValueError("install transaction directory owner/mode is not trusted")
    _require_identity(
        directory,
        _identity_from_stat(metadata),
        "install transaction directory identity changed",
    )
    return directory


@contextlib.contextmanager
def _runtime_transaction_lock(
    target: Path,
    *,
    create: bool,
) -> Iterator[tuple[int, int] | None]:
    transaction_directory = _install_transaction_directory(target.parent, create=create)
    if transaction_directory is None:
        yield None
        return
    with secure_build_lock(
        transaction_directory / ".cleanup.lock",
        cleanup_root=target.parent,
    ) as cleanup_guard:
        if _install_transaction_directory(target.parent, create=False) != transaction_directory:
            raise ValueError("install transaction directory changed while locking")
        yield cleanup_guard


def _renamex(source: Path, destination: Path, flags: int) -> None:
    renamex_np = getattr(LIBC, "renamex_np", None)
    if renamex_np is None:
        raise OSError("macOS atomic rename primitives are unavailable")
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(source), os.fsencode(destination), flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _renameatx(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
    flags: int,
) -> None:
    renameatx_np = getattr(LIBC, "renameatx_np", None)
    if renameatx_np is None:
        raise OSError("macOS atomic rename-at primitives are unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    if (
        renameatx_np(
            source_descriptor,
            os.fsencode(source_name),
            destination_descriptor,
            os.fsencode(destination_name),
            flags,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _write_new_file(path: Path, payload: bytes, mode: int = 0o600) -> FileIdentity:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        identity = _identity_from_stat(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return identity


def _identity_document(identity: FileIdentity | None) -> list[int] | None:
    return None if identity is None else list(identity)


def _write_finished_transaction(
    path: Path,
    outcome: str,
    target_identity: FileIdentity | None,
) -> None:
    if outcome not in {"committed", "aborted"}:
        raise ValueError("invalid finished transaction outcome")
    payload = {
        "outcome": outcome,
        "schema": 1,
        "target_identity": _identity_document(target_identity),
    }
    _write_new_file(
        path,
        (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )


def _validate_transaction_id(transaction_id: str) -> str:
    if TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None:
        raise ValueError("invalid install transaction id")
    return transaction_id


def _cleanup_tombstone_name(transaction_id: str) -> str:
    return f".sidecar-runtime.tombstone.{_validate_transaction_id(transaction_id)}"


def _cleanup_claim_name(
    transaction_id: str,
    phase: str,
    identity: FileIdentity,
) -> str:
    if phase not in {"c", "d"}:
        raise ValueError("invalid cleanup claim phase")
    if (
        identity.device < 0
        or identity.device > 0xFFFFFFFFFFFFFFFF
        or identity.inode < 0
        or identity.inode > 0xFFFFFFFFFFFFFFFF
        or identity.file_type < 0
        or identity.file_type > 0xFFFFFFFF
    ):
        raise ValueError("cleanup identity exceeds the bounded claim format")
    return (
        f".sidecar-runtime.claim.{_validate_transaction_id(transaction_id)}.{phase}."
        f"{identity.device:016x}.{identity.inode:016x}.{identity.file_type:08x}"
    )


def _parse_cleanup_claim(
    name: str,
    transaction_id: str,
) -> tuple[str, FileIdentity] | None:
    if not name.startswith(".sidecar-runtime.claim."):
        return None
    match = CLEANUP_CLAIM_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError("malformed cleanup claim")
    if match.group(1) != _validate_transaction_id(transaction_id):
        raise ValueError("cleanup claim belongs to another transaction")
    return (
        match.group(2),
        FileIdentity(
            int(match.group(3), 16),
            int(match.group(4), 16),
            int(match.group(5), 16),
        ),
    )


def _parse_cleanup_root_name(
    name: str,
) -> tuple[str, str, FileIdentity | None] | None:
    if name.startswith(".sidecar-runtime.tombstone."):
        match = CLEANUP_TOMBSTONE_PATTERN.fullmatch(name)
        if match is None:
            raise ValueError("malformed cleanup tombstone")
        return match.group(1), "t", None
    if name.startswith(".sidecar-runtime.claim."):
        match = CLEANUP_CLAIM_PATTERN.fullmatch(name)
        if match is None:
            raise ValueError("malformed cleanup claim")
        return (
            match.group(1),
            match.group(2),
            FileIdentity(
                int(match.group(3), 16),
                int(match.group(4), 16),
                int(match.group(5), 16),
            ),
        )
    return None


def _scan_cleanup_namespace(
    candidate: Path,
    expected: FileIdentity | None,
    transaction_id: str,
    cleanup_guard: tuple[int, int] | None,
) -> Path | None:
    transaction_id = _validate_transaction_id(transaction_id)
    ownership = _require_active_cleanup_lock(cleanup_guard, candidate.parent)
    if expected is not None and expected.file_type not in {stat.S_IFDIR, stat.S_IFREG}:
        raise ValueError("cleanup root type is not supported")
    parent_descriptor = os.open(
        candidate.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if _identity_from_stat(os.fstat(parent_descriptor)) != ownership.protected_root_identity:
            raise ValueError("cleanup root identity changed while scanning")
        roots: list[tuple[str, str, FileIdentity | None]] = []
        entry_count = 0
        with os.scandir(parent_descriptor) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_CLEANUP_NAMESPACE_ENTRIES:
                    raise ValueError("cleanup namespace entry limit exceeded")
                name = entry.name
                if name == candidate.name:
                    roots.append((name, "candidate", None))
                    continue
                parsed = _parse_cleanup_root_name(name)
                if parsed is None:
                    continue
                root_transaction, phase, encoded_identity = parsed
                if root_transaction == transaction_id:
                    roots.append((name, phase, encoded_identity))
        if len(roots) > 1:
            raise ValueError("multiple cleanup roots exist for one transaction")
        if not roots:
            return None
        if expected is None:
            raise ValueError("unexpected cleanup root exists for this transaction")
        name, phase, encoded_identity = roots[0]
        if encoded_identity is not None and encoded_identity != expected:
            raise ValueError("cleanup claim identity does not match its transaction")
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        actual = _identity_from_stat(metadata)
        if actual != expected:
            if phase == "candidate":
                raise ValueError("cleanup candidate identity changed")
            if phase == "t":
                raise ValueError("cleanup tombstone identity changed")
            raise ValueError("cleanup claim identity changed")
        trusted_type = stat.S_ISDIR(metadata.st_mode) or (
            stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
        )
        if (
            not trusted_type
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or _xattrs(candidate.parent / name)
        ):
            raise ValueError("cleanup root owner/mode is not trusted")
        if _descriptor_entry_identity(parent_descriptor, name) != expected:
            raise ValueError("cleanup root identity changed while scanning")
        _require_active_cleanup_lock(cleanup_guard, candidate.parent)
        return candidate.parent / name
    finally:
        os.close(parent_descriptor)


def _descriptor_entry_identity(descriptor: int, name: str) -> FileIdentity:
    return _identity_from_stat(os.stat(name, dir_fd=descriptor, follow_symlinks=False))


def _capture_cleanup_name(
    descriptor: int,
    source_name: str,
    expected: FileIdentity,
    transaction_id: str,
    phase: str,
    cleanup_guard: tuple[int, int] | None,
) -> str:
    _require_active_cleanup_lock(cleanup_guard)
    destination_name = _cleanup_claim_name(transaction_id, phase, expected)
    if source_name == destination_name:
        if _descriptor_entry_identity(descriptor, source_name) != expected:
            raise ValueError("cleanup claim identity changed")
        return source_name
    if _descriptor_entry_identity(descriptor, source_name) != expected:
        raise ValueError("cleanup source identity changed before capture")
    try:
        _renameatx(
            descriptor,
            source_name,
            descriptor,
            destination_name,
            RENAME_EXCL,
        )
    except FileExistsError:
        destination_identity = _descriptor_entry_identity(descriptor, destination_name)
        try:
            source_identity = _descriptor_entry_identity(descriptor, source_name)
        except FileNotFoundError:
            source_identity = None
        if destination_identity == expected and source_identity is None:
            return destination_name
        raise ValueError("cleanup claim path is occupied") from None
    try:
        captured = _descriptor_entry_identity(descriptor, destination_name)
    except FileNotFoundError as error:
        raise ValueError("cleanup claim disappeared after capture") from error
    if captured != expected:
        try:
            _renameatx(
                descriptor,
                destination_name,
                descriptor,
                source_name,
                RENAME_EXCL,
            )
        except (FileExistsError, FileNotFoundError):
            pass
        raise ValueError("cleanup claim identity changed")
    return destination_name


def _advance_cleanup_claim(
    descriptor: int,
    name: str,
    phase: str,
    expected: FileIdentity,
    transaction_id: str,
    cleanup_guard: tuple[int, int] | None,
) -> str:
    if phase == "d":
        if _descriptor_entry_identity(descriptor, name) != expected:
            raise ValueError("cleanup delete claim identity changed")
        return name
    if phase != "c":
        raise ValueError("invalid cleanup claim phase")
    return _capture_cleanup_name(
        descriptor,
        name,
        expected,
        transaction_id,
        "d",
        cleanup_guard,
    )


def _remove_private_cleanup_claim(
    descriptor: int,
    name: str,
    expected: FileIdentity,
    *,
    directory: bool,
    cleanup_guard: tuple[int, int] | None,
) -> None:
    # M0 protects a private root shared by cooperating PDF2MD processes holding
    # this exclusive guard. macOS/POSIX has no inode-conditional unlink, so a
    # non-cooperating same-UID process replacing this final private name between
    # the check and syscall is explicitly outside the M0 threat model.
    _require_active_cleanup_lock(cleanup_guard)
    if _descriptor_entry_identity(descriptor, name) != expected:
        raise ValueError("cleanup delete claim identity changed")
    if directory:
        os.rmdir(name, dir_fd=descriptor)
    else:
        os.unlink(name, dir_fd=descriptor)


def _purge_directory_descriptor(
    descriptor: int,
    display: Path,
    transaction_id: str,
    cleanup_guard: tuple[int, int] | None,
) -> None:
    for original_name in os.listdir(descriptor):
        parsed = _parse_cleanup_claim(original_name, transaction_id)
        if parsed is None:
            before = os.stat(
                original_name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            expected = _identity_from_stat(before)
            name = _capture_cleanup_name(
                descriptor,
                original_name,
                expected,
                transaction_id,
                "c",
                cleanup_guard,
            )
            phase = "c"
        else:
            phase, expected = parsed
            name = original_name
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _identity_from_stat(before) != expected:
                raise ValueError(f"cleanup claim identity changed: {display / name}")
        name = _advance_cleanup_claim(
            descriptor,
            name,
            phase,
            expected,
            transaction_id,
            cleanup_guard,
        )
        child_display = display / name
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if _identity_from_stat(current) != expected:
            raise ValueError(f"cleanup delete claim identity changed: {child_display}")
        if stat.S_ISDIR(current.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                if _identity_from_stat(os.fstat(child)) != expected:
                    raise ValueError(f"cleanup directory changed while opening: {child_display}")
                _purge_directory_descriptor(
                    child,
                    child_display,
                    transaction_id,
                    cleanup_guard,
                )
            finally:
                os.close(child)
            _remove_private_cleanup_claim(
                descriptor,
                name,
                expected,
                directory=True,
                cleanup_guard=cleanup_guard,
            )
            continue
        if stat.S_ISREG(current.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                if _identity_from_stat(os.fstat(child)) != expected:
                    raise ValueError(f"cleanup file changed while opening: {child_display}")
            finally:
                os.close(child)
        elif not stat.S_ISLNK(current.st_mode):
            raise ValueError(f"unsupported cleanup node: {child_display}")
        _remove_private_cleanup_claim(
            descriptor,
            name,
            expected,
            directory=False,
            cleanup_guard=cleanup_guard,
        )
    os.fsync(descriptor)


def _remove_verified_tombstone(
    tombstone: Path,
    expected: FileIdentity,
    transaction_id: str,
    cleanup_guard: tuple[int, int] | None,
) -> None:
    _require_active_cleanup_lock(cleanup_guard, tombstone.parent)
    parent_descriptor = os.open(tombstone.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_descriptor = -1
    try:
        parsed = _parse_cleanup_claim(tombstone.name, transaction_id)
        if parsed is None:
            name = _capture_cleanup_name(
                parent_descriptor,
                tombstone.name,
                expected,
                transaction_id,
                "c",
                cleanup_guard,
            )
            phase = "c"
        else:
            phase, claim_identity = parsed
            if claim_identity != expected:
                raise ValueError("cleanup root claim identity changed")
            name = tombstone.name
        name = _advance_cleanup_claim(
            parent_descriptor,
            name,
            phase,
            expected,
            transaction_id,
            cleanup_guard,
        )
        if expected.file_type == stat.S_IFREG:
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                _identity_from_stat(current) != expected
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
            ):
                raise ValueError("cleanup file identity changed")
            _remove_private_cleanup_claim(
                parent_descriptor,
                name,
                expected,
                directory=False,
                cleanup_guard=cleanup_guard,
            )
            os.fsync(parent_descriptor)
            return
        if expected.file_type != stat.S_IFDIR:
            raise ValueError("cleanup root type is not supported")
        root_descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        if _identity_from_stat(os.fstat(root_descriptor)) != expected:
            raise ValueError("cleanup tombstone identity changed")
        _purge_directory_descriptor(
            root_descriptor,
            tombstone,
            transaction_id,
            cleanup_guard,
        )
        _remove_private_cleanup_claim(
            parent_descriptor,
            name,
            expected,
            directory=True,
            cleanup_guard=cleanup_guard,
        )
        os.fsync(parent_descriptor)
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _retire_cleanup_candidate(
    candidate: Path,
    expected: FileIdentity,
    transaction_id: str,
    cleanup_guard: tuple[int, int] | None = None,
) -> None:
    _require_active_cleanup_lock(cleanup_guard, candidate.parent)
    cleanup_root = _scan_cleanup_namespace(
        candidate,
        expected,
        transaction_id,
        cleanup_guard,
    )
    if cleanup_root is None:
        return
    if cleanup_root == candidate:
        tombstone = candidate.parent / _cleanup_tombstone_name(transaction_id)
        _require_active_cleanup_lock(cleanup_guard, candidate.parent)
        _renamex(candidate, tombstone, RENAME_EXCL)
        _fsync_directory(candidate.parent)
        cleanup_root = tombstone
        if _identity_or_none(cleanup_root) != expected:
            raise ValueError("cleanup tombstone identity changed")
    _remove_verified_tombstone(
        cleanup_root,
        expected,
        transaction_id,
        cleanup_guard,
    )


def atomic_install(
    staged: Path,
    target: Path,
    *,
    expected_staged: FileIdentity | None = None,
    manifest_sha256: str = "",
    stamp_sha256: str = "",
    verifier: Callable[[Path], None] | None = None,
) -> None:
    with _runtime_transaction_lock(target, create=True) as cleanup_guard:
        if cleanup_guard is None:
            raise ValueError("install transaction lock is unavailable")
        _recover_install_transactions_locked(target, cleanup_guard)
        _atomic_install_locked(
            staged,
            target,
            expected_staged=expected_staged,
            manifest_sha256=manifest_sha256,
            stamp_sha256=stamp_sha256,
            verifier=verifier,
            cleanup_guard=cleanup_guard,
        )


def _atomic_install_locked(
    staged: Path,
    target: Path,
    *,
    expected_staged: FileIdentity | None,
    manifest_sha256: str,
    stamp_sha256: str,
    verifier: Callable[[Path], None] | None,
    cleanup_guard: tuple[int, int],
) -> None:
    _require_active_cleanup_lock(cleanup_guard, target.parent)
    if staged.parent != target.parent:
        raise ValueError("staged runtime and target must share a parent directory")
    expected_staged = expected_staged or file_identity(staged)
    if expected_staged.file_type != stat.S_IFDIR:
        raise ValueError("staged runtime must be a directory")
    _require_identity(staged, expected_staged, "staged runtime identity changed")
    target_before = _identity_or_none(target)
    transaction_directory = target.parent / ".sidecar-runtime-transactions"
    transaction_directory.mkdir(mode=0o700, exist_ok=True)
    transaction_metadata = transaction_directory.lstat()
    if (
        not stat.S_ISDIR(transaction_metadata.st_mode)
        or transaction_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(transaction_metadata.st_mode) != 0o700
    ):
        raise ValueError("install transaction directory owner/mode is not trusted")
    transaction_id = uuid.uuid4().hex
    journal = transaction_directory / f"{transaction_id}.json"
    aborted = transaction_directory / f"{transaction_id}.aborted"
    finished = transaction_directory / f"{transaction_id}.finished"
    document = {
        "manifest_sha256": manifest_sha256,
        "schema": 1,
        "stamp_sha256": stamp_sha256,
        "staged": staged.name,
        "staged_identity": _identity_document(expected_staged),
        "target": target.name,
        "target_identity": _identity_document(target_before),
    }
    _write_new_file(
        journal,
        (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )

    def mark_aborted() -> None:
        _write_new_file(aborted, b'{"schema":1}\n')

    def rollback_verified_publication(previous: FileIdentity | None) -> None:
        _require_identity(target, expected_staged, "published runtime changed before rollback")
        if previous is None:
            if _identity_or_none(staged) is not None:
                raise ValueError("staged path occupied before rollback")
            _renamex(target, staged, RENAME_EXCL)
        else:
            _require_identity(staged, previous, "replaced target changed before rollback")
            _renamex(staged, target, RENAME_SWAP)
        _fsync_directory(target.parent)
        if _identity_or_none(target) != previous or _identity_or_none(staged) != expected_staged:
            raise ValueError("runtime rollback identity check failed")
        mark_aborted()

    _require_identity(staged, expected_staged, "staged runtime identity changed")
    if target_before is None:
        _renamex(staged, target, RENAME_EXCL)
    else:
        _require_identity(target, target_before, "target runtime identity changed")
        _renamex(staged, target, RENAME_SWAP)
    _fsync_directory(target.parent)

    target_after = _identity_or_none(target)
    if target_after != expected_staged:
        if target_before is not None and _identity_or_none(staged) == target_before:
            unexpected_target = target_after
            _renamex(staged, target, RENAME_SWAP)
            _fsync_directory(target.parent)
            if (
                _identity_or_none(target) != target_before
                or _identity_or_none(staged) != unexpected_target
            ):
                raise ValueError("runtime rollback identity check failed")
            mark_aborted()
        elif target_before is None and target_after is not None and not staged.exists():
            _renamex(target, staged, RENAME_EXCL)
            _fsync_directory(target.parent)
            if _identity_or_none(target) is not None or _identity_or_none(staged) != target_after:
                raise ValueError("runtime rollback identity check failed")
            mark_aborted()
        raise ValueError("published runtime identity changed")
    if target_before is not None:
        replaced_target = _identity_or_none(staged)
        if replaced_target != target_before:
            rollback_verified_publication(replaced_target)
            raise ValueError("target runtime identity changed during publication")

    try:
        if manifest_sha256:
            manifest_path = target / ".runtime-manifest.json"
            manifest = manifest_path.read_bytes()
            if hashlib.sha256(manifest).hexdigest() != manifest_sha256:
                raise ValueError("published runtime manifest changed")
            verify_runtime_manifest_bytes(target / "python", manifest)
        if stamp_sha256:
            stamp = (target / ".runtime-stamp.json").read_bytes()
            if hashlib.sha256(stamp).hexdigest() != stamp_sha256:
                raise ValueError("published runtime stamp changed")
        if verifier is not None:
            verifier(target)
    except Exception as error:
        try:
            rollback_verified_publication(target_before)
        except Exception as rollback_error:
            error.add_note(f"runtime rollback failed: {rollback_error}")
            raise error from rollback_error
        raise

    commit = transaction_directory / f"{transaction_id}.committed"
    _write_new_file(
        commit,
        (json.dumps({"schema": 1, "target_identity": list(expected_staged)}) + "\n").encode(),
    )
    _require_identity(target, expected_staged, "committed runtime target identity changed")
    if target_before is not None:
        if _identity_or_none(staged) != target_before:
            raise ValueError("replaced runtime identity changed before cleanup")
        _retire_cleanup_candidate(
            staged,
            target_before,
            transaction_id,
            cleanup_guard,
        )
    _write_finished_transaction(finished, "committed", expected_staged)
    _reclaim_completed_install_transactions_locked(target, cleanup_guard)


def _parse_identity(value: object) -> FileIdentity | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, int) for item in value)
    ):
        raise ValueError("invalid install transaction identity")
    return FileIdentity(*value)


def _read_transaction(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError(f"invalid install transaction file: {path}")
        payload = b""
        while len(payload) <= 64 * 1024:
            block = os.read(descriptor, 8192)
            if not block:
                break
            payload += block
        if len(payload) > 64 * 1024:
            raise ValueError("install transaction is too large")
        document = json.loads(payload)
    finally:
        os.close(descriptor)
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("invalid install transaction document")
    return document


def _transaction_claim_name(
    transaction_id: str,
    suffix: str,
    identity: FileIdentity,
) -> str:
    if suffix not in {"json", "committed", "aborted", "finished"}:
        raise ValueError("invalid transaction cleanup member")
    return (
        f".sidecar-runtime.transaction-claim.{_validate_transaction_id(transaction_id)}."
        f"{suffix}.{identity.device:016x}.{identity.inode:016x}."
        f"{identity.file_type:08x}"
    )


def _bounded_scandir_entries(
    descriptor: int,
    *,
    limit_error: str,
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if len(entries) >= MAX_CLEANUP_NAMESPACE_ENTRIES:
                raise ValueError(limit_error)
            entries.append(entry)
    return entries


def _scan_install_transaction_namespace(
    transaction_directory: Path,
    cleanup_guard: tuple[int, int],
) -> dict[str, dict[str, TransactionMember]]:
    _require_active_cleanup_lock(cleanup_guard, transaction_directory.parent)
    directory_identity = file_identity(transaction_directory)
    descriptor = os.open(
        transaction_directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if _identity_from_stat(os.fstat(descriptor)) != directory_identity:
            raise ValueError("install transaction directory identity changed while scanning")
        entries = _bounded_scandir_entries(
            descriptor,
            limit_error="install transaction namespace entry limit exceeded",
        )
        families: dict[str, dict[str, TransactionMember]] = {}
        for entry in entries:
            name = entry.name
            if name == ".cleanup.lock":
                continue
            claimed = False
            match = INSTALL_TRANSACTION_MEMBER_PATTERN.fullmatch(name)
            if match is not None:
                transaction_id, suffix = match.groups()
            elif name.startswith(".sidecar-runtime.transaction-claim."):
                claim = TRANSACTION_CLAIM_PATTERN.fullmatch(name)
                if claim is None:
                    raise ValueError("malformed transaction cleanup claim")
                transaction_id, suffix = claim.group(1), claim.group(2)
                encoded_identity = FileIdentity(
                    int(claim.group(3), 16),
                    int(claim.group(4), 16),
                    int(claim.group(5), 16),
                )
                claimed = True
            else:
                raise ValueError(f"unexpected install transaction entry: {name}")
            _validate_transaction_id(transaction_id)
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            identity = _identity_from_stat(metadata)
            if claimed and identity != encoded_identity:
                raise ValueError("transaction cleanup claim identity changed")
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or _xattrs(transaction_directory / name)
            ):
                raise ValueError("install transaction member owner/mode is not trusted")
            if _descriptor_entry_identity(descriptor, name) != identity:
                raise ValueError("install transaction member identity changed while scanning")
            family = families.setdefault(transaction_id, {})
            if suffix in family:
                raise ValueError("duplicate install transaction family member")
            family[suffix] = TransactionMember(
                transaction_directory / name,
                identity,
                claimed,
            )
        _require_identity(
            transaction_directory,
            directory_identity,
            "install transaction directory identity changed while scanning",
        )
        return families
    finally:
        os.close(descriptor)


def _read_bound_transaction_member(member: TransactionMember) -> dict[str, object]:
    document = _read_transaction(member.path)
    _require_identity(
        member.path,
        member.identity,
        "install transaction member identity changed while reading",
    )
    return document


def _completed_transaction_outcome(
    transaction_id: str,
    members: dict[str, TransactionMember],
    target: Path,
) -> str:
    if "finished" not in members:
        raise ValueError("completed install transaction lacks finished evidence")
    finished = _read_bound_transaction_member(members["finished"])
    outcome = finished.get("outcome")
    if outcome not in {"committed", "aborted"}:
        raise ValueError("invalid finished install transaction")
    required = {"json", outcome, "finished"}
    if set(members) != required:
        raise ValueError("incomplete completed install transaction family")
    journal = _read_bound_transaction_member(members["json"])
    if journal.get("target") != target.name:
        raise ValueError("install transaction targets another runtime")
    expected_staged = _parse_identity(journal.get("staged_identity"))
    target_before = _parse_identity(journal.get("target_identity"))
    if expected_staged is None:
        raise ValueError("install transaction lacks staged identity")
    outcome_document = _read_bound_transaction_member(members[outcome])
    finished_target = _parse_identity(finished.get("target_identity"))
    if outcome == "committed":
        outcome_target = _parse_identity(outcome_document.get("target_identity"))
        if outcome_target != expected_staged or finished_target != expected_staged:
            raise ValueError("committed transaction identity is invalid")
    elif finished_target != target_before:
        raise ValueError("aborted transaction identity is invalid")
    return outcome


def _capture_transaction_family_member(
    descriptor: int,
    transaction_id: str,
    suffix: str,
    member: TransactionMember,
    cleanup_guard: tuple[int, int],
) -> TransactionMember:
    _require_active_cleanup_lock(cleanup_guard)
    if member.claimed:
        return member
    destination_name = _transaction_claim_name(transaction_id, suffix, member.identity)
    if _descriptor_entry_identity(descriptor, member.path.name) != member.identity:
        raise ValueError("install transaction member changed before capture")
    _renameatx(
        descriptor,
        member.path.name,
        descriptor,
        destination_name,
        RENAME_EXCL,
    )
    if _descriptor_entry_identity(descriptor, destination_name) != member.identity:
        raise ValueError("transaction cleanup claim identity changed")
    os.fsync(descriptor)
    return TransactionMember(member.path.with_name(destination_name), member.identity, True)


def _resume_completed_transaction_reclaim_locked(
    transaction_id: str,
    members: dict[str, TransactionMember],
    target: Path,
    cleanup_guard: tuple[int, int],
) -> None:
    _require_active_cleanup_lock(cleanup_guard, target.parent)
    claimed = {suffix for suffix, member in members.items() if member.claimed}
    outcome: str
    if not claimed or len(members) == 3:
        outcome = _completed_transaction_outcome(transaction_id, members, target)
    else:
        if any(not member.claimed for member in members.values()) or "finished" not in members:
            raise ValueError("incomplete transaction reclaim state")
        finished = _read_bound_transaction_member(members["finished"])
        raw_outcome = finished.get("outcome")
        if not isinstance(raw_outcome, str) or raw_outcome not in {"committed", "aborted"}:
            raise ValueError("invalid finished install transaction")
        outcome = raw_outcome
        if not set(members) <= {"json", outcome, "finished"}:
            raise ValueError("invalid transaction reclaim member set")

    transaction_directory = target.parent / ".sidecar-runtime-transactions"
    descriptor = os.open(
        transaction_directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if len(members) == 3:
            for suffix in ("json", outcome, "finished"):
                members[suffix] = _capture_transaction_family_member(
                    descriptor,
                    transaction_id,
                    suffix,
                    members[suffix],
                    cleanup_guard,
                )
        elif any(not member.claimed for member in members.values()):
            raise ValueError("incomplete transaction reclaim capture")

        for suffix in ("json", outcome, "finished"):
            member = members.get(suffix)
            if member is None:
                continue
            if not member.claimed:
                raise ValueError("transaction member was not captured before deletion")
            _remove_private_cleanup_claim(
                descriptor,
                member.path.name,
                member.identity,
                directory=False,
                cleanup_guard=cleanup_guard,
            )
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reclaim_completed_install_transactions_locked(
    target: Path,
    cleanup_guard: tuple[int, int],
) -> None:
    transaction_directory = target.parent / ".sidecar-runtime-transactions"
    families = _scan_install_transaction_namespace(transaction_directory, cleanup_guard)
    for transaction_id, members in sorted(families.items()):
        if any(member.claimed for member in members.values()):
            _resume_completed_transaction_reclaim_locked(
                transaction_id,
                members,
                target,
                cleanup_guard,
            )
    families = _scan_install_transaction_namespace(transaction_directory, cleanup_guard)
    completed: list[tuple[int, str, dict[str, TransactionMember]]] = []
    for transaction_id, members in families.items():
        if "finished" not in members:
            continue
        _completed_transaction_outcome(transaction_id, members, target)
        completed.append(
            (
                members["finished"].path.lstat().st_mtime_ns,
                transaction_id,
                members,
            )
        )
    completed.sort(reverse=True)
    for _completed_ns, transaction_id, members in completed[COMPLETED_TRANSACTION_RETENTION:]:
        _resume_completed_transaction_reclaim_locked(
            transaction_id,
            members,
            target,
            cleanup_guard,
        )


def recover_install_transactions(target: Path) -> None:
    with _runtime_transaction_lock(target, create=False) as cleanup_guard:
        if cleanup_guard is None:
            return
        _recover_install_transactions_locked(target, cleanup_guard)


def _recover_install_transactions_locked(
    target: Path,
    cleanup_guard: tuple[int, int],
) -> None:
    _require_active_cleanup_lock(cleanup_guard, target.parent)
    transaction_directory = target.parent / ".sidecar-runtime-transactions"
    if not transaction_directory.exists():
        return
    if transaction_directory.is_symlink() or not transaction_directory.is_dir():
        raise ValueError("invalid install transaction directory")
    transaction_metadata = transaction_directory.lstat()
    if (
        transaction_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(transaction_metadata.st_mode) != 0o700
    ):
        raise ValueError("install transaction directory owner/mode is not trusted")
    families = _scan_install_transaction_namespace(transaction_directory, cleanup_guard)
    for transaction_id, members in sorted(families.items()):
        if any(member.claimed for member in members.values()):
            _resume_completed_transaction_reclaim_locked(
                transaction_id,
                members,
                target,
                cleanup_guard,
            )
    families = _scan_install_transaction_namespace(transaction_directory, cleanup_guard)
    for transaction_id, members in sorted(families.items()):
        if "json" not in members:
            raise ValueError("install transaction family lacks journal")
        journal = members["json"].path
        committed = transaction_directory / f"{transaction_id}.committed"
        aborted = transaction_directory / f"{transaction_id}.aborted"
        finished = transaction_directory / f"{transaction_id}.finished"
        document = _read_transaction(journal)
        if document.get("target") != target.name:
            raise ValueError("install transaction targets another runtime")
        staged_name = document.get("staged")
        if not isinstance(staged_name, str) or not staged_name.startswith(
            ".sidecar-runtime.staged."
        ):
            raise ValueError("invalid install transaction staging path")
        if Path(staged_name).name != staged_name:
            raise ValueError("invalid install transaction staging path")
        staged = target.parent / staged_name
        expected_staged = _parse_identity(document.get("staged_identity"))
        target_before = _parse_identity(document.get("target_identity"))
        if expected_staged is None:
            raise ValueError("install transaction lacks staged identity")
        target_now = _identity_or_none(target)
        staged_now = _identity_or_none(staged)
        manifest_sha256 = document.get("manifest_sha256")
        stamp_sha256 = document.get("stamp_sha256", "")
        if not isinstance(manifest_sha256, str) or not isinstance(stamp_sha256, str):
            raise ValueError("invalid install transaction manifest")

        if finished.exists():
            finished_document = _read_transaction(finished)
            outcome = finished_document.get("outcome")
            finished_target = _parse_identity(finished_document.get("target_identity"))
            if committed.exists() and outcome == "committed" and finished_target == expected_staged:
                _read_transaction(committed)
                if (
                    _scan_cleanup_namespace(
                        staged,
                        target_before,
                        transaction_id,
                        cleanup_guard,
                    )
                    is not None
                ):
                    raise ValueError("finished transaction retains a cleanup root")
                continue
            if aborted.exists() and outcome == "aborted" and finished_target == target_before:
                _read_transaction(aborted)
                if (
                    _scan_cleanup_namespace(
                        staged,
                        expected_staged,
                        transaction_id,
                        cleanup_guard,
                    )
                    is not None
                ):
                    raise ValueError("finished transaction retains a cleanup root")
                continue
            raise ValueError("invalid finished install transaction")

        if committed.exists():
            commit_document = _read_transaction(committed)
            committed_target = _parse_identity(commit_document.get("target_identity"))
            if committed_target != expected_staged:
                raise ValueError("committed transaction identity is invalid")
            if target_now != committed_target:
                raise ValueError("committed runtime target identity changed")
            if target_before is not None:
                _retire_cleanup_candidate(
                    staged,
                    target_before,
                    transaction_id,
                    cleanup_guard,
                )
            _write_finished_transaction(finished, "committed", expected_staged)
            continue
        if aborted.exists():
            _read_transaction(aborted)
            if target_now != target_before:
                raise ValueError("aborted runtime target identity changed")
            if staged_now is not None:
                if staged_now != expected_staged:
                    raise ValueError("aborted cleanup candidate identity changed")
            _retire_cleanup_candidate(
                staged,
                expected_staged,
                transaction_id,
                cleanup_guard,
            )
            _write_finished_transaction(finished, "aborted", target_before)
            continue

        if target_now == expected_staged:
            manifest = (target / ".runtime-manifest.json").read_bytes()
            if manifest_sha256 and hashlib.sha256(manifest).hexdigest() != manifest_sha256:
                raise ValueError("recovered runtime manifest changed")
            verify_runtime_manifest_bytes(target / "python", manifest)
            if stamp_sha256:
                stamp = (target / ".runtime-stamp.json").read_bytes()
                if hashlib.sha256(stamp).hexdigest() != stamp_sha256:
                    raise ValueError("recovered runtime stamp changed")
            _write_new_file(
                committed,
                (
                    json.dumps({"schema": 1, "target_identity": list(expected_staged)}) + "\n"
                ).encode(),
            )
            _require_identity(target, expected_staged, "committed runtime target identity changed")
            if target_before is not None:
                _retire_cleanup_candidate(
                    staged,
                    target_before,
                    transaction_id,
                    cleanup_guard,
                )
            _write_finished_transaction(finished, "committed", expected_staged)
            continue

        if target_now == target_before and staged_now == expected_staged:
            _write_new_file(aborted, b'{"schema":1}\n')
            if _identity_or_none(target) != target_before:
                raise ValueError("aborted runtime target identity changed")
            _retire_cleanup_candidate(
                staged,
                expected_staged,
                transaction_id,
                cleanup_guard,
            )
            _write_finished_transaction(finished, "aborted", target_before)
            continue
        if target_before is not None and staged_now == target_before and target_now is not None:
            _renamex(staged, target, RENAME_SWAP)
            _fsync_directory(target.parent)
            _write_new_file(aborted, b'{"schema":1}\n')
            continue
        if target_before is None and staged_now is None and target_now is not None:
            _renamex(target, staged, RENAME_EXCL)
            _fsync_directory(target.parent)
            _write_new_file(aborted, b'{"schema":1}\n')
            continue
        raise ValueError(f"ambiguous install transaction state: {journal.name}")
    _reclaim_completed_install_transactions_locked(target, cleanup_guard)


def _require_system_tool(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
    ):
        raise ValueError(f"untrusted system tool: {path}")


def _open_cached_archive(cache: Path, archive_name: str, expected_sha256: str) -> int | None:
    try:
        descriptor = os.open(cache / archive_name, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > DEFAULT_MAX_ARCHIVE_BYTES
            or _fd_sha256(descriptor) != expected_sha256
        ):
            os.close(descriptor)
            return None
        _require_identity(
            cache / archive_name,
            _identity_from_stat(metadata),
            "cached archive path identity changed",
        )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _terminate_download_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def _download_archive(
    cache: Path,
    archive_name: str,
    url: str,
    expected_sha256: str,
    lock_descriptors: tuple[int, int],
) -> int:
    _require_active_cleanup_lock(lock_descriptors, cache)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Python runtime archive URL must use github.com over HTTPS")
    _require_system_tool(SYSTEM_CURL)
    cache_fd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = f".{archive_name}.download.{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=cache_fd,
    )
    temporary_identity = _identity_from_stat(os.fstat(descriptor))
    try:
        process = subprocess.Popen(
            [
                str(SYSTEM_CURL),
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--retry",
                "3",
                "--silent",
                "--show-error",
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sanitized_build_environment(cache / ".download-home"),
            pass_fds=lock_descriptors,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        streams = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
        stderr = bytearray()
        transferred = 0
        deadline = time.monotonic() + 300
        try:
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(str(SYSTEM_CURL), 300)
                ready, _, _ = select.select(list(streams), [], [], min(remaining, 1.0))
                if not ready:
                    continue
                for stream_fd in ready:
                    block = os.read(stream_fd, COPY_CHUNK_SIZE)
                    if not block:
                        streams.pop(stream_fd, None)
                        continue
                    if streams[stream_fd] == "stderr":
                        if len(stderr) < 1024 * 1024:
                            stderr.extend(block[: 1024 * 1024 - len(stderr)])
                        continue
                    if transferred + len(block) > DEFAULT_MAX_ARCHIVE_BYTES:
                        _terminate_download_process(process)
                        raise ValueError("archive resource limit exceeded: compressed size")
                    transferred += len(block)
                    offset = 0
                    while offset < len(block):
                        offset += os.write(descriptor, block[offset:])
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:
            _terminate_download_process(process)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        if returncode != 0:
            detail = bytes(stderr).decode(errors="replace").strip()
            raise OSError(f"Python runtime download failed: {detail}")
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if metadata.st_size > DEFAULT_MAX_ARCHIVE_BYTES:
            raise ValueError("archive resource limit exceeded: compressed size")
        if _fd_sha256(descriptor) != expected_sha256:
            raise ValueError("archive checksum mismatch")
        try:
            os.link(
                temporary_name,
                archive_name,
                src_dir_fd=cache_fd,
                dst_dir_fd=cache_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass
        _remove_private_cleanup_claim(
            cache_fd,
            temporary_name,
            temporary_identity,
            directory=False,
            cleanup_guard=lock_descriptors,
        )
        os.fsync(cache_fd)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        try:
            _remove_private_cleanup_claim(
                cache_fd,
                temporary_name,
                temporary_identity,
                directory=False,
                cleanup_guard=lock_descriptors,
            )
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(cache_fd)


def _open_or_download_archive(
    cache: Path,
    archive_name: str,
    url: str,
    expected_sha256: str,
    lock_descriptors: tuple[int, int],
) -> int:
    descriptor = _open_cached_archive(cache, archive_name, expected_sha256)
    if descriptor is not None:
        return descriptor
    return _download_archive(cache, archive_name, url, expected_sha256, lock_descriptors)


def _run_required(
    command: list[str],
    *,
    environment: dict[str, str],
    lock_descriptors: tuple[int, int],
    timeout: int = 300,
) -> str:
    result = subprocess.run(
        command,
        env=environment,
        pass_fds=lock_descriptors,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OSError(f"command failed ({result.returncode}): {command[0]}: {detail}")
    return result.stdout


def _read_bound_regular(path: Path, *, max_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > max_bytes
            or _xattrs(path)
        ):
            raise ValueError(f"untrusted regular file: {path}")
        payload = b""
        while len(payload) <= max_bytes:
            block = os.read(descriptor, min(COPY_CHUNK_SIZE, max_bytes + 1 - len(payload)))
            if not block:
                break
            payload += block
        after = os.fstat(descriptor)
        if len(payload) > max_bytes or len(payload) != after.st_size:
            raise ValueError(f"file resource limit exceeded: {path}")
        if _file_observation(before) != _file_observation(after):
            raise ValueError(f"file changed while reading: {path}")
        _require_identity(path, _identity_from_stat(before), f"file path changed: {path}")
        return payload
    finally:
        os.close(descriptor)


def _validate_locked_requirements(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("locked requirements must be UTF-8") from error
    if "\x00" in text or "\r" in text:
        raise ValueError("locked requirements contain unsafe characters")
    logical = ""
    requirements = 0
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical = f"{logical} {stripped}".strip()
        if logical.endswith("\\"):
            logical = logical[:-1].rstrip()
            continue
        if (
            logical.startswith("-")
            or "://" in logical
            or " @ " in logical
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^ ;]+", logical)
            or "--hash=sha256:" not in logical
        ):
            raise ValueError("locked requirements contain an unsafe or unhashed entry")
        requirements += 1
        logical = ""
    if logical:
        raise ValueError("locked requirements end with an incomplete entry")
    if requirements == 0:
        raise ValueError("locked requirements are empty")
    return hashlib.sha256(payload).hexdigest()


def validate_wheelhouse(
    wheelhouse_root: Path,
    requirements_path: Path,
    lock_path: Path,
) -> Wheelhouse:
    if not wheelhouse_root.is_absolute():
        raise ValueError("wheelhouse root must be absolute")
    root_metadata = wheelhouse_root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
        or _xattrs(wheelhouse_root)
    ):
        raise ValueError("wheelhouse root owner/mode is not trusted")
    requirements = _read_bound_regular(requirements_path, max_bytes=16 * 1024 * 1024)
    requirements_sha256 = _validate_locked_requirements(requirements)
    lock_sha256 = _file_sha256(lock_path)
    wheelhouse = wheelhouse_root / requirements_sha256
    wheelhouse_metadata = wheelhouse.lstat()
    if (
        not stat.S_ISDIR(wheelhouse_metadata.st_mode)
        or wheelhouse_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(wheelhouse_metadata.st_mode) & 0o022
        or _xattrs(wheelhouse)
    ):
        raise ValueError("content-addressed wheelhouse owner/mode is not trusted")
    manifest_path = wheelhouse / ".wheelhouse-manifest.json"
    manifest = _read_bound_regular(manifest_path, max_bytes=4 * 1024 * 1024)
    try:
        document = json.loads(manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid wheelhouse manifest") from error
    if not isinstance(document, dict) or set(document) != {
        "files",
        "lock_sha256",
        "requirements_sha256",
        "schema",
    }:
        raise ValueError("invalid wheelhouse manifest")
    if (
        document.get("schema") != 1
        or document.get("requirements_sha256") != requirements_sha256
        or document.get("lock_sha256") != lock_sha256
    ):
        raise ValueError("wheelhouse manifest does not match uv.lock requirements")
    records = document.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("wheelhouse manifest contains no wheels")
    expected_names: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "sha256", "size"}:
            raise ValueError("invalid wheelhouse file record")
        name = record.get("name")
        expected_sha256 = record.get("sha256")
        expected_size = record.get("size")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl", name)
            or name in expected_names
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not isinstance(expected_size, int)
            or expected_size < 1
        ):
            raise ValueError("invalid wheelhouse file record")
        expected_names.add(name)
        wheel_path = wheelhouse / name
        metadata = wheel_path.lstat()
        if metadata.st_size != expected_size:
            raise ValueError(f"wheelhouse file size mismatch: {name}")
        if _file_sha256(wheel_path) != expected_sha256:
            raise ValueError(f"wheelhouse file checksum mismatch: {name}")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or _xattrs(wheel_path)
        ):
            raise ValueError(f"untrusted wheelhouse file: {name}")
    actual_names = {path.name for path in wheelhouse.iterdir()}
    if actual_names != expected_names | {manifest_path.name}:
        raise ValueError("wheelhouse contains undeclared files")
    return Wheelhouse(
        path=wheelhouse,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        requirements_sha256=requirements_sha256,
        lock_sha256=lock_sha256,
    )


def _ensure_private_wheelhouse_root(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        pass
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _xattrs(path)
    ):
        raise ValueError("wheelhouse root owner/mode is not private")


def _locked_requirement_hashes(requirements: bytes) -> set[str]:
    _validate_locked_requirements(requirements)
    hashes = set(re.findall(rb"--hash=sha256:([0-9a-f]{64})(?=\s|$)", requirements))
    if not hashes:
        raise ValueError("locked requirements contain no SHA256 hashes")
    return {value.decode("ascii") for value in hashes}


def _export_deterministic_requirements(
    uv_tool: SealedTool,
    repo: Path,
    workspace: Path,
    *,
    environment: dict[str, str],
    lock_descriptors: tuple[int, ...],
) -> tuple[Path, bytes]:
    outputs = [workspace / "requirements-serve.txt", workspace / "requirements-check.txt"]
    base_arguments = [
        "export",
        "--project",
        str(repo),
        "--frozen",
        "--offline",
        "--extra",
        "serve",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--format",
        "requirements.txt",
    ]
    payloads = []
    for index, output in enumerate(outputs, start=1):
        _run_sealed_tool(
            uv_tool,
            [*base_arguments, "--output-file", str(output)],
            environment=environment,
            lock_descriptors=lock_descriptors,
            phase=f"deterministic export {index}",
        )
        payloads.append(_read_bound_regular(output, max_bytes=16 * 1024 * 1024))
    if payloads[0] != payloads[1]:
        raise ValueError("uv export did not produce deterministic requirements")
    _validate_locked_requirements(payloads[0])
    return outputs[0], payloads[0]


def _harden_and_record_wheels(
    directory: Path,
    locked_hashes: set[str],
) -> list[dict[str, object]]:
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _xattrs(directory)
    ):
        raise ValueError("wheel staging directory owner/mode is not private")
    entries = sorted(directory.iterdir(), key=lambda path: os.fsencode(path.name))
    if not entries or len(entries) > DEFAULT_MAX_WHEELHOUSE_FILES:
        raise ValueError("wheelhouse file count is outside the allowed range")
    records: list[dict[str, object]] = []
    total_size = 0
    folded_names: set[str] = set()
    for path in entries:
        if not WHEEL_NAME_PATTERN.fullmatch(path.name):
            raise ValueError(f"wheel download produced a non-wheel entry: {path.name}")
        folded = unicodedata.normalize("NFC", path.name).casefold()
        if folded in folded_names:
            raise ValueError(f"wheel download produced a colliding name: {path.name}")
        folded_names.add(folded)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            identity = _identity_from_stat(before)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"wheel is not a regular file: {path.name}")
            if before.st_nlink != 1:
                raise ValueError(f"wheel is not singly linked: {path.name}")
            if before.st_uid not in {0, os.geteuid()}:
                raise ValueError(f"wheel owner is not trusted: {path.name}")
            if stat.S_IMODE(before.st_mode) & 0o022:
                raise ValueError(f"wheel is group/world writable: {path.name}")
            if before.st_size < 1 or before.st_size > DEFAULT_MAX_WHEEL_BYTES:
                raise ValueError(f"wheel size is outside the allowed range: {path.name}")
            if _xattrs(path):
                raise ValueError(f"wheel has untrusted extended attributes: {path.name}")
            digest = _fd_sha256(descriptor)
            after_hash = os.fstat(descriptor)
            if _file_observation(before) != _file_observation(after_hash):
                raise ValueError(f"wheel changed while hashing: {path.name}")
            _require_identity(path, identity, f"wheel path changed while hashing: {path.name}")
            if digest not in locked_hashes:
                raise ValueError(f"wheel hash is not locked: {path.name}")
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            hardened = os.fstat(descriptor)
            if (
                _identity_from_stat(hardened) != identity
                or hardened.st_size != before.st_size
                or stat.S_IMODE(hardened.st_mode) != 0o400
            ):
                raise ValueError(f"wheel changed while hardening: {path.name}")
            if _fd_sha256(descriptor) != digest:
                raise ValueError(f"wheel content changed while hardening: {path.name}")
            _require_identity(path, identity, f"wheel path changed while hardening: {path.name}")
            if _xattrs(path):
                raise ValueError(f"wheel has untrusted extended attributes: {path.name}")
        finally:
            os.close(descriptor)
        total_size += before.st_size
        if total_size > DEFAULT_MAX_WHEELHOUSE_BYTES:
            raise ValueError("wheelhouse expanded size exceeds the allowed limit")
        records.append({"name": path.name, "sha256": digest, "size": before.st_size})
    _fsync_directory(directory)
    return records


def _validate_existing_prefetch(
    wheelhouse_root: Path,
    requirements_path: Path,
    lock_path: Path,
) -> Wheelhouse:
    try:
        return validate_wheelhouse(wheelhouse_root, requirements_path, lock_path)
    except (OSError, ValueError) as error:
        raise ValueError(f"conflicting existing wheelhouse directory: {error}") from error


def _prefetch_input_document(
    repo: Path,
    python_path: Path,
    python_version: str,
    uv_path: Path,
    uv_version: str,
) -> dict[str, object]:
    repo = repo.resolve(strict=True)
    python_path = python_path.resolve(strict=True)
    uv_path = uv_path.resolve(strict=True)
    return {
        "lock_sha256": hashlib.sha256(
            _read_bound_regular(repo / "uv.lock", max_bytes=64 * 1024 * 1024)
        ).hexdigest(),
        "pyproject_sha256": hashlib.sha256(
            _read_bound_regular(repo / "pyproject.toml", max_bytes=4 * 1024 * 1024)
        ).hexdigest(),
        "python_path": str(python_path),
        "python_sha256": hashlib.sha256(
            _read_bound_regular(python_path, max_bytes=128 * 1024 * 1024)
        ).hexdigest(),
        "python_version": python_version,
        "repo": str(repo),
        "schema": 1,
        "uv_path": str(uv_path),
        "uv_sha256": hashlib.sha256(
            _read_bound_regular(uv_path, max_bytes=128 * 1024 * 1024)
        ).hexdigest(),
        "uv_version": uv_version,
    }


def _prefetch_input_bytes(
    repo: Path,
    python_path: Path,
    python_version: str,
    uv_path: Path,
    uv_version: str,
) -> bytes:
    document = _prefetch_input_document(
        repo,
        python_path,
        python_version,
        uv_path,
        uv_version,
    )
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _canonical_prefetch_input_bytes(document: object) -> bytes:
    expected_keys = {
        "lock_sha256",
        "pyproject_sha256",
        "python_path",
        "python_sha256",
        "python_version",
        "repo",
        "schema",
        "uv_path",
        "uv_sha256",
        "uv_version",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise ValueError("invalid prefetch input document")
    if type(document.get("schema")) is not int or document.get("schema") != 1:
        raise ValueError("invalid prefetch input document")
    for key in ("lock_sha256", "pyproject_sha256", "python_sha256", "uv_sha256"):
        value = document.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("invalid prefetch input document")
    for key in ("repo", "python_path", "uv_path"):
        value = document.get(key)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "\n" in value
            or not os.path.isabs(value)
            or os.path.normpath(value) != value
        ):
            raise ValueError("invalid prefetch input document")
    if (
        document.get("python_version") != PINNED_PYTHON_VERSION
        or document.get("uv_version") != PINNED_UV_VERSION
    ):
        raise ValueError("invalid prefetch input document")
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _prefetch_transaction_id(input_payload: bytes) -> str:
    try:
        document = json.loads(input_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid prefetch input document") from error
    canonical_payload = _canonical_prefetch_input_bytes(document)
    if input_payload != canonical_payload:
        raise ValueError("prefetch input document is not canonical")
    return hashlib.sha256(canonical_payload).hexdigest()[:32]


def _prefetch_intent_bytes(transaction_id: str, input_payload: bytes) -> bytes:
    try:
        input_document = json.loads(input_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid prefetch input document") from error
    canonical_payload = _canonical_prefetch_input_bytes(input_document)
    if input_payload != canonical_payload:
        raise ValueError("prefetch input document is not canonical")
    input_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    if _prefetch_transaction_id(canonical_payload) != _validate_transaction_id(transaction_id):
        raise ValueError("prefetch transaction id does not match its input")
    document = {
        "input": input_document,
        "input_sha256": input_sha256,
        "schema": 1,
        "transaction_id": transaction_id,
    }
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _read_prefetch_intent(
    path: Path,
    transaction_id: str,
    *,
    expected_input_payload: bytes | None = None,
) -> FileIdentity:
    try:
        identity, payload = _read_private_prefetch_file(
            path,
            max_bytes=64 * 1024,
            error_message="invalid prefetch intent",
        )
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("invalid prefetch intent")
        input_payload = _canonical_prefetch_input_bytes(document.get("input"))
        expected = _prefetch_intent_bytes(transaction_id, input_payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid prefetch intent") from error
    if payload != expected or (
        expected_input_payload is not None and input_payload != expected_input_payload
    ):
        raise ValueError("invalid prefetch intent")
    _require_identity(path, identity, "prefetch intent identity changed")
    return identity


def _read_private_prefetch_file(
    path: Path,
    *,
    max_bytes: int,
    error_message: str,
) -> tuple[FileIdentity, bytes]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        identity = _identity_from_stat(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > max_bytes
            or _xattrs(path)
        ):
            raise ValueError(error_message)
        payload = bytearray()
        while len(payload) <= max_bytes:
            block = os.read(descriptor, min(COPY_CHUNK_SIZE, max_bytes + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        after = os.fstat(descriptor)
        if (
            len(payload) > max_bytes
            or len(payload) != after.st_size
            or _file_observation(before) != _file_observation(after)
        ):
            raise ValueError(error_message)
        _require_identity(path, identity, error_message)
        return identity, bytes(payload)
    except OSError as error:
        raise ValueError(error_message) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _prefetch_workspace_receipt_bytes(
    transaction_id: str,
    workspace_identity: FileIdentity,
    receipt_identity: FileIdentity,
) -> bytes:
    document = {
        "receipt_identity": list(receipt_identity),
        "schema": 2,
        "transaction_id": _validate_transaction_id(transaction_id),
        "workspace_identity": list(workspace_identity),
    }
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _legacy_prefetch_workspace_receipt_bytes(
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> bytes:
    document = {
        "schema": 1,
        "transaction_id": _validate_transaction_id(transaction_id),
        "workspace_identity": list(workspace_identity),
    }
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _prefetch_receipt_allocation_path(workspace: Path, transaction_id: str) -> Path:
    return workspace / (
        f".prefetch-workspace.{_validate_transaction_id(transaction_id)}.receipt.allocating"
    )


def _prefetch_receipt_candidate_name(
    transaction_id: str,
    identity: FileIdentity,
) -> str:
    if (
        identity.device < 0
        or identity.device > 0xFFFFFFFFFFFFFFFF
        or identity.inode < 0
        or identity.inode > 0xFFFFFFFFFFFFFFFF
        or identity.file_type < 0
        or identity.file_type > 0xFFFFFFFF
    ):
        raise ValueError("prefetch receipt identity exceeds its bounded format")
    return (
        f".prefetch-workspace.{_validate_transaction_id(transaction_id)}."
        f"receipt.candidate.{identity.device:016x}.{identity.inode:016x}."
        f"{identity.file_type:08x}"
    )


def _scan_prefetch_workspace_entries(workspace: Path) -> list[str]:
    expected_identity = file_identity(workspace)
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if _identity_from_stat(os.fstat(descriptor)) != expected_identity:
            raise ValueError("prefetch workspace identity changed while scanning")
        entries = _bounded_scandir_entries(
            descriptor,
            limit_error="prefetch workspace namespace entry limit exceeded",
        )
        names = [entry.name for entry in entries]
        if _identity_from_stat(os.fstat(descriptor)) != expected_identity:
            raise ValueError("prefetch workspace identity changed while scanning")
    finally:
        os.close(descriptor)
    _require_identity(
        workspace,
        expected_identity,
        "prefetch workspace identity changed while scanning",
    )
    return names


def _prefetch_receipt_state(
    workspace: Path,
    transaction_id: str,
) -> PrefetchReceiptState:
    transaction_id = _validate_transaction_id(transaction_id)
    final_name = ".prefetch-workspace.json"
    allocation_name = _prefetch_receipt_allocation_path(workspace, transaction_id).name
    final: Path | None = None
    allocation: Path | None = None
    candidates: list[tuple[Path, FileIdentity]] = []
    others: list[str] = []
    for name in _scan_prefetch_workspace_entries(workspace):
        if name == final_name:
            final = workspace / name
            continue
        if name == allocation_name:
            allocation = workspace / name
            continue
        if name.startswith(".prefetch-workspace."):
            match = PREFETCH_RECEIPT_CANDIDATE_PATTERN.fullmatch(name)
            if match is None or match.group(1) != transaction_id:
                raise ValueError("prefetch workspace identity receipt is invalid")
            candidates.append(
                (
                    workspace / name,
                    FileIdentity(
                        int(match.group(2), 16),
                        int(match.group(3), 16),
                        int(match.group(4), 16),
                    ),
                )
            )
            continue
        others.append(name)
    if (
        len(candidates) > 1
        or sum(
            value is not None
            for value in (final, allocation, candidates[0][0] if candidates else None)
        )
        > 1
    ):
        raise ValueError("multiple workspace receipt candidates")
    candidate, candidate_identity = candidates[0] if candidates else (None, None)
    return PrefetchReceiptState(
        final=final,
        allocation=allocation,
        candidate=candidate,
        candidate_identity=candidate_identity,
        other_entries=tuple(others),
    )


def _open_prefetch_receipt(
    path: Path,
    *,
    writable: bool,
    expected_identity: FileIdentity | None = None,
) -> tuple[int, FileIdentity, bytes]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW,
        )
        before = os.fstat(descriptor)
        identity = _identity_from_stat(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > 64 * 1024
            or _xattrs(path)
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise ValueError("prefetch workspace identity receipt is invalid")
        payload = bytearray()
        while len(payload) <= 64 * 1024:
            block = os.read(
                descriptor,
                min(COPY_CHUNK_SIZE, 64 * 1024 + 1 - len(payload)),
            )
            if not block:
                break
            payload.extend(block)
        after = os.fstat(descriptor)
        if (
            len(payload) > 64 * 1024
            or len(payload) != after.st_size
            or _file_observation(before) != _file_observation(after)
        ):
            raise ValueError("prefetch workspace identity receipt is invalid")
        _require_identity(
            path,
            identity,
            "prefetch workspace identity receipt is invalid",
        )
        return descriptor, identity, bytes(payload)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError("prefetch workspace identity receipt is invalid") from error
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _rewrite_prefetch_receipt_prefix(
    path: Path,
    expected: bytes,
    *,
    accepted_prefixes: tuple[bytes, ...],
    expected_identity: FileIdentity | None = None,
    allow_rewrite: bool,
) -> FileIdentity:
    descriptor, identity, payload = _open_prefetch_receipt(
        path,
        writable=allow_rewrite,
        expected_identity=expected_identity,
    )
    changed = False
    try:
        if payload != expected:
            if not allow_rewrite or not any(
                prefix.startswith(payload) for prefix in accepted_prefixes
            ):
                raise ValueError("prefetch workspace identity receipt is invalid")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            offset = 0
            while offset < len(expected):
                written = os.write(descriptor, expected[offset:])
                if written <= 0:
                    raise OSError("short prefetch receipt write")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            changed = True
        if _identity_from_stat(os.fstat(descriptor)) != identity:
            raise ValueError("prefetch workspace identity receipt is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, len(expected) + 1) != expected:
            raise ValueError("prefetch workspace identity receipt is invalid")
        _require_identity(
            path,
            identity,
            "prefetch workspace identity receipt is invalid",
        )
    finally:
        os.close(descriptor)
    if changed:
        _fsync_directory(path.parent)
    return identity


def _create_prefetch_receipt_allocation(
    workspace: Path,
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> FileIdentity:
    allocation = _prefetch_receipt_allocation_path(workspace, transaction_id)
    descriptor = os.open(
        allocation,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        identity = _identity_from_stat(os.fstat(descriptor))
        expected = _prefetch_workspace_receipt_bytes(
            transaction_id,
            workspace_identity,
            identity,
        )
        offset = 0
        while offset < len(expected):
            written = os.write(descriptor, expected[offset:])
            if written <= 0:
                raise OSError("short prefetch receipt write")
            offset += written
        os.fsync(descriptor)
        if _identity_from_stat(os.fstat(descriptor)) != identity:
            raise ValueError("prefetch workspace identity receipt is invalid")
    finally:
        os.close(descriptor)
    _fsync_directory(workspace)
    _require_identity(
        allocation,
        identity,
        "prefetch workspace identity receipt is invalid",
    )
    return identity


def _prepare_prefetch_receipt_allocation(
    workspace: Path,
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> FileIdentity:
    state = _prefetch_receipt_state(workspace, transaction_id)
    if state.final is None and state.other_entries:
        raise ValueError("prefetch workspace lacks its identity receipt")
    if state.final is not None:
        return file_identity(state.final)
    if state.candidate is not None:
        assert state.candidate_identity is not None
        expected = _prefetch_workspace_receipt_bytes(
            transaction_id,
            workspace_identity,
            state.candidate_identity,
        )
        return _rewrite_prefetch_receipt_prefix(
            state.candidate,
            expected,
            accepted_prefixes=(expected,),
            expected_identity=state.candidate_identity,
            allow_rewrite=False,
        )
    if state.allocation is None:
        return _create_prefetch_receipt_allocation(
            workspace,
            transaction_id,
            workspace_identity,
        )
    allocation_identity = file_identity(state.allocation)
    expected = _prefetch_workspace_receipt_bytes(
        transaction_id,
        workspace_identity,
        allocation_identity,
    )
    return _rewrite_prefetch_receipt_prefix(
        state.allocation,
        expected,
        accepted_prefixes=(expected,),
        expected_identity=allocation_identity,
        allow_rewrite=True,
    )


def _promote_prefetch_receipt_allocation(
    workspace: Path,
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> FileIdentity:
    state = _prefetch_receipt_state(workspace, transaction_id)
    if state.final is not None:
        return file_identity(state.final)
    if state.candidate is not None:
        assert state.candidate_identity is not None
        expected = _prefetch_workspace_receipt_bytes(
            transaction_id,
            workspace_identity,
            state.candidate_identity,
        )
        return _rewrite_prefetch_receipt_prefix(
            state.candidate,
            expected,
            accepted_prefixes=(expected,),
            expected_identity=state.candidate_identity,
            allow_rewrite=False,
        )
    if state.allocation is None:
        raise ValueError("prefetch workspace identity receipt is invalid")
    allocation_identity = _prepare_prefetch_receipt_allocation(
        workspace,
        transaction_id,
        workspace_identity,
    )
    candidate = workspace / _prefetch_receipt_candidate_name(
        transaction_id,
        allocation_identity,
    )
    try:
        _renamex(state.allocation, candidate, RENAME_EXCL)
    except FileExistsError as error:
        raise ValueError("multiple workspace receipt candidates") from error
    _fsync_directory(workspace)
    _require_identity(
        candidate,
        allocation_identity,
        "prefetch workspace identity receipt is invalid",
    )
    return allocation_identity


def _publish_prefetch_workspace_receipt_candidate(
    workspace: Path,
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> FileIdentity:
    state = _prefetch_receipt_state(workspace, transaction_id)
    if state.final is not None:
        return file_identity(state.final)
    if state.candidate is None or state.candidate_identity is None:
        raise ValueError("prefetch workspace identity receipt is invalid")
    expected = _prefetch_workspace_receipt_bytes(
        transaction_id,
        workspace_identity,
        state.candidate_identity,
    )
    candidate_identity = _rewrite_prefetch_receipt_prefix(
        state.candidate,
        expected,
        accepted_prefixes=(expected,),
        expected_identity=state.candidate_identity,
        allow_rewrite=False,
    )
    final = workspace / ".prefetch-workspace.json"
    try:
        _renamex(state.candidate, final, RENAME_EXCL)
    except FileExistsError as error:
        raise ValueError("multiple workspace receipt candidates") from error
    _fsync_directory(workspace)
    _require_identity(
        final,
        candidate_identity,
        "prefetch workspace identity receipt is invalid",
    )
    return candidate_identity


def _read_prefetch_workspace_receipt(
    workspace: Path,
    transaction_id: str,
    workspace_identity: FileIdentity,
) -> FileIdentity:
    state = _prefetch_receipt_state(workspace, transaction_id)
    if state.final is None:
        raise ValueError("prefetch workspace identity receipt is invalid")
    receipt_identity = file_identity(state.final)
    expected = _prefetch_workspace_receipt_bytes(
        transaction_id,
        workspace_identity,
        receipt_identity,
    )
    legacy = _legacy_prefetch_workspace_receipt_bytes(
        transaction_id,
        workspace_identity,
    )
    return _rewrite_prefetch_receipt_prefix(
        state.final,
        expected,
        accepted_prefixes=(expected, legacy),
        expected_identity=receipt_identity,
        allow_rewrite=True,
    )


def _bind_prefetch_workspace(
    workspace: Path,
    transaction_id: str,
    cleanup_guard: tuple[int, int],
    intent: Path,
    intent_identity: FileIdentity,
) -> FileIdentity:
    _require_active_cleanup_lock(cleanup_guard, workspace.parent)
    if _read_prefetch_intent(intent, transaction_id) != intent_identity:
        raise ValueError("prefetch intent identity changed while binding workspace")
    metadata = workspace.lstat()
    workspace_identity = _identity_from_stat(metadata)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _xattrs(workspace)
    ):
        raise ValueError("prefetch workspace owner/mode is not trusted")
    _prepare_prefetch_receipt_allocation(
        workspace,
        transaction_id,
        workspace_identity,
    )
    candidate_identity = _promote_prefetch_receipt_allocation(
        workspace,
        transaction_id,
        workspace_identity,
    )
    published_identity = _publish_prefetch_workspace_receipt_candidate(
        workspace,
        transaction_id,
        workspace_identity,
    )
    receipt_identity = _read_prefetch_workspace_receipt(
        workspace,
        transaction_id,
        workspace_identity,
    )
    if candidate_identity != published_identity or published_identity != receipt_identity:
        raise ValueError("prefetch workspace identity receipt is invalid")
    _require_active_cleanup_lock(cleanup_guard, workspace.parent)
    if _read_prefetch_intent(intent, transaction_id) != intent_identity:
        raise ValueError("prefetch intent identity changed while binding workspace")
    _require_identity(
        workspace,
        workspace_identity,
        "prefetch workspace identity changed",
    )
    return workspace_identity


def _validate_published_wheelhouse_directory(
    wheelhouse_root: Path,
    root_descriptor: int,
    name: str,
    metadata: os.stat_result,
) -> FileIdentity:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("published wheelhouse entry is not a directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("published wheelhouse owner/mode is not trusted")
    expected_identity = _identity_from_stat(metadata)
    path = wheelhouse_root / name
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if _identity_from_stat(opened) != expected_identity:
            raise ValueError("published wheelhouse identity changed while opening")
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            raise ValueError("published wheelhouse owner/mode is not trusted")
        if _xattrs(path):
            raise ValueError("published wheelhouse owner/mode is not trusted")
        if _identity_from_stat(os.fstat(descriptor)) != expected_identity:
            raise ValueError("published wheelhouse identity changed while inspecting")
    finally:
        os.close(descriptor)
    if _descriptor_entry_identity(root_descriptor, name) != expected_identity:
        raise ValueError("published wheelhouse identity changed while scanning")
    _require_identity(
        path, expected_identity, "published wheelhouse identity changed while scanning"
    )
    return expected_identity


def _scan_prefetch_namespace(
    wheelhouse_root: Path,
    cleanup_guard: tuple[int, int],
) -> dict[str, dict[str, Path]]:
    _require_active_cleanup_lock(cleanup_guard, wheelhouse_root)
    root_identity = file_identity(wheelhouse_root)
    descriptor = os.open(
        wheelhouse_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        entries = _bounded_scandir_entries(
            descriptor,
            limit_error="prefetch namespace entry limit exceeded",
        )
        families: dict[str, dict[str, Path]] = {}
        published_identities: dict[str, FileIdentity] = {}
        for entry in entries:
            name = entry.name
            kind: str | None = None
            transaction_id: str | None = None
            if PUBLISHED_WHEELHOUSE_PATTERN.fullmatch(name):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                published_identities[name] = _validate_published_wheelhouse_directory(
                    wheelhouse_root,
                    descriptor,
                    name,
                    metadata,
                )
                continue
            match = PREFETCH_INTENT_PATTERN.fullmatch(name)
            if match is not None:
                transaction_id, kind = match.group(1), "intent"
            else:
                match = PREFETCH_WORKSPACE_PATTERN.fullmatch(name)
                if match is not None:
                    transaction_id, kind = match.group(1), "workspace"
                else:
                    parsed = _parse_cleanup_root_name(name)
                    if parsed is not None:
                        transaction_id, _phase, _encoded_identity = parsed
                        kind = "cleanup"
                    elif name.startswith(".prefetch."):
                        raise ValueError("malformed prefetch workspace or intent")
                    else:
                        raise ValueError(f"unexpected prefetch namespace entry: {name}")
            assert transaction_id is not None and kind is not None
            family = families.setdefault(_validate_transaction_id(transaction_id), {})
            if kind in family:
                raise ValueError("duplicate prefetch transaction member")
            family[kind] = wheelhouse_root / name
        for name, expected_identity in published_identities.items():
            if _descriptor_entry_identity(descriptor, name) != expected_identity:
                raise ValueError("published wheelhouse identity changed while scanning")
            _require_identity(
                wheelhouse_root / name,
                expected_identity,
                "published wheelhouse identity changed while scanning",
            )
        _require_identity(
            wheelhouse_root,
            root_identity,
            "wheelhouse root identity changed while scanning",
        )
        return families
    finally:
        os.close(descriptor)


def _prefetch_cleanup_workspace_identity(
    cleanup: Path,
    transaction_id: str,
    cleanup_guard: tuple[int, int],
    intent: Path,
    intent_identity: FileIdentity,
) -> FileIdentity:
    parsed = _parse_cleanup_root_name(cleanup.name)
    if parsed is None or parsed[0] != _validate_transaction_id(transaction_id):
        raise ValueError("invalid prefetch cleanup root")
    _root_transaction, phase, encoded_identity = parsed
    if encoded_identity is None:
        return _bind_prefetch_workspace(
            cleanup,
            transaction_id,
            cleanup_guard,
            intent,
            intent_identity,
        )
    if file_identity(cleanup) != encoded_identity or encoded_identity.file_type != stat.S_IFDIR:
        raise ValueError("prefetch cleanup root identity changed")
    receipt = cleanup / ".prefetch-workspace.json"
    if receipt.exists():
        if (
            _bind_prefetch_workspace(
                cleanup,
                transaction_id,
                cleanup_guard,
                intent,
                intent_identity,
            )
            != encoded_identity
        ):
            raise ValueError("prefetch cleanup receipt identity changed")
    elif phase != "d":
        raise ValueError("prefetch cleanup root lost its receipt before deletion")
    return encoded_identity


def _recover_prefetch_transactions_locked(
    wheelhouse_root: Path,
    cleanup_guard: tuple[int, int],
) -> None:
    families = _scan_prefetch_namespace(wheelhouse_root, cleanup_guard)
    for transaction_id, members in sorted(families.items()):
        intent = members.get("intent")
        workspace = members.get("workspace")
        cleanup = members.get("cleanup")
        if workspace is not None and intent is None:
            raise ValueError("foreign prefetch workspace lacks an intent")
        if workspace is not None and cleanup is not None:
            raise ValueError("multiple prefetch cleanup roots exist")
        if intent is not None:
            intent_identity = _read_prefetch_intent(intent, transaction_id)
            if workspace is not None:
                workspace_identity = _bind_prefetch_workspace(
                    workspace,
                    transaction_id,
                    cleanup_guard,
                    intent,
                    intent_identity,
                )
                _retire_cleanup_candidate(
                    workspace,
                    workspace_identity,
                    transaction_id,
                    cleanup_guard,
                )
            elif cleanup is not None:
                cleanup_identity = file_identity(cleanup)
                if cleanup_identity.file_type != stat.S_IFDIR:
                    raise ValueError("prefetch intent cleanup conflicts with live intent")
                workspace_identity = _prefetch_cleanup_workspace_identity(
                    cleanup,
                    transaction_id,
                    cleanup_guard,
                    intent,
                    intent_identity,
                )
                _retire_cleanup_candidate(
                    wheelhouse_root / f".prefetch.{transaction_id}.workspace",
                    workspace_identity,
                    transaction_id,
                    cleanup_guard,
                )
            _retire_cleanup_candidate(
                intent,
                intent_identity,
                transaction_id,
                cleanup_guard,
            )
            continue
        if cleanup is not None:
            cleanup_identity = file_identity(cleanup)
            if cleanup_identity.file_type != stat.S_IFREG:
                raise ValueError("prefetch workspace cleanup lacks its intent")
            _read_prefetch_intent(cleanup, transaction_id)
            _retire_cleanup_candidate(
                wheelhouse_root / f".prefetch.{transaction_id}.intent",
                cleanup_identity,
                transaction_id,
                cleanup_guard,
            )


def _ensure_prefetch_intent(
    path: Path,
    transaction_id: str,
    payload: bytes,
) -> FileIdentity:
    expected = _prefetch_intent_bytes(transaction_id, payload)
    try:
        identity = _write_new_file(path, expected)
    except FileExistsError:
        identity = _read_prefetch_intent(
            path,
            transaction_id,
            expected_input_payload=payload,
        )
        if _read_bound_regular(path, max_bytes=64 * 1024) != expected:
            raise ValueError("conflicting prefetch intent") from None
    return identity


def prefetch_wheelhouse(
    *,
    repo: Path,
    python_path: Path,
    python_version: str,
    uv_path: Path,
    uv_version: str,
    wheelhouse_root: Path,
) -> Wheelhouse:
    if os.uname().machine != "arm64":
        raise ValueError(f"wheelhouse prefetch requires arm64, got: {os.uname().machine}")
    repo = repo.resolve(strict=True)
    if not (repo / "pyproject.toml").is_file() or not (repo / "uv.lock").is_file():
        raise ValueError("wheelhouse prefetch requires pyproject.toml and uv.lock")
    if not wheelhouse_root.is_absolute():
        raise ValueError("wheelhouse root must be absolute")
    canonical_parent = wheelhouse_root.parent.resolve(strict=True)
    if wheelhouse_root.parent != canonical_parent:
        raise ValueError("wheelhouse root parent must be canonical")
    python_path = _canonical_executable(python_path, "Python")
    uv_path = _canonical_executable(uv_path, "uv")
    lock_path = repo / "uv.lock"
    lock = wheelhouse_root.parent / f".{wheelhouse_root.name}.prefetch.lock"
    input_payload = _prefetch_input_bytes(
        repo,
        python_path,
        python_version,
        uv_path,
        uv_version,
    )
    workspace_transaction_id = _prefetch_transaction_id(input_payload)
    intent = wheelhouse_root / f".prefetch.{workspace_transaction_id}.intent"
    workspace = wheelhouse_root / f".prefetch.{workspace_transaction_id}.workspace"
    _ensure_private_wheelhouse_root(wheelhouse_root)
    with secure_build_lock(lock, cleanup_root=wheelhouse_root) as lock_descriptors:
        _ensure_private_wheelhouse_root(wheelhouse_root)
        _recover_prefetch_transactions_locked(wheelhouse_root, lock_descriptors)
        if (
            _prefetch_input_bytes(
                repo,
                python_path,
                python_version,
                uv_path,
                uv_version,
            )
            != input_payload
        ):
            raise ValueError("prefetch inputs changed while acquiring the lock")
        intent_identity = _ensure_prefetch_intent(
            intent,
            workspace_transaction_id,
            input_payload,
        )
        workspace.mkdir(mode=0o700)
        _fsync_directory(wheelhouse_root)
        workspace_identity = file_identity(workspace)
        _bind_prefetch_workspace(
            workspace,
            workspace_transaction_id,
            lock_descriptors,
            intent,
            intent_identity,
        )
        python_tool: SealedTool | None = None
        uv_tool: SealedTool | None = None
        try:
            environment = sanitized_prefetch_environment(workspace / "home")
            python_tool = bind_prefetch_python(
                python_path,
                python_version,
                environment=environment,
                pass_fds=lock_descriptors,
            )
            uv_tool = seal_uv_tool(
                uv_path,
                workspace / "sealed/bin/uv",
                uv_version,
                pass_fds=lock_descriptors,
            )
            lock_before = _read_bound_regular(lock_path, max_bytes=64 * 1024 * 1024)
            requirements_path, requirements = _export_deterministic_requirements(
                uv_tool,
                repo,
                workspace,
                environment=environment,
                lock_descriptors=lock_descriptors,
            )
            if _read_bound_regular(lock_path, max_bytes=64 * 1024 * 1024) != lock_before:
                raise ValueError("uv.lock changed during deterministic export")
            requirements_sha256 = hashlib.sha256(requirements).hexdigest()
            destination = wheelhouse_root / requirements_sha256
            if _identity_or_none(destination) is not None:
                return _validate_existing_prefetch(
                    wheelhouse_root,
                    requirements_path,
                    lock_path,
                )

            staged = workspace / requirements_sha256
            staged.mkdir(mode=0o700)
            _run_sealed_tool(
                python_tool,
                [
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "download",
                    "--isolated",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-cache-dir",
                    "--no-deps",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--index-url",
                    PYPI_SIMPLE_INDEX,
                    "--dest",
                    str(staged),
                    "--requirement",
                    str(requirements_path),
                ],
                environment=environment,
                lock_descriptors=lock_descriptors,
                phase="locked wheel download",
                timeout=900,
            )
            records = _harden_and_record_wheels(
                staged,
                _locked_requirement_hashes(requirements),
            )
            manifest = {
                "files": records,
                "lock_sha256": hashlib.sha256(lock_before).hexdigest(),
                "requirements_sha256": requirements_sha256,
                "schema": 1,
            }
            manifest_bytes = (
                json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode()
            _write_new_file(staged / ".wheelhouse-manifest.json", manifest_bytes, 0o400)
            staged_identity = file_identity(staged)
            validate_wheelhouse(workspace, requirements_path, lock_path)
            try:
                _renamex(staged, destination, RENAME_EXCL)
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise
                return _validate_existing_prefetch(
                    wheelhouse_root,
                    requirements_path,
                    lock_path,
                )
            _fsync_directory(wheelhouse_root)
            if _identity_or_none(destination) != staged_identity:
                raise ValueError("published wheelhouse identity changed")
            return validate_wheelhouse(wheelhouse_root, requirements_path, lock_path)
        finally:
            active_error = sys.exc_info()[1]
            if uv_tool is not None:
                os.close(uv_tool.descriptor)
            if python_tool is not None:
                os.close(python_tool.descriptor)
            cleanup_error: Exception | None = None
            try:
                current_workspace = _identity_or_none(workspace)
                if current_workspace is not None:
                    if current_workspace != workspace_identity:
                        raise ValueError("prefetch workspace identity changed before cleanup")
                    _bind_prefetch_workspace(
                        workspace,
                        workspace_transaction_id,
                        lock_descriptors,
                        intent,
                        intent_identity,
                    )
                    _retire_cleanup_candidate(
                        workspace,
                        workspace_identity,
                        workspace_transaction_id,
                        lock_descriptors,
                    )
                current_intent = _read_prefetch_intent(
                    intent,
                    workspace_transaction_id,
                    expected_input_payload=input_payload,
                )
                if current_intent != intent_identity:
                    raise ValueError("prefetch intent identity changed before cleanup")
                _retire_cleanup_candidate(
                    intent,
                    intent_identity,
                    workspace_transaction_id,
                    lock_descriptors,
                )
            except Exception as error:
                cleanup_error = error
            if cleanup_error is not None:
                if active_error is None:
                    raise cleanup_error
                active_error.add_note(f"prefetch cleanup failed: {cleanup_error}")


def _project_metadata(
    pyproject: Path,
    runtime_python: SealedTool,
    environment: dict[str, str],
    lock_descriptors: tuple[int, int],
) -> dict[str, object]:
    source = """
import json
import pathlib
import sys
import tomllib

with pathlib.Path(sys.argv[1]).open("rb") as stream:
    project = tomllib.load(stream).get("project")
if not isinstance(project, dict):
    raise SystemExit("pyproject [project] table is missing")
print(json.dumps(project, ensure_ascii=False, sort_keys=True))
"""
    output = _run_sealed_tool(
        runtime_python,
        ["-I", "-B", "-c", source, str(pyproject)],
        environment=environment,
        lock_descriptors=lock_descriptors,
        phase="project metadata read",
    )
    document = json.loads(output)
    if not isinstance(document, dict) or document.get("name") != "parsing-core":
        raise ValueError("pyproject project name must be parsing-core")
    version = document.get("version")
    requires_python = document.get("requires-python")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ValueError("pyproject project version is missing or invalid")
    if not isinstance(requires_python, str) or "\n" in requires_python:
        raise ValueError("pyproject requires-python is missing or invalid")
    for key in ("dependencies",):
        values = document.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) and "\n" not in value for value in values
        ):
            raise ValueError(f"pyproject {key} is invalid")
    optional = document.get("optional-dependencies", {})
    if not isinstance(optional, dict) or not all(
        isinstance(extra, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", extra)
        and isinstance(values, list)
        and all(isinstance(value, str) and "\n" not in value for value in values)
        for extra, values in optional.items()
    ):
        raise ValueError("pyproject optional-dependencies is invalid")
    scripts = document.get("scripts", {})
    if not isinstance(scripts, dict) or not all(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
        and isinstance(value, str)
        and "\n" not in value
        for name, value in scripts.items()
    ):
        raise ValueError("pyproject scripts are invalid")
    return document


def _copy_project_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o755)
    for path in sorted(source.rglob("*"), key=lambda value: os.fsencode(value.relative_to(source))):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        output = destination / relative
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            output.mkdir(mode=0o755, exist_ok=True)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"unsupported project source node: {relative}")
        output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            opened = os.fstat(source_fd)
            while True:
                block = os.read(source_fd, COPY_CHUNK_SIZE)
                if not block:
                    break
                offset = 0
                while offset < len(block):
                    offset += os.write(output_fd, block[offset:])
            if _file_observation(opened) != _file_observation(os.fstat(source_fd)):
                raise ValueError(f"project source changed while copying: {relative}")
            _require_identity(
                path,
                _identity_from_stat(opened),
                f"project source path changed while copying: {relative}",
            )
            os.fchmod(output_fd, 0o644)
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
            os.close(source_fd)


def _record_line(path: Path, root: Path) -> str:
    digest = bytes.fromhex(_file_sha256(path))
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    relative = path.relative_to(root).as_posix()
    return f"{relative},sha256={encoded},{path.stat().st_size}\n"


def _optional_requirement(dependency: str, extra: str) -> str:
    if ";" not in dependency:
        return f'{dependency} ; extra == "{extra}"'
    requirement, marker = dependency.split(";", 1)
    return f'{requirement.strip()} ; ({marker.strip()}) and extra == "{extra}"'


def install_project_without_pep517(
    repo: Path,
    site_packages: Path,
    metadata: dict[str, object],
) -> None:
    package_source = repo / "src/parsing_core"
    package_target = site_packages / "parsing_core"
    if package_target.exists() or package_target.is_symlink():
        raise ValueError("project package already exists in staged runtime")
    _copy_project_tree(package_source, package_target)
    version = metadata["version"]
    assert isinstance(version, str)
    metadata_directory = site_packages / f"parsing_core-{version}.dist-info"
    metadata_directory.mkdir(mode=0o755)
    requires_python = metadata["requires-python"]
    assert isinstance(requires_python, str)
    dependencies = metadata.get("dependencies", [])
    optional = metadata.get("optional-dependencies", {})
    scripts = metadata.get("scripts", {})
    assert isinstance(dependencies, list)
    assert isinstance(optional, dict)
    assert isinstance(scripts, dict)
    metadata_lines = [
        "Metadata-Version: 2.3",
        "Name: parsing-core",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    metadata_lines.extend(f"Requires-Dist: {dependency}" for dependency in dependencies)
    for extra in sorted(optional):
        values = optional[extra]
        assert isinstance(extra, str) and isinstance(values, list)
        metadata_lines.append(f"Provides-Extra: {extra}")
        metadata_lines.extend(
            f"Requires-Dist: {_optional_requirement(dependency, extra)}" for dependency in values
        )
    entry_points = "[console_scripts]\n" + "".join(
        f"{name} = {target}\n" for name, target in sorted(scripts.items())
    )
    files = {
        "INSTALLER": "pdf2md-sidecar-runtime\n",
        "METADATA": "\n".join(metadata_lines) + "\n",
        "WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: pdf2md-sidecar-runtime\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        "entry_points.txt": entry_points,
        "top_level.txt": "parsing_core\n",
    }
    for name, content in files.items():
        _write_new_file(metadata_directory / name, content.encode(), 0o644)
    record_paths = [
        path
        for root in (package_target, metadata_directory)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "RECORD"
    ]
    record = "".join(_record_line(path, site_packages) for path in record_paths)
    record += f"{metadata_directory.relative_to(site_packages).as_posix()}/RECORD,,\n"
    _write_new_file(metadata_directory / "RECORD", record.encode(), 0o644)


def _remove_path(path: Path, cleanup_guard: tuple[int, int]) -> None:
    ownership = _require_active_cleanup_lock(cleanup_guard)
    candidate = _absolute_path(path)
    try:
        relative = candidate.relative_to(ownership.protected_root)
    except ValueError as error:
        raise ValueError("unpublished cleanup path escapes its protected root") from error
    if not relative.parts or not relative.parts[0].startswith(".sidecar-runtime.staged."):
        raise ValueError("unpublished cleanup path is outside a staged runtime")
    # The guard excludes cooperating PDF2MD processes. As with final claim
    # deletion, hostile same-UID replacement at the syscall boundary is outside M0.
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _filter_lines(path: Path, forbidden: tuple[str, ...]) -> None:
    if not path.is_file() or path.is_symlink():
        return
    original = path.read_text(encoding="utf-8")
    filtered = "".join(
        line
        for line in original.splitlines(keepends=True)
        if not any(value in line for value in forbidden)
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    _write_new_file(temporary, filtered.encode(), 0o644)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _canonicalize_runtime_modes(runtime: Path) -> None:
    for path in [runtime, *runtime.rglob("*")]:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if stat.S_ISDIR(metadata.st_mode):
            flags |= os.O_DIRECTORY
            canonical_mode = 0o755
        elif stat.S_ISREG(metadata.st_mode):
            executable = bool(stat.S_IMODE(metadata.st_mode) & 0o111)
            if path.suffix == ".py":
                executable = False
            elif executable and path != runtime / "bin/python3.12":
                descriptor = os.open(path, flags)
                try:
                    opened = os.fstat(descriptor)
                    if _identity_from_stat(metadata) != _identity_from_stat(opened):
                        raise ValueError(f"runtime path changed while opening: {path}")
                    executable = os.read(descriptor, 2) != b"#!"
                finally:
                    os.close(descriptor)
            canonical_mode = 0o755 if executable else 0o644
        else:
            continue

        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            identity = _identity_from_stat(opened)
            if _identity_from_stat(metadata) != identity:
                raise ValueError(f"runtime path changed while opening: {path}")
            os.fchmod(descriptor, canonical_mode)
            if _identity_from_stat(os.fstat(descriptor)) != identity:
                raise ValueError(f"runtime inode changed while setting mode: {path}")
            _require_identity(path, identity, f"runtime path changed while setting mode: {path}")
        finally:
            os.close(descriptor)


def sanitize_runtime(runtime: Path, cleanup_guard: tuple[int, int]) -> None:
    for path in sorted(runtime.rglob("__pycache__"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            _remove_path(path, cleanup_guard)
    for path in list(runtime.rglob("*.pyc")) + list(runtime.rglob("*.pyo")):
        if path.is_file() and not path.is_symlink():
            _remove_path(path, cleanup_guard)
    for path in (
        runtime / "lib/python3.12/site-packages/bin",
        runtime / "lib/python3.12/site-packages/pip",
        runtime / "share/man",
        runtime / "lib/python3.12/config-3.12-darwin",
    ):
        _remove_path(path, cleanup_guard)
    for path in runtime.glob("lib/python3.12/site-packages/**/sboms"):
        _remove_path(path, cleanup_guard)
    for path in runtime.glob("lib/python3.12/site-packages/**/direct_url.json"):
        _remove_path(path, cleanup_guard)
    _filter_lines(
        runtime / "lib/python3.12/ctypes/macholib/dyld.py",
        (
            'expanduser("~/Library/Frameworks")',
            '"/Library/Frameworks"',
            '"/Network/Library/Frameworks"',
        ),
    )
    _filter_lines(
        runtime / "lib/python3.12/site-packages/markitdown/_markitdown.py",
        ('"/usr/local/bin"', '"/opt'),
    )
    bin_directory = runtime / "bin"
    for path in bin_directory.iterdir():
        if path.name not in {"python", "python3", "python3.12"}:
            _remove_path(path, cleanup_guard)
    _canonicalize_runtime_modes(runtime)


def _runtime_version(
    runtime: Path,
    python_version: str,
    lock_descriptors: tuple[int, int],
    home: Path,
) -> SealedTool:
    _require_system_tool(SYSTEM_FILE)
    binary = runtime / "bin/python3.12"
    _harden_executable_mode(binary)
    tool = _bind_existing_tool(binary, "runtime Python")
    try:
        environment = sanitized_build_environment(home)
        _validate_sealed_tool(tool, "sealed runtime Python changed before architecture check")
        architecture = _run_required(
            [str(SYSTEM_FILE), str(binary)],
            environment=environment,
            lock_descriptors=lock_descriptors,
        )
        _validate_sealed_tool(tool, "sealed runtime Python changed after architecture check")
        if "arm64" not in architecture:
            raise ValueError("embedded Python runtime is not arm64")
        observed = _run_sealed_tool(
            tool,
            [
                "-I",
                "-B",
                "-c",
                "import platform; print(platform.python_version())",
            ],
            environment=environment,
            lock_descriptors=lock_descriptors,
            phase="version check",
        ).strip()
        if observed != python_version:
            raise ValueError(f"embedded Python version mismatch: {observed}")
        return tool
    except Exception:
        os.close(tool.descriptor)
        raise


def _stamp_bytes(
    archive_sha256: str,
    input_sha256: str,
    lock_sha256: str,
    python_version: str,
    requirements_sha256: str,
    uv_sha256: str,
    uv_version: str,
    wheelhouse_manifest_sha256: str,
) -> bytes:
    document = {
        "archive_sha256": archive_sha256,
        "input_sha256": input_sha256,
        "lock_sha256": lock_sha256,
        "python_version": python_version,
        "requirements_sha256": requirements_sha256,
        "schema": 3,
        "uv_sha256": uv_sha256,
        "uv_version": uv_version,
        "wheelhouse_manifest_sha256": wheelhouse_manifest_sha256,
    }
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _matching_existing_stamp(
    candidate: Path,
    *,
    archive_sha256: str,
    input_sha256: str,
    lock_sha256: str,
    python_version: str,
    uv_sha256: str,
    uv_version: str,
) -> bytes | None:
    try:
        payload = _read_bound_regular(candidate / ".runtime-stamp.json", max_bytes=64 * 1024)
        document = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or set(document) != {
        "archive_sha256",
        "input_sha256",
        "lock_sha256",
        "python_version",
        "requirements_sha256",
        "schema",
        "uv_sha256",
        "uv_version",
        "wheelhouse_manifest_sha256",
    }:
        return None
    expected = {
        "archive_sha256": archive_sha256,
        "input_sha256": input_sha256,
        "lock_sha256": lock_sha256,
        "python_version": python_version,
        "schema": 3,
        "uv_sha256": uv_sha256,
        "uv_version": uv_version,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        return None
    if not all(
        isinstance(document.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", document[key])
        for key in ("requirements_sha256", "wheelhouse_manifest_sha256")
    ):
        return None
    return payload


def runtime_is_valid(
    candidate: Path,
    expected_stamp: bytes,
    python_version: str,
    lock_descriptors: tuple[int, int],
) -> bool:
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return False
        allowed = {"python", ".runtime-manifest.json", ".runtime-stamp.json"}
        if {path.name for path in candidate.iterdir()} != allowed:
            return False
        stamp = candidate / ".runtime-stamp.json"
        manifest_path = candidate / ".runtime-manifest.json"
        for metadata_path in (stamp, manifest_path):
            metadata = metadata_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(metadata.st_mode) != 0o444
                or _xattrs(metadata_path)
            ):
                return False
        if stamp.read_bytes() != expected_stamp:
            return False
        manifest = manifest_path.read_bytes()
        verify_runtime_manifest_bytes(candidate / "python", manifest)
        if os.readlink(candidate / "python/bin/python") != "python3.12":
            return False
        if os.readlink(candidate / "python/bin/python3") != "python3.12":
            return False
        if not (candidate / "python/lib/python3.12/os.py").is_file():
            return False
        with tempfile.TemporaryDirectory(
            prefix="pdf2md-runtime-validation-", dir=candidate.parent
        ) as home:
            runtime_python = _runtime_version(
                candidate / "python", python_version, lock_descriptors, Path(home)
            )
            os.close(runtime_python.descriptor)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return True


LAUNCHER = r"""#!/bin/bash -p
set -euo pipefail

export PATH="/usr/bin:/bin"

script_dir="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")" && pwd)"
resources="$script_dir/../Resources"
runtime="$resources/python-runtime"
python="$runtime/bin/python3"
support="$HOME/Library/Application Support/PDF2MD"
arguments=("$@")
parent_pid=""
socket_fd=""
session_token_fd=""

while (( $# > 0 )); do
  case "$1" in
    --parent-pid)
      (( $# >= 2 )) || { echo "missing --parent-pid value" >&2; exit 64; }
      parent_pid="$2"
      shift 2
      ;;
    --socket-fd)
      (( $# >= 2 )) || { echo "missing --socket-fd value" >&2; exit 64; }
      socket_fd="$2"
      shift 2
      ;;
    --session-token-fd)
      (( $# >= 2 )) || { echo "missing --session-token-fd value" >&2; exit 64; }
      session_token_fd="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [[ ! "$parent_pid" =~ ^[1-9][0-9]*$ || "$parent_pid" -le 1 ]]; then
  echo "invalid --parent-pid value" >&2
  exit 64
fi
if [[ ! "$socket_fd" =~ ^[0-9]+$ || "$socket_fd" -le 2 ]]; then
  echo "invalid --socket-fd value" >&2
  exit 64
fi
if [[ ! "$session_token_fd" =~ ^[0-9]+$ || "$session_token_fd" -le 2 ]]; then
  echo "invalid --session-token-fd value" >&2
  exit 64
fi

if [[ ! -x "$python" || ! -d "$runtime/lib/python3.12" ]]; then
  echo "bundled Python runtime is incomplete" >&2
  exit 70
fi

/bin/mkdir -p "$support/data" "$support/cache" "$support/tmp" "$support/logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$resources/src:$runtime/lib/python3.12/site-packages"
export XDG_DATA_HOME="$support/data"
export XDG_CACHE_HOME="$support/cache"
export TMPDIR="$support/tmp"
export PDF2MD_RESOURCES="$resources"
export PDF2MD_VISION_HELPER="${PDF2MD_VISION_HELPER:-$resources/vision-ocr}"

exec "$python" -s -m parsing_core.serving.lifecycle "${arguments[@]}"
"""


def publish_launcher(app: Path) -> None:
    binaries = app / "src-tauri/binaries"
    binaries.mkdir(parents=True, exist_ok=True)
    launcher = binaries / "python3"
    temporary = binaries / f".python3.{uuid.uuid4().hex}"
    _write_new_file(temporary, LAUNCHER.encode(), 0o755)
    os.replace(temporary, launcher)
    link = binaries / "python3-aarch64-apple-darwin"
    link_temporary = binaries / f".python3-aarch64-apple-darwin.{uuid.uuid4().hex}"
    os.symlink("python3", link_temporary)
    os.replace(link_temporary, link)
    _fsync_directory(binaries)


def prepare_runtime(
    *,
    repo: Path,
    app: Path,
    prepare_script: Path,
    helper: Path,
    cache: Path,
    archive_name: str,
    archive_url: str,
    archive_sha256: str,
    python_version: str,
    uv_path: Path,
    uv_version: str,
    wheelhouse_root: Path,
) -> None:
    if os.uname().machine != "arm64":
        raise ValueError(f"embedded Python runtime requires arm64, got: {os.uname().machine}")
    repo = repo.resolve(strict=True)
    app = app.resolve(strict=True)
    if app.parent != repo or prepare_script.resolve(strict=True).parent != app / "scripts":
        raise ValueError("invalid repository layout for sidecar preparation")
    if helper.resolve(strict=True).parent != app / "scripts":
        raise ValueError("invalid sidecar helper location")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_metadata = cache.lstat()
    if (
        not stat.S_ISDIR(cache_metadata.st_mode)
        or cache_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(cache_metadata.st_mode) & 0o022
    ):
        raise ValueError("sidecar cache owner/mode is not trusted")
    target_parent = app / "src-tauri"
    target_parent.mkdir(parents=True, exist_ok=True)
    target = target_parent / "sidecar-runtime"
    lock = cache / ".pdf2md-sidecar-runtime.lock"
    with secure_build_lock(lock, cleanup_root=cache) as lock_descriptors:
        recover_install_transactions(target)
        tool_cleanup_id = uuid.uuid4().hex
        tool_sandbox = Path(
            tempfile.mkdtemp(prefix=f".sidecar-tools.{tool_cleanup_id}.", dir=target_parent)
        )
        tool_sandbox_identity = file_identity(tool_sandbox)
        staged: Path | None = None
        staged_cleanup_id: str | None = None
        staged_cleanup_identity: FileIdentity | None = None
        archive_fd = -1
        uv_tool: SealedTool | None = None
        try:
            sealed_uv = tool_sandbox / "bin/uv"
            uv_tool = seal_uv_tool(
                uv_path,
                sealed_uv,
                uv_version,
                pass_fds=lock_descriptors,
            )
            input_sha256 = build_input_digest(repo, prepare_script, helper)
            lock_sha256 = _file_sha256(repo / "uv.lock")
            reusable_stamp = _matching_existing_stamp(
                target,
                archive_sha256=archive_sha256,
                input_sha256=input_sha256,
                lock_sha256=lock_sha256,
                python_version=python_version,
                uv_sha256=uv_tool.sha256,
                uv_version=uv_version,
            )
            if reusable_stamp is not None and runtime_is_valid(
                target, reusable_stamp, python_version, lock_descriptors
            ):
                publish_launcher(app)
                return

            build_home = tool_sandbox / "build-home"
            environment = sanitized_build_environment(build_home)
            requirements = tool_sandbox / "requirements-serve.txt"
            _run_sealed_tool(
                uv_tool,
                [
                    "export",
                    "--project",
                    str(repo),
                    "--frozen",
                    "--offline",
                    "--extra",
                    "serve",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-header",
                    "--no-annotate",
                    "--format",
                    "requirements.txt",
                    "--output-file",
                    str(requirements),
                ],
                environment=environment,
                lock_descriptors=lock_descriptors,
                phase="export",
            )
            wheelhouse = validate_wheelhouse(
                wheelhouse_root,
                requirements,
                repo / "uv.lock",
            )
            expected_stamp = _stamp_bytes(
                archive_sha256,
                input_sha256,
                wheelhouse.lock_sha256,
                python_version,
                wheelhouse.requirements_sha256,
                uv_tool.sha256,
                uv_version,
                wheelhouse.manifest_sha256,
            )

            archive_fd = _open_or_download_archive(
                cache,
                archive_name,
                archive_url,
                archive_sha256,
                lock_descriptors,
            )
            staged_cleanup_id = uuid.uuid4().hex
            staged = Path(
                tempfile.mkdtemp(
                    prefix=f".sidecar-runtime.staged.{staged_cleanup_id}.",
                    dir=target_parent,
                )
            )
            staged_cleanup_identity = file_identity(staged)
            extract_verified_archive_fd(
                archive_fd,
                staged,
                archive_sha256,
                expected_destination=staged_cleanup_identity,
            )
            runtime = staged / "python"
            if os.readlink(runtime / "bin/python") != "python3.12":
                raise ValueError("embedded Python link is invalid: python")
            if os.readlink(runtime / "bin/python3") != "python3.12":
                raise ValueError("embedded Python link is invalid: python3")
            runtime_python = _runtime_version(runtime, python_version, lock_descriptors, build_home)
            try:
                site_packages = runtime / "lib/python3.12/site-packages"
                site_packages.mkdir(parents=True, exist_ok=True)
                _run_sealed_tool(
                    runtime_python,
                    [
                        "-I",
                        "-B",
                        "-m",
                        "pip",
                        "install",
                        "--isolated",
                        "--disable-pip-version-check",
                        "--no-compile",
                        "--no-deps",
                        "--no-index",
                        "--find-links",
                        str(wheelhouse.path),
                        "--require-hashes",
                        "--only-binary=:all:",
                        "--target",
                        str(site_packages),
                        "--requirement",
                        str(requirements),
                    ],
                    environment=environment,
                    lock_descriptors=lock_descriptors,
                    phase="dependency install",
                    timeout=900,
                )
                project_metadata = _project_metadata(
                    repo / "pyproject.toml",
                    runtime_python,
                    environment,
                    lock_descriptors,
                )
            finally:
                os.close(runtime_python.descriptor)
            install_project_without_pep517(repo, site_packages, project_metadata)
            with _runtime_transaction_lock(target, create=True) as cleanup_guard:
                if cleanup_guard is None:
                    raise ValueError("install transaction lock is unavailable")
                sanitize_runtime(runtime, cleanup_guard)
            if build_input_digest(repo, prepare_script, helper) != input_sha256:
                raise ValueError("build inputs changed during sidecar preparation")
            _fsync_tree(runtime)
            manifest = runtime_manifest_bytes(runtime)
            _write_new_file(staged / ".runtime-manifest.json", manifest, 0o444)
            _write_new_file(staged / ".runtime-stamp.json", expected_stamp, 0o444)
            if not runtime_is_valid(staged, expected_stamp, python_version, lock_descriptors):
                raise ValueError("staged sidecar runtime validation failed")
            staged_identity = file_identity(staged)

            def verify_published(candidate: Path) -> None:
                if not runtime_is_valid(
                    candidate,
                    expected_stamp,
                    python_version,
                    lock_descriptors,
                ):
                    raise ValueError("published sidecar runtime validation failed")

            atomic_install(
                staged,
                target,
                expected_staged=staged_identity,
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                stamp_sha256=hashlib.sha256(expected_stamp).hexdigest(),
                verifier=verify_published,
            )
            staged = None
            if not runtime_is_valid(target, expected_stamp, python_version, lock_descriptors):
                raise ValueError("published sidecar runtime validation failed")
            publish_launcher(app)
        finally:
            active_error = sys.exc_info()[1]
            if archive_fd >= 0:
                os.close(archive_fd)
            if uv_tool is not None:
                os.close(uv_tool.descriptor)
            cleanup_error: Exception | None = None
            try:
                with _runtime_transaction_lock(target, create=True) as cleanup_guard:
                    if cleanup_guard is None:
                        raise ValueError("install transaction lock is unavailable")
                    if staged is not None:
                        if staged_cleanup_id is None or staged_cleanup_identity is None:
                            raise ValueError("staged runtime cleanup evidence is incomplete")
                        if _identity_or_none(staged) != staged_cleanup_identity:
                            raise ValueError("staged runtime changed before guarded cleanup")
                        _retire_cleanup_candidate(
                            staged,
                            staged_cleanup_identity,
                            staged_cleanup_id,
                            cleanup_guard,
                        )
                    if _identity_or_none(tool_sandbox) != tool_sandbox_identity:
                        raise ValueError("tool sandbox changed before guarded cleanup")
                    _retire_cleanup_candidate(
                        tool_sandbox,
                        tool_sandbox_identity,
                        tool_cleanup_id,
                        cleanup_guard,
                    )
            except Exception as error:
                cleanup_error = error
            if cleanup_error is not None:
                if active_error is None:
                    raise cleanup_error
                active_error.add_note(f"protected build cleanup failed: {cleanup_error}")


def run_with_lock(lock: Path, command: list[str]) -> int:
    with secure_build_lock(lock) as descriptors:
        return subprocess.run(command, check=False, pass_fds=descriptors).returncode


def validate_archive(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        # This is an isolated standard temporary directory, not a runtime,
        # transaction, or prefetch resource.
        with tempfile.TemporaryDirectory(prefix="pdf2md-archive-check-") as temporary:
            extract_verified_archive_fd(
                descriptor,
                Path(temporary) / "runtime",
                _fd_sha256(descriptor),
            )
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("emit-launcher")
    validate = subparsers.add_parser("validate-archive")
    validate.add_argument("archive", type=Path)
    install = subparsers.add_parser("atomic-install")
    install.add_argument("staged", type=Path)
    install.add_argument("target", type=Path)
    locked = subparsers.add_parser("run-with-lock")
    locked.add_argument("lock", type=Path)
    locked.add_argument("command_args", nargs=argparse.REMAINDER)
    prefetch = subparsers.add_parser("prefetch-wheelhouse")
    prefetch.add_argument("--repo", required=True, type=Path)
    prefetch.add_argument("--python-path", required=True, type=Path)
    prefetch.add_argument(
        "--python-version",
        required=True,
        choices=(PINNED_PYTHON_VERSION,),
    )
    prefetch.add_argument("--uv-path", required=True, type=Path)
    prefetch.add_argument(
        "--uv-version",
        required=True,
        choices=(PINNED_UV_VERSION,),
    )
    prefetch.add_argument("--wheelhouse-root", required=True, type=Path)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--repo", required=True, type=Path)
    prepare.add_argument("--app", required=True, type=Path)
    prepare.add_argument("--prepare-script", required=True, type=Path)
    prepare.add_argument("--helper", required=True, type=Path)
    prepare.add_argument("--cache", required=True, type=Path)
    prepare.add_argument("--archive-name", required=True)
    prepare.add_argument("--archive-url", required=True)
    prepare.add_argument("--archive-sha256", required=True)
    prepare.add_argument("--python-version", required=True)
    prepare.add_argument("--uv-path", required=True, type=Path)
    prepare.add_argument("--uv-version", required=True)
    prepare.add_argument("--wheelhouse-root", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.command == "emit-launcher":
            sys.stdout.buffer.write(LAUNCHER.encode())
        elif args.command == "validate-archive":
            validate_archive(args.archive)
        elif args.command == "atomic-install":
            atomic_install(args.staged, args.target)
        elif args.command == "run-with-lock":
            command = (
                args.command_args[1:] if args.command_args[:1] == ["--"] else args.command_args
            )
            if not command:
                raise ValueError("run-with-lock requires a command")
            return run_with_lock(args.lock, command)
        elif args.command == "prefetch-wheelhouse":
            prefetch_wheelhouse(
                repo=args.repo,
                python_path=args.python_path,
                python_version=args.python_version,
                uv_path=args.uv_path,
                uv_version=args.uv_version,
                wheelhouse_root=args.wheelhouse_root,
            )
        else:
            prepare_runtime(
                repo=args.repo,
                app=args.app,
                prepare_script=args.prepare_script,
                helper=args.helper,
                cache=args.cache,
                archive_name=args.archive_name,
                archive_url=args.archive_url,
                archive_sha256=args.archive_sha256,
                python_version=args.python_version,
                uv_path=args.uv_path,
                uv_version=args.uv_version,
                wheelhouse_root=args.wheelhouse_root,
            )
    except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
