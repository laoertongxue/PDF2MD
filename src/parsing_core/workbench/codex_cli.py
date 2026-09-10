from __future__ import annotations

import json
import os
import re
import shutil
import stat
from functools import wraps
from pathlib import Path
from typing import Any, cast

from .secure_codex import (
    SecureCodexError,
    SecureCodexRunner,
    codex_exec_prefix,
    read_executable_identity,
    resolve_official_native_from_wrapper,
    sanitize_codex_exception,
)


class CodexCliError(RuntimeError):
    pass


class _VerifiedCodexPath(str):
    selected_path: str
    native_path: str

    def __new__(cls, native_path: str, selected_path: str) -> _VerifiedCodexPath:
        value = str.__new__(cls, native_path)
        value.selected_path = selected_path
        value.native_path = native_path
        return value


_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_TASK_PACKAGE_BYTES = 4 * 1024 * 1024
_MAX_OUTPUT_FILE_BYTES = 4 * 1024 * 1024
_ROUND_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MACHO_MAGICS = frozenset(
    {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)
_SAFE_RUNNER_ERRORS = frozenset(
    {
        "codex cli cancelled",
        "codex cli failed",
        "codex cli is not available",
        "codex cli input exceeded limit",
        "codex cli output exceeded limit",
        "codex cli timed out",
    }
)


def _public_codex_cli_errors(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except CodexCliError as exc:
            message = str(exc)
            if message not in _SAFE_RUNNER_ERRORS and message not in {
                "codex cli not found",
                "codex output exceeded limit",
                "codex output file empty",
                "codex output file is invalid",
                "codex output file missing",
            }:
                message = "codex cli failed"
            sanitize_codex_exception(exc, message)
            raise

    return wrapped


@_public_codex_cli_errors
def resolve_codex_path(configured_path: str | Path | None = None) -> str:
    configured = (
        str(configured_path) if configured_path is not None else os.environ.get("CODEX_CLI_PATH")
    )
    candidate = configured if configured else shutil.which("codex")
    if not candidate:
        raise CodexCliError("codex cli not found")
    try:
        selected = str(Path(candidate).expanduser().resolve(strict=True))
        native = str(_resolve_native_codex(candidate))
        return _VerifiedCodexPath(native, selected)
    except (OSError, RuntimeError, SecureCodexError):
        raise CodexCliError("codex cli not found") from None


class CodexCliExecutor:
    @_public_codex_cli_errors
    def __init__(self, codex_path: str, run_dir: str | Path, timeout: int = 300):
        self.run_dir = Path(run_dir).expanduser()
        self._selected_codex_path = str(getattr(codex_path, "selected_path", codex_path))
        try:
            self._runner = SecureCodexRunner(
                codex_path,
                timeout=timeout,
                max_stdin_bytes=_MAX_TASK_PACKAGE_BYTES,
                max_stdout_bytes=_MAX_STDOUT_BYTES,
                max_stderr_bytes=_MAX_STDERR_BYTES,
            )
        except SecureCodexError as exc:
            raise CodexCliError(_safe_runner_error(exc)) from None
        self.codex_path = str(self._runner.path)

    def checkpoint_identity(self) -> str:
        source = self._runner.source_identity
        snapshot = self._runner.identity
        value = {
            "selected_path": self._selected_codex_path,
            "native_path": str(source.path),
            "version": self._runner.codex_version,
            "model": None,
            "configuration": codex_exec_prefix(self.codex_path)[1:],
            "limits": {
                "timeout": self._runner.timeout,
                "max_stdout_bytes": self._runner.max_stdout_bytes,
                "max_stderr_bytes": self._runner.max_stderr_bytes,
                "max_stdin_bytes": self._runner.max_stdin_bytes,
            },
            "source_identity": {
                "device": source.device,
                "inode": source.inode,
                "uid": source.uid,
                "mode": source.mode,
                "nlink": source.nlink,
                "size": source.size,
                "mtime_ns": source.mtime_ns,
                "ctime_ns": source.ctime_ns,
                "sha256": source.sha256,
            },
            "snapshot_sha256": snapshot.sha256,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @_public_codex_cli_errors
    def run(self, round_key: str, task_package: str) -> str:
        if (
            not isinstance(round_key, str)
            or _ROUND_KEY.fullmatch(round_key) is None
            or not isinstance(task_package, str)
        ):
            raise CodexCliError("codex cli failed")
        try:
            payload = task_package.encode("utf-8")
        except UnicodeError:
            raise CodexCliError("codex cli failed") from None
        if len(payload) > _MAX_TASK_PACKAGE_BYTES:
            raise CodexCliError("codex cli input exceeded limit")
        try:
            with self._runner.private_task() as task_dir:
                output_path = task_dir / f"codex-{round_key}-output.md"
                command = codex_exec_prefix(self.codex_path) + [
                    "--cd",
                    str(task_dir),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
                self._runner.run(command, payload, cwd=task_dir)
                return cast(str, _read_output_file(output_path))
        except SecureCodexError as exc:
            raise CodexCliError(_safe_runner_error(exc)) from None


def _resolve_native_codex(selected: str | Path) -> Path:
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        raise CodexCliError("codex cli not found")
    entry_info = candidate.lstat()
    resolved = candidate.resolve(strict=True)
    if stat.S_ISLNK(entry_info.st_mode):
        if not _is_official_codex_wrapper(resolved):
            raise CodexCliError("codex cli not found")
        return _validated_native(resolve_official_native_from_wrapper(resolved))
    if not stat.S_ISREG(entry_info.st_mode) or resolved != candidate:
        raise CodexCliError("codex cli not found")
    if _is_macho_executable(resolved, expected_entry=entry_info):
        return _validated_native(resolved)
    if not _is_official_codex_wrapper(resolved):
        raise CodexCliError("codex cli not found")
    return _validated_native(resolve_official_native_from_wrapper(resolved))


def _validated_native(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if resolved != path or not _is_macho_executable(resolved):
        raise CodexCliError("codex cli not found")
    read_executable_identity(resolved)
    return resolved


def _is_macho_executable(path: Path, *, expected_entry: os.stat_result | None = None) -> bool:
    fd: int | None = None
    is_macho = False
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or (
            expected_entry is not None
            and _macho_file_snapshot(expected_entry) != _macho_file_snapshot(before)
        ):
            return False
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or _macho_file_snapshot(before) != _macho_file_snapshot(opened)
            or _macho_file_snapshot(opened) != _macho_file_snapshot(after)
        ):
            is_macho = False
        else:
            is_macho = os.read(fd, 4) in _MACHO_MAGICS
    except OSError:
        is_macho = False
    finally:
        if fd is not None:
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                is_macho = False
    return is_macho


def _macho_file_snapshot(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _is_official_codex_wrapper(path: Path) -> bool:
    return (
        path.name == "codex.js"
        and path.parent.name == "bin"
        and path.parent.parent.name == "codex"
        and path.parent.parent.parent.name == "@openai"
    )


def _safe_runner_error(error: SecureCodexError) -> str:
    message = str(error)
    return message if message in _SAFE_RUNNER_ERRORS else "codex cli failed"


@_public_codex_cli_errors
def _read_output_file(path: Path) -> str:
    directory_fd: int | None = None
    output_fd: int | None = None
    payload = b""
    failure: CodexCliError | None = None
    unexpected_failure = False
    try:
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_output_stat(before)
        output_fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(output_fd)
        _validate_output_stat(opened)
        if _output_fingerprint(before) != _output_fingerprint(opened):
            raise CodexCliError("codex output file is invalid")
        if opened.st_size > _MAX_OUTPUT_FILE_BYTES:
            raise CodexCliError("codex output exceeded limit")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = _read_output_chunk(output_fd, min(64 * 1024, remaining))
            if not chunk:
                raise CodexCliError("codex output file is invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(output_fd)
        path_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _output_fingerprint(opened) != _output_fingerprint(after) or _output_fingerprint(
            opened
        ) != _output_fingerprint(path_after):
            raise CodexCliError("codex output file is invalid")
        payload = b"".join(chunks)
    except FileNotFoundError:
        failure = CodexCliError("codex output file missing")
    except CodexCliError as exc:
        failure = exc
    except OSError:
        failure = CodexCliError("codex output file is invalid")
    except BaseException:
        unexpected_failure = True
        raise
    finally:
        owned_fds: list[int] = []
        if output_fd is not None:
            owned_fds.append(output_fd)
            output_fd = None
        if directory_fd is not None:
            owned_fds.append(directory_fd)
            directory_fd = None
        close_error = _close_owned_fds(owned_fds)
        if close_error is not None and failure is None and not unexpected_failure:
            failure = CodexCliError("codex output file is invalid")
    if failure is not None:
        raise failure from None
    if not payload:
        raise CodexCliError("codex output file empty")
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        raise CodexCliError("codex output file is invalid") from None


def _validate_output_stat(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CodexCliError("codex output file is invalid")


def _output_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_output_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _close_fd_once(fd: int) -> None:
    os.close(fd)


def _close_owned_fds(fds: list[int]) -> OSError | None:
    first_error: OSError | None = None
    for fd in fds:
        try:
            _close_fd_once(fd)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    return first_error
