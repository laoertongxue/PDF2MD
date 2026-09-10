from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol


class FileWriter(Protocol):
    def __call__(self, fd: int, data: memoryview, /) -> int: ...


class FdOperation(Protocol):
    def __call__(self, fd: int, /) -> None: ...


class DirectoryOpener(Protocol):
    def __call__(self, path: Path, /) -> int: ...


class FileReplacer(Protocol):
    def __call__(self, source: Path, target: Path, directory_fd: int, /) -> None: ...


class TemporaryUnlinker(Protocol):
    def __call__(self, path: Path, /) -> None: ...


class AtomicCommitError(OSError):
    """The target was replaced, but a post-commit operation failed."""

    committed = True

    def __init__(
        self,
        target: Path,
        *,
        durability_uncertain: bool,
        identity_uncertain: bool = False,
    ) -> None:
        if identity_uncertain:
            detail = "the published identity could not be confirmed"
        elif durability_uncertain:
            detail = "directory durability could not be confirmed"
        else:
            detail = "post-commit cleanup failed"
        super().__init__(f"atomic artifact was committed but {detail}: {target}")
        self.target = target
        self.durability_uncertain = durability_uncertain
        self.identity_uncertain = identity_uncertain


@dataclass
class _CommitState:
    committed: bool = False
    durability_confirmed: bool = False


def _write_file(fd: int, data: memoryview) -> int:
    return os.write(fd, data)


def sync_file_data(fd: int) -> None:
    """Flush file data, adding macOS full-device durability when available."""
    os.fsync(fd)
    if sys.platform != "darwin":
        return
    operation = getattr(fcntl, "F_FULLFSYNC", None)
    if operation is None:
        return
    try:
        fcntl.fcntl(fd, operation)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            errno.ENOTTY,
            errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        }
        if exc.errno not in unsupported:
            raise


def _create_temporary(directory_fd: int, prefix: str) -> tuple[int, str]:
    if not prefix or "/" in prefix or prefix in {".", ".."}:
        raise ValueError("atomic temporary prefix is invalid")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(128):
        name = f"{prefix}{secrets.token_hex(16)}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("unable to create atomic temporary file")


