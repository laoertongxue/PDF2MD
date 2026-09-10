from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import selectors
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from .process_control import close_process_pipes as _close_process_pipes
from .process_control import spawn_isolated_process as _spawn_isolated_process
from .process_control import terminate_process_group as _terminate_isolated_process


class SecureCodexError(RuntimeError):
    pass


DISABLED_CODEX_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "plugins",
    "apps",
    "hooks",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "workspace_dependencies",
    "image_generation",
    "in_app_browser",
    "goals",
    "memories",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "skill_mcp_dependency_install",
    "plugin_sharing",
    "auto_compaction",
    "collaboration_modes",
    "enable_request_compression",
    "fast_mode",
    "guardian_approval",
    "mentions_v2",
    "personality",
    "remote_compaction_v2",
    "sqlite",
    "steer",
)

SUPPORTED_CODEX_VERSION = "codex-cli 0.142.1"
SUPPORTED_CODEX_SHA256 = "df8ae76bd03329da060c4e427461dcee2faa87711e1a5d47143181ce3618b25d"
SUPPORTED_CODEX_WRAPPER_SHA256 = "d3be844c45c4fd89392536e56e1010963f94785592596b50cd0c45bb8a341406"
SUPPORTED_CODEX_WRAPPER_PACKAGE_SHA256 = (
    "73295205497228cf2fff803c47f26a1997ad5b90adf4ac01870deca8099772f7"
)
SUPPORTED_CODEX_NATIVE_PACKAGE_SHA256 = (
    "db2efb461f6ce18df0a6cc22c71bf2b9cae0ef65f54cbb864a354798e014050b"
)
_OFFICIAL_WRAPPER_PACKAGE_VERSION = "0.142.1"
_OFFICIAL_NATIVE_PACKAGE_VERSION = "0.142.1-darwin-arm64"
_OFFICIAL_NATIVE_PACKAGE = "codex-darwin-arm64"
_OFFICIAL_NATIVE_RELATIVE_PATH = (
    "node_modules",
    "@openai",
    "codex-darwin-arm64",
    "vendor",
    "aarch64-apple-darwin",
    "bin",
    "codex",
)
_MAX_PACKAGE_METADATA_BYTES = 64 * 1024
_MAX_WRAPPER_BYTES = 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
MAX_CODEX_STDIN_BYTES = 4 * 1024 * 1024
MAX_CODEX_EXECUTION_SECONDS = 3600.0
_ALLOWED_ENABLED_FEATURES = frozenset(
    {"resize_all_images", "terminal_resize_reflow", "tui_app_server"}
)
_FEATURE_LINE = re.compile(
    r"([a-z][a-z0-9_]*) {2,}(stable|experimental|under development|deprecated|removed)"
    r" {2,}(true|false)\Z"
)
_ARM64_CPU_TYPE = 0x0100000C
_MAX_FAT_ARCHITECTURES = 32
_PREFLIGHT_STDOUT_BYTES = 256 * 1024
_PREFLIGHT_STDERR_BYTES = 64 * 1024
_FIXED_PATH = "/usr/bin:/bin"
_LOCALE_ENV = {"LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8"}
_VISION_SCHEMAS = frozenset({"page-transcription.json", "page-adjudication.json"})
_TEXT_OUTPUT = re.compile(r"codex-[A-Za-z0-9_-]{1,64}-output\.md\Z")
_RUNTIME_ROOT: Path | None = None
_RUNTIME_TEMP: tempfile.TemporaryDirectory[str] | None = None
_RUNTIME_ROOT_LOCK = threading.Lock()
_SNAPSHOT_LOCK = threading.Lock()
_IDENTITY_CACHE_LOCK = threading.Lock()
_PROCESS_STATE_PID = os.getpid()
_IDENTITY_CACHE_LIMIT = 16
_PRIVATE_TASK_PREFIX = "task-"
_RULE_MARKERS = ("AGENTS.md", ".git", ".codex", ".agents")
_SAFE_SECURE_ERRORS = frozenset(
    {
        "codex cli cancelled",
        "codex cli failed",
        "codex cli input exceeded limit",
        "codex cli is not available",
        "codex cli output exceeded limit",
        "codex cli timed out",
    }
)


@dataclass(frozen=True)
class ExecutableIdentity:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


_SOURCE_DIGEST_CACHE: dict[tuple[object, ...], str] = {}
_SNAPSHOT_IDENTITY_CACHE: dict[str, ExecutableIdentity] = {}


def _reset_process_state_after_fork() -> None:
    global _IDENTITY_CACHE_LOCK
    global _PROCESS_STATE_PID
    global _RUNTIME_ROOT
    global _RUNTIME_ROOT_LOCK
    global _RUNTIME_TEMP
    global _SNAPSHOT_LOCK
    inherited_temp = _RUNTIME_TEMP
    if inherited_temp is not None:
        finalizer = getattr(inherited_temp, "_finalizer", None)
        if finalizer is not None:
            try:
                finalizer.detach()
            except BaseException:
                pass
    _RUNTIME_ROOT = None
    _RUNTIME_TEMP = None
    _RUNTIME_ROOT_LOCK = threading.Lock()
    _SNAPSHOT_LOCK = threading.Lock()
    _IDENTITY_CACHE_LOCK = threading.Lock()
    _SOURCE_DIGEST_CACHE.clear()
    _SNAPSHOT_IDENTITY_CACHE.clear()
    _PROCESS_STATE_PID = os.getpid()


def _ensure_process_state() -> None:
    if _PROCESS_STATE_PID != os.getpid():
        _reset_process_state_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_state_after_fork)


def sanitize_codex_exception(error: BaseException, message: str) -> None:
    error.args = (message,)
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    if hasattr(error, "__notes__"):
        error.__notes__ = []


def _safe_secure_error(error: SecureCodexError) -> str:
    message = str(error)
    return message if message in _SAFE_SECURE_ERRORS else "codex cli failed"


def _public_secure_errors(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except SecureCodexError as exc:
            sanitize_codex_exception(exc, _safe_secure_error(exc))
            raise

    return wrapped


def _close_fd_once(fd: int) -> None:
    os.close(fd)


def _close_owned_fds(fds: Sequence[int]) -> OSError | None:
    first_error: OSError | None = None
    for fd in fds:
        try:
            _close_fd_once(fd)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    return first_error


def codex_exec_prefix(executable: str | Path) -> list[str]:
    prefix = [
        str(executable),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
    ]
    for feature in DISABLED_CODEX_FEATURES:
        prefix.extend(["--disable", feature])
    prefix.extend(["--sandbox", "read-only"])
    return prefix


@_public_secure_errors
def read_executable_identity(path: str | Path) -> ExecutableIdentity:
    fd, identity = _open_verified_executable(path)
    descriptor = fd
    fd = -1
    try:
        _close_fd_once(descriptor)
    except OSError:
        raise SecureCodexError("codex cli is not available") from None
    return identity


def validate_codex_argv(
    argv: Sequence[str], executable: Path, *, cwd: str | Path | None = None
) -> list[str]:
    normalized = list(argv)
    if (
        len(normalized) < 2
        or any(not isinstance(part, str) or not part or "\x00" in part for part in normalized)
        or normalized[0] != str(executable)
    ):
        raise SecureCodexError("codex cli failed")
    if normalized == [str(executable), "--version"]:
        return normalized

    prefix = codex_exec_prefix(executable)
    if normalized[: len(prefix)] != prefix:
        raise SecureCodexError("codex cli failed")
    tail = normalized[len(prefix) :]
    if _valid_vision_tail(tail) or _valid_text_tail(tail, cwd):
        return normalized
    raise SecureCodexError("codex cli failed")


class SecureCodexRunner:
    """Run one verified Codex byte sequence from a private immutable-by-convention copy.

    Copying from the already-open source descriptor closes package-path swap races. macOS cannot
    protect a user-owned file from a separate malicious process running as that same user; that
    same-UID active attacker is the explicit boundary of this unprivileged desktop process.
    """

    @_public_secure_errors
    def __init__(
        self,
        codex_path: str | Path,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        max_stdin_bytes: int = MAX_CODEX_STDIN_BYTES,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> None:
        self._owner_pid = os.getpid()
        self.timeout = _validated_timeout(timeout)
        _validated_deadline(deadline)
        if (
            not isinstance(max_stdout_bytes, int)
            or isinstance(max_stdout_bytes, bool)
            or not isinstance(max_stderr_bytes, int)
            or isinstance(max_stderr_bytes, bool)
            or not isinstance(max_stdin_bytes, int)
            or isinstance(max_stdin_bytes, bool)
            or max_stdout_bytes <= 0
            or max_stderr_bytes <= 0
            or max_stdin_bytes <= 0
        ):
            raise ValueError("secure codex runner limits must be positive")
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_stdin_bytes = max_stdin_bytes
        self.source_identity, self.identity = _materialize_executable_snapshot(codex_path)
        self._task_lock = threading.Lock()
        self._active_tasks: dict[Path, tuple[int, int]] = {}
        self.codex_version = self._preflight(deadline=deadline, cancel_event=cancel_event)

    @property
    def path(self) -> Path:
        return self.identity.path

    @_public_secure_errors
    def assert_current(self) -> None:
        self._assert_process_owner()
        try:
            _assert_snapshot_current(self.path, self.identity)
        except SecureCodexError:
            raise
        except Exception:
            raise SecureCodexError("codex cli is not available") from None

    @contextmanager
    def private_task(self) -> Iterator[Path]:
        self._assert_process_owner()
        task_path, task_identity = _create_private_task_directory()
        with self._task_lock:
            self._active_tasks[task_path] = task_identity
        try:
            yield task_path
        finally:
            active_error = bool(sys.exc_info()[0])
            with self._task_lock:
                self._active_tasks.pop(task_path, None)
            try:
                _remove_private_task_directory(task_path, task_identity)
            except BaseException:
                if not active_error:
                    raise SecureCodexError("codex cli failed") from None

    @_public_secure_errors
    def run(
        self,
        argv: Sequence[str],
        payload: bytes,
        *,
        cwd: str | Path,
        deadline: float | None = None,
        cancel_event: Any | None = None,
    ) -> bytes:
        self._assert_process_owner()
        _validate_stdin_payload(payload, self.max_stdin_bytes)
        execution_cwd = self._validated_task_cwd(cwd)
        normalized_argv = validate_codex_argv(argv, self.path, cwd=execution_cwd)
        effective_deadline = _effective_deadline(self.timeout, deadline)
        _check_execution_control(effective_deadline, cancel_event)
        _snapshot_boundary_hook(
            "before_spawn", source_path=self.source_identity.path, snapshot_path=self.path
        )
        return self._execute(
            normalized_argv,
            payload,
            cwd=execution_cwd,
            deadline=effective_deadline,
            cancel_event=cancel_event,
            environment=_codex_environment(execution_cwd),
            max_stdin_bytes=self.max_stdin_bytes,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
        )

    def _validated_task_cwd(self, cwd: str | Path) -> Path:
        self._assert_process_owner()
        execution_cwd = _validated_execution_cwd(cwd)
        with self._task_lock:
            expected = self._active_tasks.get(execution_cwd)
        if expected is None:
            raise SecureCodexError("codex cli failed")
        _validate_task_directory_snapshot(execution_cwd, expected)
        _reject_rule_ancestry(execution_cwd)
        return execution_cwd

    def _assert_process_owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise SecureCodexError("codex cli is not available")

    def _preflight(self, *, deadline: float | None, cancel_event: Any | None) -> str:
        timeout = min(max(self.timeout, 0.1), 5.0)
        effective_deadline = _effective_deadline(timeout, deadline)
        with self.private_task() as task_path:
            environment = _preflight_environment(task_path)
            version_payload = self._execute(
                [str(self.path), "--version"],
                b"",
                cwd=task_path,
                deadline=effective_deadline,
                cancel_event=cancel_event,
                environment=environment,
                max_stdin_bytes=self.max_stdin_bytes,
                max_stdout_bytes=1024,
                max_stderr_bytes=_PREFLIGHT_STDERR_BYTES,
            )
            version = _parse_supported_version(version_payload)
            feature_argv = [str(self.path)]
            for feature in DISABLED_CODEX_FEATURES:
                feature_argv.extend(["--disable", feature])
            feature_argv.extend(["features", "list"])
            feature_payload = self._execute(
                feature_argv,
                b"",
                cwd=task_path,
                deadline=effective_deadline,
                cancel_event=cancel_event,
                environment=environment,
                max_stdin_bytes=self.max_stdin_bytes,
                max_stdout_bytes=_PREFLIGHT_STDOUT_BYTES,
                max_stderr_bytes=_PREFLIGHT_STDERR_BYTES,
            )
            _validate_feature_output(feature_payload)
            return version

    def _execute(
        self,
        argv: list[str],
        payload: bytes,
        *,
        cwd: Path,
        deadline: float,
        cancel_event: Any | None,
        environment: dict[str, str],
        max_stdin_bytes: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> bytes:
        _validate_stdin_payload(payload, max_stdin_bytes)
        _check_execution_control(deadline, cancel_event)
        self.assert_current()
        execution_cwd = self._validated_task_cwd(cwd)
        _check_execution_control(deadline, cancel_event)
        try:
            process, process_group_id = _spawn_isolated_process(
                argv,
                cwd=execution_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                close_fds=True,
            )
        except Exception:
            raise SecureCodexError("codex cli failed") from None

        failure: SecureCodexError | None = None
        primary_base_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        stdout = b""
        try:
            stdout = _communicate_bounded(
                process,
                payload,
                deadline=deadline,
                cancel_event=cancel_event,
                max_stdin_bytes=max_stdin_bytes,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
        except SecureCodexError as exc:
            failure = exc
        except Exception:
            failure = SecureCodexError("codex cli failed")
        except BaseException as exc:
            primary_base_error = exc
        try:
            _terminate_isolated_process(process, process_group_id)
        except BaseException as exc:
            cleanup_error = exc
        try:
            _reap_process(process)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        try:
            _close_process_pipes(process)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc

        try:
            self.assert_current()
        except SecureCodexError as exc:
            if failure is None and primary_base_error is None:
                failure = exc
        except BaseException as exc:
            if primary_base_error is None:
                primary_base_error = exc
        if primary_base_error is not None:
            raise primary_base_error
        if failure is not None:
            if cleanup_error is not None:
                raise failure from cleanup_error
            raise failure
        if cleanup_error is not None:
            raise SecureCodexError("codex cli failed") from cleanup_error
        return stdout


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    process.wait(timeout=0.5)


def _valid_text_tail(tail: list[str], cwd: str | Path | None) -> bool:
    if cwd is None or len(tail) != 5:
        return False
    execution_cwd = Path(cwd)
    if tail[0] != "--cd" or tail[1] != str(execution_cwd):
        return False
    if tail[2] != "--output-last-message" or tail[4] != "-":
        return False
    output = Path(tail[3])
    return (
        output.is_absolute()
        and output.parent == execution_cwd
        and _TEXT_OUTPUT.fullmatch(output.name) is not None
    )


def _valid_vision_tail(tail: list[str]) -> bool:
    index = 0
    images: list[str] = []
    while index + 1 < len(tail) and tail[index] == "--image":
        images.append(tail[index + 1])
        index += 2
    expected_images = ["page.png", *[f"crop-{number}.png" for number in range(1, 5)]]
    if not images or len(images) > 5 or images != expected_images[: len(images)]:
        return False
    if index + 1 >= len(tail) or tail[index + 1] not in _VISION_SCHEMAS:
        return False
    return tail[index:] == [
        "--output-schema",
        tail[index + 1],
        "--output-last-message",
        "result.json",
        "-",
    ]


def _parse_supported_version(payload: bytes) -> str:
    try:
        rendered = payload.decode("ascii")
    except UnicodeError:
        raise SecureCodexError("codex cli is not available") from None
    if rendered not in {SUPPORTED_CODEX_VERSION, f"{SUPPORTED_CODEX_VERSION}\n"}:
        raise SecureCodexError("codex cli is not available")
    return SUPPORTED_CODEX_VERSION


def _validate_feature_output(payload: bytes) -> None:
    try:
        rendered = payload.decode("ascii")
    except UnicodeError:
        raise SecureCodexError("codex cli is not available") from None
    lines = rendered.splitlines()
    if not lines or rendered not in {"\n".join(lines), "\n".join(lines) + "\n"}:
        raise SecureCodexError("codex cli is not available")
    states: dict[str, bool] = {}
    for line in lines:
        match = _FEATURE_LINE.fullmatch(line)
        if match is None or match.group(1) in states:
            raise SecureCodexError("codex cli is not available")
        states[match.group(1)] = match.group(3) == "true"
    if any(states.get(feature) is not False for feature in DISABLED_CODEX_FEATURES):
        raise SecureCodexError("codex cli is not available")
    enabled = {name for name, value in states.items() if value}
    if enabled != _ALLOWED_ENABLED_FEATURES:
        raise SecureCodexError("codex cli is not available")


def _materialize_executable_snapshot(
    path: str | Path,
) -> tuple[ExecutableIdentity, ExecutableIdentity]:
    _ensure_process_state()
    source_fd, source_identity = _open_verified_executable(path)
    try:
        _snapshot_boundary_hook(
            "source_verified", source_path=source_identity.path, snapshot_path=None
        )
        root = _secure_runtime_root()
        snapshot_path = root / f"codex-{source_identity.sha256}"
        with _SNAPSHOT_LOCK:
            snapshot_identity = _SNAPSHOT_IDENTITY_CACHE.get(source_identity.sha256)
            if snapshot_identity is not None and snapshot_identity.path == snapshot_path:
                try:
                    _assert_snapshot_current(snapshot_path, snapshot_identity)
                except SecureCodexError:
                    snapshot_identity = None
                    _SNAPSHOT_IDENTITY_CACHE.pop(source_identity.sha256, None)
            else:
                snapshot_identity = None
            if snapshot_identity is None:
                try:
                    snapshot_identity = _verified_snapshot_identity(
                        snapshot_path, source_identity.sha256
                    )
                except SecureCodexError:
                    snapshot_identity = _publish_snapshot(
                        source_fd, source_identity, root, snapshot_path
                    )
                _remember_bounded(
                    _SNAPSHOT_IDENTITY_CACHE, source_identity.sha256, snapshot_identity
                )
        _snapshot_boundary_hook(
            "snapshot_published",
            source_path=source_identity.path,
            snapshot_path=snapshot_path,
        )
        return source_identity, snapshot_identity
    finally:
        active_error = bool(sys.exc_info()[0])
        descriptor = source_fd
        source_fd = -1
        try:
            _close_fd_once(descriptor)
        except OSError:
            if not active_error:
                raise SecureCodexError("codex cli is not available") from None


def _publish_snapshot(
    source_fd: int,
    source_identity: ExecutableIdentity,
    root: Path,
    snapshot_path: Path,
) -> ExecutableIdentity:
    directory_fd: int | None = None
    temp_name = (
        f".{snapshot_path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
    )
    temp_fd: int | None = None
    try:
        if snapshot_path.parent != root:
            raise SecureCodexError("codex cli is not available")
        root_before = root.lstat()
        _validate_private_directory_stat(root_before)
        directory_fd = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_opened = os.fstat(directory_fd)
        root_after = root.lstat()
        _validate_private_directory_stat(root_opened)
        _validate_private_directory_stat(root_after)
        if _stat_fingerprint(root_before) != _stat_fingerprint(root_opened) or _stat_fingerprint(
            root_opened
        ) != _stat_fingerprint(root_after):
            raise SecureCodexError("codex cli is not available")
        try:
            os.unlink(snapshot_path.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        temp_fd = os.open(
            temp_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.lseek(source_fd, 0, os.SEEK_SET)
        remaining = source_identity.size
        while remaining:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SecureCodexError("codex cli is not available")
            _write_all(temp_fd, chunk)
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            raise SecureCodexError("codex cli is not available")
        os.fsync(temp_fd)
        copied_sha256 = _fd_sha256(temp_fd)
        copied = os.fstat(temp_fd)
        if (
            copied_sha256 != source_identity.sha256
            or copied.st_size != source_identity.size
            or not stat.S_ISREG(copied.st_mode)
            or copied.st_uid != os.geteuid()
            or copied.st_nlink != 1
        ):
            raise SecureCodexError("codex cli is not available")
        os.fchmod(temp_fd, 0o500)
        os.fsync(temp_fd)
        os.replace(
            temp_name,
            snapshot_path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        assert directory_fd is not None
        os.fsync(directory_fd)
        descriptor = temp_fd
        temp_fd = None
        _close_fd_once(descriptor)
        return _verified_snapshot_identity(snapshot_path, source_identity.sha256)
    except SecureCodexError:
        raise
    except OSError:
        raise SecureCodexError("codex cli is not available") from None
    finally:
        active_error = bool(sys.exc_info()[0])
        cleanup_error: OSError | None = None
        if temp_fd is not None:
            descriptor = temp_fd
            temp_fd = None
            try:
                _close_fd_once(descriptor)
            except OSError as exc:
                cleanup_error = exc
        try:
            if directory_fd is not None:
                os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if directory_fd is not None:
            descriptor = directory_fd
            directory_fd = None
            try:
                _close_fd_once(descriptor)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None and not active_error:
            raise SecureCodexError("codex cli is not available") from None


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short executable snapshot write")
        written += count


def _secure_runtime_root() -> Path:
    global _RUNTIME_ROOT, _RUNTIME_TEMP
    _ensure_process_state()
    with _RUNTIME_ROOT_LOCK:
        if _RUNTIME_ROOT is None:
            _RUNTIME_TEMP = tempfile.TemporaryDirectory(
                prefix=f"pdf2md-codex-{os.geteuid()}-",
                dir="/private/tmp",
            )
            created = Path(_RUNTIME_TEMP.name).resolve(strict=True)
            os.chmod(created, 0o700)
            _reject_rule_ancestry(created)
            _RUNTIME_ROOT = created
        return _validated_private_directory(_RUNTIME_ROOT)


def _create_private_task_directory() -> tuple[Path, tuple[int, int]]:
    root = _secure_runtime_root()
    try:
        created = Path(tempfile.mkdtemp(prefix=_PRIVATE_TASK_PREFIX, dir=root))
        os.chmod(created, 0o700, follow_symlinks=False)
        validated = _validated_private_directory(created)
        if validated.parent != root:
            raise OSError
        _reject_rule_ancestry(validated)
        info = validated.stat()
        return validated, (info.st_dev, info.st_ino)
    except (OSError, RuntimeError, SecureCodexError):
        raise SecureCodexError("codex cli failed") from None


def _remove_private_task_directory(path: Path, expected: tuple[int, int]) -> None:
    root = _secure_runtime_root()
    if path.parent != root or not path.name.startswith(_PRIVATE_TASK_PREFIX):
        raise SecureCodexError("codex cli failed")
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != expected
        ):
            raise OSError
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError:
        raise SecureCodexError("codex cli failed") from None


def _reject_rule_ancestry(path: Path) -> None:
    current = path
    while True:
        if any(os.path.lexists(current / marker) for marker in _RULE_MARKERS):
            raise SecureCodexError("codex cli failed")
        if current.parent == current:
            return
        current = current.parent


def _validated_private_directory(path: Path) -> Path:
    candidate = _canonical_path(path)
    fd: int | None = None
    try:
        before = candidate.lstat()
        _validate_private_directory_stat(before)
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        after = candidate.lstat()
        _validate_private_directory_stat(opened)
        _validate_private_directory_stat(after)
        if _stat_fingerprint(before) != _stat_fingerprint(opened) or _stat_fingerprint(
            opened
        ) != _stat_fingerprint(after):
            raise SecureCodexError("codex cli is not available")
        return candidate
    except SecureCodexError:
        raise
    except (OSError, RuntimeError):
        raise SecureCodexError("codex cli is not available") from None
    finally:
        if fd is not None:
            active_error = bool(sys.exc_info()[0])
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                if not active_error:
                    raise SecureCodexError("codex cli is not available") from None


def _validate_private_directory_stat(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SecureCodexError("codex cli is not available")


def _verified_snapshot_identity(path: Path, expected_sha256: str) -> ExecutableIdentity:
    fd, identity = _open_executable(path, enforce_official_policy=False)
    descriptor = fd
    fd = -1
    try:
        _close_fd_once(descriptor)
    except OSError:
        raise SecureCodexError("codex cli is not available") from None
    if (
        identity.uid != os.geteuid()
        or identity.mode != 0o500
        or identity.nlink != 1
        or identity.sha256 != expected_sha256
    ):
        raise SecureCodexError("codex cli is not available")
    return identity


def _assert_snapshot_current(path: Path, expected: ExecutableIdentity) -> None:
    candidate = _canonical_path(path)
    fd: int | None = None
    try:
        link_info = candidate.lstat()
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        current = candidate.lstat()
        fingerprint = (
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            stat.S_IMODE(opened.st_mode),
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        expected_fingerprint = (
            expected.device,
            expected.inode,
            expected.uid,
            expected.mode,
            expected.nlink,
            expected.size,
            expected.mtime_ns,
            expected.ctime_ns,
        )
        if (
            fingerprint != expected_fingerprint
            or not stat.S_ISREG(link_info.st_mode)
            or _stat_fingerprint(link_info) != _stat_fingerprint(opened)
            or _stat_fingerprint(opened) != _stat_fingerprint(current)
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise SecureCodexError("codex cli is not available")
    except SecureCodexError:
        raise
    except OSError:
        raise SecureCodexError("codex cli is not available") from None
    finally:
        if fd is not None:
            active_error = bool(sys.exc_info()[0])
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                if not active_error:
                    raise SecureCodexError("codex cli is not available") from None


def _open_verified_executable(path: str | Path) -> tuple[int, ExecutableIdentity]:
    return _open_executable(path, enforce_official_policy=True)


def _open_executable(
    path: str | Path, *, enforce_official_policy: bool
) -> tuple[int, ExecutableIdentity]:
    _ensure_process_state()
    candidate = _canonical_path(path)
    fd: int | None = None
    try:
        link_info = candidate.lstat()
        if not stat.S_ISREG(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode):
            raise OSError
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(fd)
        current = candidate.lstat()
        mode = stat.S_IMODE(before.st_mode)
        if (
            _stat_fingerprint(link_info) != _stat_fingerprint(before)
            or _stat_fingerprint(before) != _stat_fingerprint(current)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_nlink != 1
            or before.st_size > _MAX_EXECUTABLE_BYTES
            or not mode & stat.S_IXUSR
            or mode & 0o022
        ):
            raise SecureCodexError("codex cli is not available")
        digest_key = _source_digest_key(candidate, before)
        with _IDENTITY_CACHE_LOCK:
            digest = _SOURCE_DIGEST_CACHE.get(digest_key)
        should_cache_digest = digest is None
        if digest is None:
            digest = _fd_sha256(fd)
        after = os.fstat(fd)
        path_after = candidate.lstat()
        if (
            _stat_fingerprint(before) != _stat_fingerprint(after)
            or _stat_fingerprint(after) != _stat_fingerprint(path_after)
            or not stat.S_ISREG(path_after.st_mode)
        ):
            raise SecureCodexError("codex cli is not available")
        identity = ExecutableIdentity(
            path=candidate,
            device=before.st_dev,
            inode=before.st_ino,
            uid=before.st_uid,
            mode=mode,
            nlink=before.st_nlink,
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=digest,
        )
        if enforce_official_policy:
            _OFFICIAL_BINARY_POLICY_PROBE(candidate, fd, identity)
        final = os.fstat(fd)
        final_path = candidate.lstat()
        if (
            _stat_fingerprint(before) != _stat_fingerprint(final)
            or _stat_fingerprint(final) != _stat_fingerprint(final_path)
            or not stat.S_ISREG(final_path.st_mode)
        ):
            raise SecureCodexError("codex cli is not available")
        if should_cache_digest:
            with _IDENTITY_CACHE_LOCK:
                _remember_bounded(_SOURCE_DIGEST_CACHE, digest_key, digest)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, identity
    except SecureCodexError:
        if fd is not None:
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                pass
        raise
    except (OSError, RuntimeError):
        if fd is not None:
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                pass
        raise SecureCodexError("codex cli is not available") from None


def resolve_official_native_from_wrapper(path: str | Path) -> Path:
    wrapper = _canonical_path(path)
    wrapper_root = wrapper.parent.parent
    if (
        wrapper != wrapper_root / "bin" / "codex.js"
        or wrapper_root.name != "codex"
        or wrapper_root.parent.name != "@openai"
        or wrapper_root.parent.parent.name != "node_modules"
    ):
        raise SecureCodexError("codex cli is not available")
    return wrapper_root.joinpath(*_OFFICIAL_NATIVE_RELATIVE_PATH)


def _verify_official_binary_policy(path: Path, fd: int, identity: ExecutableIdentity) -> None:
    wrapper_root, native_root = _official_package_roots(path)
    _validate_official_package_layout(wrapper_root, native_root, path)
    if (
        platform.system() != "Darwin"
        or platform.machine().lower() not in {"arm64", "aarch64"}
        or identity.sha256 != SUPPORTED_CODEX_SHA256
        or "arm64" not in _macho_architectures(fd, identity.size)
    ):
        raise SecureCodexError("codex cli is not available")


def _official_package_roots(path: Path) -> tuple[Path, Path]:
    try:
        native_root = path.parents[3]
        wrapper_root = path.parents[6]
    except IndexError:
        raise SecureCodexError("codex cli is not available") from None
    if (
        wrapper_root.name != "codex"
        or wrapper_root.parent.name != "@openai"
        or wrapper_root.parent.parent.name != "node_modules"
        or native_root.name != _OFFICIAL_NATIVE_PACKAGE
        or path != wrapper_root.joinpath(*_OFFICIAL_NATIVE_RELATIVE_PATH)
    ):
        raise SecureCodexError("codex cli is not available")
    return wrapper_root, native_root


def _validate_official_package_layout(wrapper_root: Path, native_root: Path, native: Path) -> None:
    triple_root = native_root / "vendor" / "aarch64-apple-darwin"
    directories = (
        wrapper_root.parent.parent,
        wrapper_root.parent,
        wrapper_root,
        wrapper_root / "bin",
        wrapper_root / "node_modules",
        wrapper_root / "node_modules" / "@openai",
        native_root,
        native_root / "vendor",
        triple_root,
        triple_root / "bin",
    )
    for directory in directories:
        _validate_trusted_directory_snapshot(directory)

    wrapper = wrapper_root / "bin" / "codex.js"
    wrapper_payload = _read_trusted_regular_file(
        wrapper,
        max_bytes=_MAX_WRAPPER_BYTES,
        require_executable=True,
    )
    wrapper_metadata, wrapper_package_digest = _read_package_metadata(wrapper_root / "package.json")
    native_metadata, native_package_digest = _read_package_metadata(native_root / "package.json")
    optional_dependencies = wrapper_metadata.get("optionalDependencies")
    if (
        hashlib.sha256(wrapper_payload).hexdigest() != SUPPORTED_CODEX_WRAPPER_SHA256
        or wrapper_package_digest != SUPPORTED_CODEX_WRAPPER_PACKAGE_SHA256
        or native_package_digest != SUPPORTED_CODEX_NATIVE_PACKAGE_SHA256
        or not isinstance(optional_dependencies, dict)
        or wrapper_metadata.get("name") != "@openai/codex"
        or wrapper_metadata.get("version") != _OFFICIAL_WRAPPER_PACKAGE_VERSION
        or wrapper_metadata.get("bin") != {"codex": "bin/codex.js"}
        or optional_dependencies.get("@openai/codex-darwin-arm64")
        != f"npm:@openai/codex@{_OFFICIAL_NATIVE_PACKAGE_VERSION}"
        or native_metadata.get("name") != "@openai/codex"
        or native_metadata.get("version") != _OFFICIAL_NATIVE_PACKAGE_VERSION
        or native_metadata.get("os") != ["darwin"]
        or native_metadata.get("cpu") != ["arm64"]
        or native != native_root / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    ):
        raise SecureCodexError("codex cli is not available")


def _read_package_metadata(path: Path) -> tuple[dict[str, Any], str]:
    payload = _read_trusted_regular_file(
        path,
        max_bytes=_MAX_PACKAGE_METADATA_BYTES,
        require_executable=False,
    )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SecureCodexError("codex cli is not available") from None
    if not isinstance(decoded, dict):
        raise SecureCodexError("codex cli is not available")
    return decoded, hashlib.sha256(payload).hexdigest()


def _read_trusted_regular_file(path: Path, *, max_bytes: int, require_executable: bool) -> bytes:
    candidate = _canonical_path(path)
    fd: int | None = None
    try:
        before = candidate.lstat()
        _validate_trusted_regular_stat(before, require_executable=require_executable)
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        path_after_open = candidate.lstat()
        _validate_trusted_regular_stat(opened, require_executable=require_executable)
        _validate_trusted_regular_stat(path_after_open, require_executable=require_executable)
        if (
            opened.st_size > max_bytes
            or _stat_fingerprint(before) != _stat_fingerprint(opened)
            or _stat_fingerprint(opened) != _stat_fingerprint(path_after_open)
        ):
            raise SecureCodexError("codex cli is not available")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise SecureCodexError("codex cli is not available")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        path_after_read = candidate.lstat()
        if _stat_fingerprint(opened) != _stat_fingerprint(after) or _stat_fingerprint(
            after
        ) != _stat_fingerprint(path_after_read):
            raise SecureCodexError("codex cli is not available")
        return b"".join(chunks)
    except SecureCodexError:
        raise
    except (OSError, RuntimeError):
        raise SecureCodexError("codex cli is not available") from None
    finally:
        if fd is not None:
            active_error = bool(sys.exc_info()[0])
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                if not active_error:
                    raise SecureCodexError("codex cli is not available") from None


def _validate_trusted_regular_stat(info: os.stat_result, *, require_executable: bool) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_nlink != 1
        or mode & 0o022
        or (require_executable and not mode & stat.S_IXUSR)
    ):
        raise SecureCodexError("codex cli is not available")


def _validate_trusted_directory_snapshot(path: Path) -> None:
    candidate = _canonical_path(path)
    fd: int | None = None
    try:
        before = candidate.lstat()
        _validate_trusted_directory_stat(before)
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        after = candidate.lstat()
        _validate_trusted_directory_stat(opened)
        _validate_trusted_directory_stat(after)
        if _stat_fingerprint(before) != _stat_fingerprint(opened) or _stat_fingerprint(
            opened
        ) != _stat_fingerprint(after):
            raise SecureCodexError("codex cli is not available")
    except SecureCodexError:
        raise
    except (OSError, RuntimeError):
        raise SecureCodexError("codex cli is not available") from None
    finally:
        if fd is not None:
            active_error = bool(sys.exc_info()[0])
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                if not active_error:
                    raise SecureCodexError("codex cli is not available") from None


def _validate_trusted_directory_stat(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise SecureCodexError("codex cli is not available")


_OFFICIAL_BINARY_POLICY_PROBE = _verify_official_binary_policy


def _macho_architectures(fd: int, size: int) -> frozenset[str]:
    if size < 8:
        return frozenset()
    magic = os.pread(fd, 4, 0)
    thin_formats = {
        b"\xcf\xfa\xed\xfe": "<",
        b"\xfe\xed\xfa\xcf": ">",
    }
    endian = thin_formats.get(magic)
    if endian is not None:
        if size < 32:
            return frozenset()
        cpu_type = struct.unpack(f"{endian}I", os.pread(fd, 4, 4))[0]
        return frozenset({"arm64"}) if cpu_type == _ARM64_CPU_TYPE else frozenset()

    fat_formats = {
        b"\xca\xfe\xba\xbe": (">", 20),
        b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32),
        b"\xbf\xba\xfe\xca": ("<", 32),
    }
    fat = fat_formats.get(magic)
    if fat is None:
        return frozenset()
    endian, entry_size = fat
    count_bytes = os.pread(fd, 4, 4)
    if len(count_bytes) != 4:
        return frozenset()
    count = struct.unpack(f"{endian}I", count_bytes)[0]
    table_size = 8 + count * entry_size
    if count == 0 or count > _MAX_FAT_ARCHITECTURES or table_size > size:
        return frozenset()
    architectures: set[str] = set()
    for index in range(count):
        cpu_bytes = os.pread(fd, 4, 8 + index * entry_size)
        if len(cpu_bytes) != 4:
            return frozenset()
        if struct.unpack(f"{endian}I", cpu_bytes)[0] == _ARM64_CPU_TYPE:
            architectures.add("arm64")
    return frozenset(architectures)


def _canonical_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise SecureCodexError("codex cli is not available")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SecureCodexError("codex cli is not available") from None
    if resolved != candidate:
        raise SecureCodexError("codex cli is not available")
    return resolved


def _validated_execution_cwd(path: str | Path) -> Path:
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise OSError
        _validate_task_directory_snapshot(candidate, None)
    except (OSError, RuntimeError, SecureCodexError):
        raise SecureCodexError("codex cli failed") from None
    return candidate


def _validate_task_directory_snapshot(candidate: Path, expected: tuple[int, int] | None) -> None:
    fd: int | None = None
    try:
        before = candidate.lstat()
        _validate_task_directory_stat(before, expected)
        fd = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(fd)
        after = candidate.lstat()
        _validate_task_directory_stat(opened, expected)
        _validate_task_directory_stat(after, expected)
        if _stat_fingerprint(before) != _stat_fingerprint(opened) or _stat_fingerprint(
            opened
        ) != _stat_fingerprint(after):
            raise SecureCodexError("codex cli failed")
    except SecureCodexError:
        raise
    except (OSError, RuntimeError):
        raise SecureCodexError("codex cli failed") from None
    finally:
        if fd is not None:
            active_error = bool(sys.exc_info()[0])
            descriptor = fd
            fd = None
            try:
                _close_fd_once(descriptor)
            except OSError:
                if not active_error:
                    raise SecureCodexError("codex cli failed") from None


def _validate_task_directory_stat(info: os.stat_result, expected: tuple[int, int] | None) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or (expected is not None and (info.st_dev, info.st_ino) != expected)
    ):
        raise SecureCodexError("codex cli failed")


def _fd_sha256(fd: int) -> str:
    try:
        before = os.fstat(fd)
        if before.st_size < 0 or before.st_size > _MAX_EXECUTABLE_BYTES:
            raise SecureCodexError("codex cli is not available")
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SecureCodexError("codex cli is not available")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise SecureCodexError("codex cli is not available")
        after = os.fstat(fd)
        if _stat_fingerprint(before) != _stat_fingerprint(after):
            raise SecureCodexError("codex cli is not available")
        return digest.hexdigest()
    except SecureCodexError:
        raise
    except OSError:
        raise SecureCodexError("codex cli is not available") from None


def _stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _source_digest_key(path: Path, info: os.stat_result) -> tuple[object, ...]:
    return (str(path), *_stat_fingerprint(info))


def _remember_bounded(mapping: dict[Any, Any], key: Any, value: Any) -> None:
    if key not in mapping and len(mapping) >= _IDENTITY_CACHE_LIMIT:
        mapping.pop(next(iter(mapping)))
    mapping[key] = value


def _codex_environment(task_path: Path) -> dict[str, str]:
    environment = {"PATH": _FIXED_PATH, **_LOCALE_ENV}
    home = _validated_environment_directory(os.environ.get("HOME"))
    if home is not None:
        environment["HOME"] = home
    environment["TMPDIR"] = str(task_path)
    return environment


def _preflight_environment(task_path: Path) -> dict[str, str]:
    home = task_path / "preflight-home"
    try:
        home.mkdir(mode=0o700)
    except OSError:
        raise SecureCodexError("codex cli is not available") from None
    return {
        "CODEX_HOME": str(home),
        "HOME": str(home),
        "TMPDIR": str(task_path),
        "PATH": _FIXED_PATH,
        **_LOCALE_ENV,
    }


def _validated_environment_directory(raw: str | None) -> str | None:
    if not raw or "\x00" in raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        return None
    return str(resolved)


def _snapshot_boundary_hook(
    name: str, *, source_path: Path, snapshot_path: Path | None = None
) -> None:
    del name, source_path, snapshot_path


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("secure codex runner limits are invalid")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_CODEX_EXECUTION_SECONDS:
        raise ValueError("secure codex runner limits are invalid")
    return timeout


def _validated_deadline(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("secure codex runner limits are invalid")
    deadline = float(value)
    if not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("secure codex runner limits are invalid")
    now = time.monotonic()
    if not math.isfinite(now) or deadline - now > MAX_CODEX_EXECUTION_SECONDS:
        raise ValueError("secure codex runner limits are invalid")
    return deadline


def _effective_deadline(timeout: float, deadline: float | None) -> float:
    bounded_timeout = _validated_timeout(timeout)
    bounded_deadline = _validated_deadline(deadline)
    local_deadline = time.monotonic() + bounded_timeout
    if not math.isfinite(local_deadline):
        raise ValueError("secure codex runner limits are invalid")
    return local_deadline if bounded_deadline is None else min(local_deadline, bounded_deadline)


def _check_execution_control(deadline: float, cancel_event: Any | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SecureCodexError("codex cli cancelled")
    if time.monotonic() >= deadline:
        raise SecureCodexError("codex cli timed out")


def _validate_stdin_payload(payload: object, max_stdin_bytes: int) -> None:
    if not isinstance(payload, bytes) or len(payload) > max_stdin_bytes:
        raise SecureCodexError("codex cli input exceeded limit")


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    deadline: float,
    cancel_event: Any | None,
    max_stdin_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> bytes:
    _validate_stdin_payload(payload, max_stdin_bytes)
    stdout_chunks: list[bytes] = []
    stdout_size = 0
    stderr_size = 0
    payload_offset = 0
    selector: selectors.BaseSelector | None = None
    primary_pending = False
    stdin = process.stdin
    try:
        selector = selectors.DefaultSelector()
        if stdin is not None:
            os.set_blocking(stdin.fileno(), False)
            selector.register(stdin.fileno(), selectors.EVENT_WRITE, "stdin")
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is None:
                continue
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe.fileno(), selectors.EVENT_READ, name)
        while selector.get_map():
            _check_execution_control(deadline, cancel_event)
            remaining = deadline - time.monotonic()
            for key, _mask in selector.select(timeout=min(0.1, max(0, remaining))):
                if key.data == "stdin":
                    if stdin is None:
                        raise SecureCodexError("codex cli failed")
                    if payload_offset == len(payload):
                        selector.unregister(key.fd)
                        stdin.close()
                        continue
                    try:
                        written = os.write(key.fd, payload[payload_offset : payload_offset + 65536])
                    except BlockingIOError:
                        written = 0
                    except BrokenPipeError:
                        selector.unregister(key.fd)
                        stdin.close()
                        payload_offset = len(payload)
                        continue
                    payload_offset += written
                    if payload_offset == len(payload):
                        selector.unregister(key.fd)
                        stdin.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                if key.data == "stdout":
                    stdout_size += len(chunk)
                    if stdout_size > max_stdout_bytes:
                        raise SecureCodexError("codex cli output exceeded limit")
                    stdout_chunks.append(chunk)
                else:
                    stderr_size += len(chunk)
                    if stderr_size > max_stderr_bytes:
                        raise SecureCodexError("codex cli output exceeded limit")
        while process.poll() is None:
            _check_execution_control(deadline, cancel_event)
            remaining = deadline - time.monotonic()
            try:
                process.wait(timeout=min(0.1, max(0, remaining)))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        primary_pending = True
        raise
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                if not primary_pending:
                    raise
    if process.returncode != 0:
        raise SecureCodexError("codex cli failed")
    return b"".join(stdout_chunks)