def _unlink_name(name: str, directory_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def rename_exclusive(source: Path, target: Path, directory_fd: int) -> None:
    """Rename without replacing an existing target in one pinned directory."""
    _validate_lexical_paths(source, target)
    _rename_exclusive_at(source.name, target.name, directory_fd)


def _rename_exclusive_at(source_name: str, target_name: str, directory_fd: int) -> None:
    if not source_name or not target_name or "/" in source_name or "/" in target_name:
        raise ValueError("atomic rename name is invalid")
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    function = None
    flags = 0
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        flags = 0x00000001  # RENAME_NOREPLACE
    if function is not None:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        if function(directory_fd, source, directory_fd, target, flags) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), target_name)
        return

    # Older non-Darwin platforms may not expose renameat2. The hard-link
    # fallback preserves no-replace semantics; callers still verify the moved
    # identity after the operation.
    os.link(
        source_name,
        target_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    linked_identity = _entry_identity(target_name, directory_fd)
    if _entry_identity(source_name, directory_fd) != linked_identity:
        _unlink_name(target_name, directory_fd)
        raise ValueError("atomic temporary file changed")
    os.unlink(source_name, dir_fd=directory_fd)


def write_all(fd: int, data: bytes, *, writer: FileWriter = _write_file) -> None:
    remaining = memoryview(data)
    while remaining:
        written = writer(fd, remaining)
        if written <= 0:
            raise OSError("artifact write made no progress")
        remaining = remaining[written:]


def atomic_write_bytes(
    *,
    target: Path,
    data: bytes,
    temporary_prefix: str,
    writer: FileWriter,
    sync_file: FdOperation,
    close_file: FdOperation,
    open_directory: DirectoryOpener,
    replace_file: FileReplacer,
    sync_directory: FdOperation,
) -> None:
    """Create and publish bytes relative to one pinned parent directory."""
    directory_fd: int | None = None
    temporary_fd: int | None = None
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    state = _CommitState()
    first_error: BaseException | None = None
    first_traceback: TracebackType | None = None
    try:
        directory_fd = open_directory(target.parent)
        temporary_fd, temporary_name = _create_temporary(directory_fd, temporary_prefix)
        temporary_identity = _regular_identity(os.fstat(temporary_fd), require_single_link=True)
        commit_fd = temporary_fd
        temporary_fd = None
        _commit_open_bytes(
            fd=commit_fd,
            temporary_name=temporary_name,
            temporary_identity=temporary_identity,
            target=target,
            data=data,
            writer=writer,
            sync_file=sync_file,
            close_file=close_file,
            directory_fd=directory_fd,
            replace_file=replace_file,
            sync_directory=sync_directory,
            state=state,
        )
    except BaseException as exc:
        first_error = exc
        first_traceback = exc.__traceback__

    if temporary_fd is not None:
        closing_fd = temporary_fd
        temporary_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    if (
        not state.committed
        and directory_fd is not None
        and temporary_name is not None
        and temporary_identity is not None
    ):
        try:
            _cleanup_bound_name(
                temporary_name,
                directory_fd,
                expected=temporary_identity,
            )
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    if directory_fd is not None:
        closing_fd = directory_fd
        directory_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    _raise_transaction_error(first_error, first_traceback, state, target)


def atomic_replace_bytes(
    *,
    fd: int,
    temporary: Path,
    target: Path,
    data: bytes,
    writer: FileWriter,
    sync_file: FdOperation,
    close_file: FdOperation,
    open_directory: DirectoryOpener,
    replace_file: FileReplacer,
    sync_directory: FdOperation,
    unlink_temporary: TemporaryUnlinker,
) -> None:
    """Publish a caller-created temporary after binding it to a pinned directory.

    New callers use :func:`atomic_write_bytes`, which creates the temporary
    through the pinned descriptor. This compatibility path fails closed if the
    caller's descriptor no longer names an entry in that directory.
    """
    del unlink_temporary
    _validate_lexical_paths(temporary, target)
    directory_fd: int | None = None
    owned_fd: int | None = fd
    temporary_identity: tuple[int, int] = _regular_identity(os.fstat(fd), require_single_link=True)
    state = _CommitState()
    first_error: BaseException | None = None
    first_traceback: TracebackType | None = None
    cleanup_allowed = True
    try:
        directory_fd = open_directory(target.parent)
        _validate_open_temporary(fd, temporary.name, directory_fd)
        try:
            _reject_target_alias(
                temporary.name,
                temporary_identity,
                target.name,
                directory_fd,
            )
        except ValueError:
            cleanup_allowed = False
            raise
        commit_fd = owned_fd
        owned_fd = None
        assert commit_fd is not None
        _commit_open_bytes(
            fd=commit_fd,
            temporary_name=temporary.name,
            temporary_identity=temporary_identity,
            target=target,
            data=data,
            writer=writer,
            sync_file=sync_file,
            close_file=close_file,
            directory_fd=directory_fd,
            replace_file=replace_file,
            sync_directory=sync_directory,
            state=state,
        )
    except BaseException as exc:
        first_error = exc
        first_traceback = exc.__traceback__

    if owned_fd is not None:
        closing_fd = owned_fd
        owned_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    if cleanup_allowed and not state.committed and temporary_identity is not None:
        if directory_fd is None:
            _cleanup_after_directory_open_failure(
                temporary,
                target,
                expected=temporary_identity,
            )
        else:
            try:
                _cleanup_bound_name(
                    temporary.name,
                    directory_fd,
                    expected=temporary_identity,
                )
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                    first_traceback = exc.__traceback__

    if directory_fd is not None:
        closing_fd = directory_fd
        directory_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    _raise_transaction_error(first_error, first_traceback, state, target)


def atomic_replace_file(
    *,
    temporary: Path,
    target: Path,
    close_file: FdOperation,
    open_directory: DirectoryOpener,
    replace_file: FileReplacer,
    sync_directory: FdOperation,
    unlink_temporary: TemporaryUnlinker,
) -> None:
    """Replace a target from a verified entry in one pinned directory."""
    del unlink_temporary
    _validate_lexical_paths(temporary, target)
    directory_fd: int | None = None
    temporary_fd: int | None = None
    temporary_identity: tuple[int, int] | None = None
    state = _CommitState()
    first_error: BaseException | None = None
    first_traceback: TracebackType | None = None
    cleanup_allowed = True
    try:
        temporary_fd = os.open(
            temporary,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        temporary_info = os.fstat(temporary_fd)
        temporary_identity = _regular_identity(temporary_info, require_single_link=False)
        sync_file_data(temporary_fd)
        closing_fd = temporary_fd
        temporary_fd = None
        close_file(closing_fd)
        directory_fd = open_directory(target.parent)
        try:
            _reject_target_alias(
                temporary.name,
                temporary_identity,
                target.name,
                directory_fd,
            )
        except ValueError:
            cleanup_allowed = False
            raise
        if temporary_info.st_nlink != 1:
            raise ValueError("atomic temporary file is unsafe")
        _assert_bound_identity(temporary.name, directory_fd, temporary_identity)
        replace_file(temporary, target, directory_fd)
        state.committed = True
        _assert_committed_identity(target, directory_fd, temporary_identity)
        sync_directory(directory_fd)
        state.durability_confirmed = True
    except BaseException as exc:
        first_error = exc
        first_traceback = exc.__traceback__

    if temporary_fd is not None:
        closing_fd = temporary_fd
        temporary_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    if first_error is not None and directory_fd is None and temporary_identity is not None:
        _cleanup_after_directory_open_failure(
            temporary,
            target,
            expected=temporary_identity,
        )

    if (
        cleanup_allowed
        and not state.committed
        and directory_fd is not None
        and temporary_identity is not None
    ):
        try:
            _cleanup_bound_name(
                temporary.name,
                directory_fd,
                expected=temporary_identity,
            )
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    if directory_fd is not None:
        closing_fd = directory_fd
        directory_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    _raise_transaction_error(first_error, first_traceback, state, target)


def _commit_open_bytes(
    *,
    fd: int,
    temporary_name: str,
    temporary_identity: tuple[int, int],
    target: Path,
    data: bytes,
    writer: FileWriter,
    sync_file: FdOperation,
    close_file: FdOperation,
    directory_fd: int,
    replace_file: FileReplacer,
    sync_directory: FdOperation,
    state: _CommitState,
) -> None:
    owned_fd: int | None = fd
    first_error: BaseException | None = None
    first_traceback: TracebackType | None = None
    try:
        write_all(fd, data, writer=writer)
        sync_file(fd)
        owned_fd = None
        close_file(fd)
        _assert_bound_identity(temporary_name, directory_fd, temporary_identity)
        _reject_target_alias(
            temporary_name,
            temporary_identity,
            target.name,
            directory_fd,
        )
        replace_file(target.parent / temporary_name, target, directory_fd)
        state.committed = True
        _assert_committed_identity(target, directory_fd, temporary_identity)
        sync_directory(directory_fd)
        state.durability_confirmed = True
    except BaseException as exc:
        first_error = exc
        first_traceback = exc.__traceback__

    if owned_fd is not None:
        closing_fd = owned_fd
        owned_fd = None
        try:
            close_file(closing_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                first_traceback = exc.__traceback__

    _raise_transaction_error(first_error, first_traceback, state, target)


def _raise_transaction_error(
    error: BaseException | None,
    traceback: TracebackType | None,
    state: _CommitState,
    target: Path,
) -> None:
    if error is None:
        return
    if state.committed and isinstance(error, Exception):
        if isinstance(error, AtomicCommitError):
            raise error.with_traceback(traceback)
        raise AtomicCommitError(
            target,
            durability_uncertain=not state.durability_confirmed,
        ) from error
    raise error.with_traceback(traceback)


def _validate_lexical_paths(temporary: Path, target: Path) -> None:
    if temporary.parent != target.parent:
        raise ValueError("atomic source and target must share a directory")
    if temporary.name == target.name:
        raise ValueError("atomic source and target must not alias")


def _regular_identity(
    info: os.stat_result,
    *,
    require_single_link: bool,
) -> tuple[int, int]:
    if not stat.S_ISREG(info.st_mode) or (require_single_link and info.st_nlink != 1):
        raise ValueError("atomic temporary file is unsafe")
    return info.st_dev, info.st_ino


def _entry_identity(name: str, directory_fd: int) -> tuple[int, int]:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("atomic temporary file changed") from exc
    return _regular_identity(info, require_single_link=True)


def _validate_open_temporary(fd: int, name: str, directory_fd: int) -> None:
    opened = _regular_identity(os.fstat(fd), require_single_link=True)
    if _entry_identity(name, directory_fd) != opened:
        raise ValueError("atomic temporary file changed")


def _assert_bound_identity(
    name: str,
    directory_fd: int,
    expected: tuple[int, int],
) -> None:
    if _entry_identity(name, directory_fd) != expected:
        raise ValueError("atomic temporary file changed")


def _assert_committed_identity(
    target: Path,
    directory_fd: int,
    expected: tuple[int, int],
) -> None:
    try:
        current = _entry_identity(target.name, directory_fd)
    except ValueError as exc:
        raise AtomicCommitError(
            target,
            durability_uncertain=True,
            identity_uncertain=True,
        ) from exc
    if current != expected:
        raise AtomicCommitError(
            target,
            durability_uncertain=True,
            identity_uncertain=True,
        )


def _reject_target_alias(
    temporary_name: str,
    temporary_identity: tuple[int, int],
    target_name: str,
    directory_fd: int,
) -> None:
    if temporary_name == target_name:
        raise ValueError("atomic source and target must not alias")
    try:
        target = os.stat(target_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (target.st_dev, target.st_ino) == temporary_identity:
        raise ValueError("atomic source and target must not alias")


def _cleanup_bound_name(
    name: str,
    directory_fd: int,
    *,
    expected: tuple[int, int],
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != expected:
        return
    quarantine_name = f".atomic-cleanup-{secrets.token_hex(16)}.tmp"
    try:
        _rename_exclusive_at(name, quarantine_name, directory_fd)
    except FileNotFoundError:
        return
    try:
        moved_identity = _entry_identity(quarantine_name, directory_fd)
    except ValueError:
        return
    if moved_identity != expected:
        try:
            _rename_exclusive_at(quarantine_name, name, directory_fd)
        except OSError:
            pass
        return
    _unlink_name(quarantine_name, directory_fd)


def _cleanup_after_directory_open_failure(
    temporary: Path,
    target: Path,
    *,
    expected: tuple[int, int] | None,
) -> None:
    if expected is None:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(temporary.parent, flags)
    except OSError:
        return
    try:
        try:
            target_info = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (target_info.st_dev, target_info.st_ino) == expected:
                return
        _cleanup_bound_name(temporary.name, directory_fd, expected=expected)
    except (OSError, ValueError):
        return
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass
