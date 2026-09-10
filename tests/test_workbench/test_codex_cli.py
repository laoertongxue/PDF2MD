import hashlib
import json
import math
import os
import platform
import shlex
import signal
import socket
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import parsing_core.workbench.codex_cli as codex_cli_module
import parsing_core.workbench.secure_codex as secure_codex_module
from parsing_core.workbench.codex_cli import CodexCliError, CodexCliExecutor, resolve_codex_path
from parsing_core.workbench.secure_codex import SecureCodexError, validate_codex_argv

_DISABLED_FEATURES = secure_codex_module.DISABLED_CODEX_FEATURES

_SUPPORTED_VERSION = "codex-cli 0.142.1"
_WRAPPER_PACKAGE_VERSION = "0.142.1"
_NATIVE_PACKAGE_VERSION = "0.142.1-darwin-arm64"
_FEATURES_OUTPUT = "\n".join(
    [
        *(f"{name:<36} stable             false" for name in _DISABLED_FEATURES),
        "resize_all_images                   removed            true",
        "terminal_resize_reflow              removed            true",
        "tui_app_server                      removed            true",
    ]
)


def _write_fake_codex(
    path: Path,
    action: str,
    *,
    version: str = _SUPPORTED_VERSION,
    features_output: str = _FEATURES_OUTPUT,
) -> Path:
    version_branch = (
        'if [ "$#" -eq 1 ] && [ "$1" = \'--version\' ]; then '
        f"printf '%s\\n' {shlex.quote(version)}; exit 0; fi\n"
    )
    features_branch = (
        'case " $*" in *" features list") '
        f"printf '%s\\n' {shlex.quote(features_output)}; exit 0;; esac\n"
    )
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"{version_branch}"
        f"{features_branch}"
        "output=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        "  if [ \"$1\" = '--output-last-message' ]; then\n"
        "    shift\n"
        "    output=$1\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        'if [ -z "$output" ]; then exit 91; fi\n'
        f"{action}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _successful_codex(
    path: Path,
    text: str = "# result",
    *,
    version: str = _SUPPORTED_VERSION,
    features_output: str = _FEATURES_OUTPUT,
) -> Path:
    return _write_fake_codex(
        path,
        f"printf '%s\\n' '{text}' > \"$output\"",
        version=version,
        features_output=features_output,
    )


def _write_fake_native(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_thin_macho(0x0100000C))
    path.chmod(0o700)
    return path


def _thin_macho(cpu_type: int) -> bytes:
    return struct.pack("<IIIIIIII", 0xFEEDFACF, cpu_type, 0, 2, 0, 0, 0, 0)


def _official_wrapper_root(root: Path) -> Path:
    return root / "lib" / "node_modules" / "@openai" / "codex"


def _official_native_path(wrapper_root: Path) -> Path:
    return (
        wrapper_root
        / "node_modules"
        / "@openai"
        / "codex-darwin-arm64"
        / "vendor"
        / "aarch64-apple-darwin"
        / "bin"
        / "codex"
    )


def _write_official_codex_install(
    root: Path, payload: bytes | None = None
) -> tuple[Path, Path, Path]:
    wrapper_root = _official_wrapper_root(root)
    wrapper = wrapper_root / "bin" / "codex.js"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    wrapper.chmod(0o700)
    (wrapper_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": _WRAPPER_PACKAGE_VERSION,
                "bin": {"codex": "bin/codex.js"},
                "optionalDependencies": {
                    "@openai/codex-darwin-arm64": (f"npm:@openai/codex@{_NATIVE_PACKAGE_VERSION}")
                },
            }
        ),
        encoding="utf-8",
    )
    native = _official_native_path(wrapper_root)
    native.parent.mkdir(parents=True)
    native.write_bytes(_thin_macho(0x0100000C) if payload is None else payload)
    native.chmod(0o700)
    native_root = native.parents[3]
    (native_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": _NATIVE_PACKAGE_VERSION,
                "os": ["darwin"],
                "cpu": ["arm64"],
            }
        ),
        encoding="utf-8",
    )
    entry = root / "bin" / "codex"
    entry.parent.mkdir()
    entry.symlink_to(wrapper)
    return entry, wrapper, native


def _use_synthetic_official_release(monkeypatch: pytest.MonkeyPatch, native: Path) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    wrapper_root = native.parents[6]
    release_files = {
        "SUPPORTED_CODEX_SHA256": native,
        "SUPPORTED_CODEX_WRAPPER_SHA256": wrapper_root / "bin" / "codex.js",
        "SUPPORTED_CODEX_WRAPPER_PACKAGE_SHA256": wrapper_root / "package.json",
        "SUPPORTED_CODEX_NATIVE_PACKAGE_SHA256": native.parents[3] / "package.json",
    }
    for constant, release_file in release_files.items():
        if not release_file.is_file():
            continue
        monkeypatch.setattr(
            secure_codex_module,
            constant,
            hashlib.sha256(release_file.read_bytes()).hexdigest(),
            raising=False,
        )


def _replace_preflight_process_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = _SUPPORTED_VERSION,
    features_output: str = _FEATURES_OUTPUT,
) -> None:
    def execute(
        runner: object,
        argv: list[str],
        _payload: bytes,
        **_kwargs: object,
    ) -> bytes:
        del runner
        if argv[-1:] == ["--version"]:
            return f"{version}\n".encode("ascii")
        if argv[-2:] == ["features", "list"]:
            return f"{features_output}\n".encode("ascii")
        raise AssertionError("unexpected preflight command")

    monkeypatch.setattr(secure_codex_module.SecureCodexRunner, "_execute", execute)


def _secure_exec_prefix(executable: str) -> list[str]:
    prefix = [
        executable,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
    ]
    for feature in _DISABLED_FEATURES:
        prefix.extend(["--disable", feature])
    prefix.extend(["--sandbox", "read-only"])
    return prefix


def _mutate_task_directory(path: Path, mutation: str) -> Path | None:
    if mutation == "chmod":
        path.chmod(0o777)
        return None
    saved = path.with_name(f"{path.name}-{mutation}-saved")
    path.rename(saved)
    if mutation == "symlink":
        path.symlink_to(saved, target_is_directory=True)
    else:
        path.mkdir(mode=0o700)
    return saved


def _restore_task_directory(path: Path, mutation: str, saved: Path | None) -> None:
    if mutation == "chmod":
        path.chmod(0o700)
        return
    assert saved is not None
    if mutation == "symlink":
        path.unlink()
    else:
        path.rmdir()
    saved.rename(path)


class _ModuleOsProxy:
    def __init__(
        self,
        close: Callable[[int], None],
        open_: Callable[..., int] = os.open,
        fstat: Callable[[int], os.stat_result] = os.fstat,
        read: Callable[[int, int], bytes] = os.read,
    ) -> None:
        self.close = close
        self.open = open_
        self.fstat = fstat
        self.read = read

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


_LOCAL_BINARY_POLICY_TOKEN = object()


@dataclass(frozen=True)
class _LocalBinaryPolicyGrant:
    path: Path
    token: object

    def permits(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return self.token is _LOCAL_BINARY_POLICY_TOKEN and self.path == resolved


class _LocalFakeCodexPolicy:
    def __init__(self, original_policy: Callable[..., None]) -> None:
        self._original_policy = original_policy
        self._allowed: dict[Path, secure_codex_module.ExecutableIdentity] = {}

    def authorize(self, path: Path) -> _LocalBinaryPolicyGrant:
        fd, identity = secure_codex_module._open_executable(path, enforce_official_policy=False)
        descriptor = fd
        fd = -1
        os.close(descriptor)
        self._allowed[identity.path] = identity
        return _LocalBinaryPolicyGrant(identity.path, _LOCAL_BINARY_POLICY_TOKEN)

    def verify(
        self,
        path: Path,
        fd: int,
        identity: secure_codex_module.ExecutableIdentity,
    ) -> None:
        if self._allowed.get(path) == identity:
            return
        self._original_policy(path, fd, identity)


def _recursive_exception_surface(error: BaseException) -> str:
    rendered: list[str] = []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend(
            (
                str(current),
                repr(current),
                repr(current.args),
                repr(getattr(current, "__notes__", ())),
            )
        )
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return "\n".join(rendered)


def _wait_for_forked_child(pid: int, *, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    _waited, status = os.waitpid(pid, 0)
    pytest.fail(f"forked child did not exit before hard timeout: status={status}")


@pytest.fixture
def execution_behavior_uses_local_binary_policy_bypass() -> Iterator[_LocalFakeCodexPolicy]:
    original_policy = secure_codex_module._OFFICIAL_BINARY_POLICY_PROBE
    assert original_policy is secure_codex_module._verify_official_binary_policy
    policy = _LocalFakeCodexPolicy(original_policy)
    with pytest.MonkeyPatch.context() as policy_patch:
        policy_patch.setattr(
            secure_codex_module,
            "_OFFICIAL_BINARY_POLICY_PROBE",
            policy.verify,
        )
        yield policy
    assert secure_codex_module._OFFICIAL_BINARY_POLICY_PROBE is original_policy


def _grant_local_binary(policy: _LocalFakeCodexPolicy, path: Path) -> None:
    grant = policy.authorize(path)
    if not grant.permits(path):
        raise AssertionError("fake Codex requires an explicit local policy grant")


def test_fake_codex_without_explicit_local_policy_grant_is_rejected(tmp_path: Path) -> None:
    executable = _successful_codex(tmp_path / "ungranted-codex")

    with pytest.raises(CodexCliError, match="^codex cli is not available$"):
        CodexCliExecutor(str(executable), tmp_path / "runs")


def test_local_binary_policy_grant_allows_only_exact_authorized_identity(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass,
) -> None:
    allowed = _successful_codex(tmp_path / "allowed-codex")
    denied = _successful_codex(tmp_path / "denied-codex")

    grant = execution_behavior_uses_local_binary_policy_bypass.authorize(allowed)
    assert grant.permits(allowed)
    CodexCliExecutor(str(allowed), tmp_path / "allowed-runs")

    with pytest.raises(CodexCliError, match="^codex cli is not available$"):
        CodexCliExecutor(str(denied), tmp_path / "denied-runs")


@pytest.mark.parametrize(
    "timeout",
    [True, False, 0, -1, math.nan, math.inf, -math.inf, 3600.001, "60"],
)
def test_secure_runner_rejects_invalid_timeout_before_binary_access(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
) -> None:
    def unexpected_binary_access(_path: object) -> object:
        raise AssertionError("invalid timeout reached binary policy")

    monkeypatch.setattr(
        secure_codex_module,
        "_materialize_executable_snapshot",
        unexpected_binary_access,
    )

    with pytest.raises(ValueError, match="^secure codex runner limits"):
        secure_codex_module.SecureCodexRunner(
            "/private/tmp/sensitive-codex",
            timeout=timeout,  # type: ignore[arg-type]
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )


@pytest.mark.parametrize(
    "deadline",
    [True, False, 0, -1, math.nan, math.inf, -math.inf, "deadline"],
)
def test_secure_deadline_rejects_nonfinite_nonpositive_and_wrong_types(
    deadline: object,
) -> None:
    with pytest.raises(ValueError, match="^secure codex runner limits"):
        secure_codex_module._effective_deadline(1.0, deadline)  # type: ignore[arg-type]


def test_secure_deadline_rejects_excessive_remaining_time() -> None:
    with pytest.raises(ValueError, match="^secure codex runner limits"):
        secure_codex_module._effective_deadline(1.0, time.monotonic() + 3600.001)


def test_secure_deadline_produces_finite_bounded_remaining_time() -> None:
    started = time.monotonic()

    deadline = secure_codex_module._effective_deadline(1.0, started + 2.0)
    remaining = deadline - time.monotonic()

    assert math.isfinite(remaining)
    assert 0 < remaining <= 1.0


def test_secure_public_error_recursively_removes_sensitive_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive = "教材 SECRET-TOKEN /Users/private/book.pdf"
    source = OSError(sensitive)
    source.add_note(f"Bearer {sensitive}")
    nested = RuntimeError(sensitive)
    nested.__cause__ = source

    runner = object.__new__(secure_codex_module.SecureCodexRunner)
    runner.identity = SimpleNamespace(path=Path("/private/tmp/sensitive-codex"))
    runner._owner_pid = os.getpid()

    def fail_snapshot(*_args: object, **_kwargs: object) -> None:
        raise nested

    monkeypatch.setattr(secure_codex_module, "_assert_snapshot_current", fail_snapshot)

    with pytest.raises(SecureCodexError) as error:
        runner.assert_current()

    assert str(error.value) == "codex cli is not available"
    assert sensitive not in _recursive_exception_surface(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not getattr(error.value, "__notes__", ())


def test_codex_cli_public_error_recursively_removes_sensitive_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sensitive = "教材 SECRET-TOKEN /Users/private/book.pdf"
    source = OSError(sensitive)
    source.add_note(f"Bearer {sensitive}")
    nested = SecureCodexError(sensitive)
    nested.__cause__ = source
    nested.add_note(sensitive)

    def fail_runner(*_args: object, **_kwargs: object) -> None:
        raise nested

    monkeypatch.setattr(codex_cli_module, "SecureCodexRunner", fail_runner)

    with pytest.raises(CodexCliError) as error:
        CodexCliExecutor("/private/tmp/sensitive-codex", tmp_path / "runs")

    assert str(error.value) == "codex cli failed"
    assert sensitive not in _recursive_exception_surface(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not getattr(error.value, "__notes__", ())


def test_secure_global_runtime_locks_and_identity_caches_reset_after_fork() -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    parent_root = secure_codex_module._secure_runtime_root()
    source_key = ("parent-secret-cache",)
    snapshot_key = "parent-secret-snapshot"
    secure_codex_module._SOURCE_DIGEST_CACHE[source_key] = "secret"
    secure_codex_module._SNAPSHOT_IDENTITY_CACHE[snapshot_key] = SimpleNamespace()
    locks = (
        secure_codex_module._RUNTIME_ROOT_LOCK,
        secure_codex_module._SNAPSHOT_LOCK,
        secure_codex_module._IDENTITY_CACHE_LOCK,
    )
    read_fd, write_fd = os.pipe()
    for lock in locks:
        lock.acquire()
    try:
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            exit_code = 0
            child_root: Path | None = None
            try:
                child_root = secure_codex_module._secure_runtime_root()
                payload = json.dumps(
                    {
                        "cache_empty": not secure_codex_module._SOURCE_DIGEST_CACHE
                        and not secure_codex_module._SNAPSHOT_IDENTITY_CACHE,
                        "different_root": child_root != parent_root,
                        "state_pid": secure_codex_module._PROCESS_STATE_PID,
                        "pid": os.getpid(),
                    },
                    sort_keys=True,
                ).encode()
            except BaseException as exc:
                exit_code = 1
                payload = f"child-error:{type(exc).__name__}".encode()
            try:
                os.write(write_fd, payload)
            finally:
                os.close(write_fd)
                if child_root is not None:
                    secure_codex_module.shutil.rmtree(child_root, ignore_errors=True)
            os._exit(exit_code)
        os.close(write_fd)
    finally:
        for lock in reversed(locks):
            lock.release()

    try:
        status = _wait_for_forked_child(pid)
        payload = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
        secure_codex_module._SOURCE_DIGEST_CACHE.pop(source_key, None)
        secure_codex_module._SNAPSHOT_IDENTITY_CACHE.pop(snapshot_key, None)

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    observed = json.loads(payload)
    assert observed == {
        "cache_empty": True,
        "different_root": True,
        "pid": observed["pid"],
        "state_pid": observed["pid"],
    }


def test_secure_runner_inherited_across_fork_is_rejected_without_reuse(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: None,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            runner.assert_current()
        except SecureCodexError as exc:
            payload = str(exc).encode()
            exit_code = 0
        else:
            payload = b"parent runner was reused"
            exit_code = 1
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(exit_code)
    os.close(write_fd)
    try:
        status = _wait_for_forked_child(pid)
        payload = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert payload == b"codex cli is not available"


def test_codex_text_task_package_rejects_multibyte_payload_over_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: None,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")

    def unexpected_run(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized task package reached secure runner")

    monkeypatch.setattr(executor._runner, "run", unexpected_run)
    oversized = "教材" * ((4 * 1024 * 1024) // len("教材".encode()) + 1)

    with pytest.raises(CodexCliError, match="^codex cli input exceeded limit$"):
        executor.run("round-1", oversized)


def test_secure_runner_rejects_payload_over_unified_stdin_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: None,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    runner.max_stdin_bytes = 8

    def unexpected_execute(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized stdin reached process execution")

    monkeypatch.setattr(runner, "_execute", unexpected_execute)
    with runner.private_task() as task_dir:
        output = task_dir / "codex-round-1-output.md"
        argv = _secure_exec_prefix(str(runner.path)) + [
            "--cd",
            str(task_dir),
            "--output-last-message",
            str(output),
            "-",
        ]
        with pytest.raises(SecureCodexError, match="^codex cli input exceeded limit$"):
            runner.run(argv, b"123456789", cwd=task_dir)


def test_bounded_communicator_rejects_oversized_stdin_before_touching_process() -> None:
    with pytest.raises(SecureCodexError, match="^codex cli input exceeded limit$"):
        secure_codex_module._communicate_bounded(
            object(),  # type: ignore[arg-type]
            b"123456789",
            deadline=time.monotonic() + 1,
            cancel_event=None,
            max_stdin_bytes=8,
            max_stdout_bytes=8,
            max_stderr_bytes=8,
        )


def test_native_hash_reader_detects_growth_without_reading_to_eof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "codex"
    candidate.write_bytes(b"x")
    descriptor = os.open(candidate, os.O_RDONLY)
    reads = 0

    def growing_read(_fd: int, _size: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 2:
            raise AssertionError("hash reader continued beyond verified size")
        return b"x"

    monkeypatch.setattr(
        secure_codex_module,
        "os",
        _ModuleOsProxy(os.close, read=growing_read),
    )
    try:
        with pytest.raises(SecureCodexError, match="^codex cli is not available$"):
            secure_codex_module._fd_sha256(descriptor)
    finally:
        os.close(descriptor)

    assert reads == 2


def test_oversized_native_candidate_is_rejected_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _entry, _wrapper, native = _write_official_codex_install(tmp_path)
    with native.open("r+b") as handle:
        handle.truncate(256 * 1024 * 1024 + 1)

    def unexpected_hash(_fd: int) -> str:
        raise AssertionError("oversized native reached hash reader")

    monkeypatch.setattr(secure_codex_module, "_fd_sha256", unexpected_hash)

    with pytest.raises(CodexCliError, match="^codex cli not found$"):
        resolve_codex_path(native)


def test_real_policy_accepts_explicit_native_in_synthetic_official_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, executable)
    monkeypatch.setenv("CODEX_CLI_PATH", "/untrusted/from-env")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex")

    assert resolve_codex_path(str(executable)) == str(executable.resolve())


def test_resolve_codex_path_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "codex"
    os.mkfifo(fifo, mode=0o700)
    errors: list[str] = []

    def resolve() -> None:
        try:
            resolve_codex_path(fifo)
        except CodexCliError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=resolve)
    worker.start()
    worker.join(timeout=0.5)
    finished_within_timeout = not worker.is_alive()

    unblock_fd: int | None = None
    try:
        if worker.is_alive():
            unblock_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            worker.join(timeout=1.0)
    finally:
        if unblock_fd is not None:
            os.close(unblock_fd)
        fifo.unlink(missing_ok=True)

    assert finished_within_timeout
    assert not worker.is_alive()
    assert worker.daemon is False
    assert errors == ["codex cli not found"]


def test_resolve_codex_path_race_to_fifo_uses_nonblocking_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = _write_fake_native(tmp_path / "codex")
    real_open = os.open
    opened_flags: list[int] = []
    errors: list[str] = []
    unexpected: list[BaseException] = []
    swapped = False

    def replace_with_fifo_before_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is None and Path(path) == executable:
            executable.unlink()
            os.mkfifo(executable, mode=0o700)
            opened_flags.append(flags)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def resolve() -> None:
        try:
            resolve_codex_path(executable)
        except CodexCliError as exc:
            errors.append(str(exc))
        except BaseException as exc:
            unexpected.append(exc)

    monkeypatch.setattr(codex_cli_module.os, "open", replace_with_fifo_before_open)
    worker = threading.Thread(target=resolve)
    worker.start()
    worker.join(timeout=0.5)
    finished_within_timeout = not worker.is_alive()

    unblock_fd: int | None = None
    try:
        if worker.is_alive():
            unblock_fd = real_open(executable, os.O_RDWR | os.O_NONBLOCK)
            worker.join(timeout=1.0)
    finally:
        if unblock_fd is not None:
            os.close(unblock_fd)
        executable.unlink(missing_ok=True)

    assert not worker.is_alive()
    assert worker.daemon is False
    assert finished_within_timeout
    assert swapped is True
    assert opened_flags[0] & os.O_NONBLOCK
    assert errors == ["codex cli not found"]
    assert unexpected == []


def test_mutation_guard_snapshot_recheck_requires_nonblocking_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _write_fake_native(tmp_path / "snapshot")
    identity_fd, identity = secure_codex_module._open_executable(
        snapshot, enforce_official_policy=False
    )
    os.close(identity_fd)
    real_open = os.open
    opened_flags: list[int] = []
    errors: list[str] = []
    unexpected: list[BaseException] = []
    swapped = False

    def replace_with_fifo_before_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is None and Path(path) == snapshot:
            snapshot.unlink()
            os.mkfifo(snapshot, mode=0o700)
            opened_flags.append(flags)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def recheck() -> None:
        try:
            secure_codex_module._assert_snapshot_current(snapshot, identity)
        except SecureCodexError as exc:
            errors.append(str(exc))
        except BaseException as exc:
            unexpected.append(exc)

    monkeypatch.setattr(secure_codex_module.os, "open", replace_with_fifo_before_open)
    worker = threading.Thread(target=recheck)
    worker.start()
    worker.join(timeout=0.5)
    finished_within_timeout = not worker.is_alive()

    unblock_fd: int | None = None
    try:
        if worker.is_alive():
            unblock_fd = real_open(snapshot, os.O_RDWR | os.O_NONBLOCK)
            worker.join(timeout=1.0)
    finally:
        if unblock_fd is not None:
            os.close(unblock_fd)
        if not worker.is_alive():
            snapshot.unlink(missing_ok=True)

    assert not worker.is_alive()
    assert worker.daemon is False
    assert finished_within_timeout
    assert swapped is True
    assert opened_flags[0] & os.O_NONBLOCK
    assert errors == ["codex cli is not available"]
    assert unexpected == []


def test_mutation_guard_authoritative_open_requires_preopen_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = _write_fake_native(tmp_path / "codex")
    replacement = _write_fake_native(tmp_path / "replacement")
    real_open = os.open
    opened_fd: int | None = None
    error: SecureCodexError | None = None
    swapped = False

    def replace_before_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is None and Path(path) == candidate:
            candidate.unlink()
            replacement.rename(candidate)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(secure_codex_module.os, "open", replace_before_open)
    try:
        try:
            opened_fd, _identity = secure_codex_module._open_executable(
                candidate, enforce_official_policy=False
            )
        except SecureCodexError as exc:
            error = exc
    finally:
        if opened_fd is not None:
            os.close(opened_fd)

    assert swapped is True
    assert error is not None
    assert str(error) == "codex cli is not available"


def test_resolver_rejects_entry_swap_before_authoritative_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, native = _write_official_codex_install(tmp_path)
    replacement = _write_fake_native(tmp_path / "replacement")
    original = native.with_name("codex-original")
    _use_synthetic_official_release(monkeypatch, native)
    real_probe = codex_cli_module._is_macho_executable
    swapped = False

    def swap_before_probe(path: Path, *args: object, **kwargs: object) -> bool:
        nonlocal swapped
        if not swapped:
            native.rename(original)
            replacement.rename(native)
            swapped = True
        return real_probe(path, *args, **kwargs)

    monkeypatch.setattr(codex_cli_module, "_is_macho_executable", swap_before_probe)
    try:
        with pytest.raises(CodexCliError, match="codex cli not found"):
            resolve_codex_path(native)
    finally:
        native.unlink(missing_ok=True)
        original.rename(native)

    assert swapped is True


def test_authoritative_open_rejects_path_swap_during_real_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, native = _write_official_codex_install(tmp_path)
    replacement = _write_fake_native(tmp_path / "replacement")
    original = native.with_name("codex-original")
    _use_synthetic_official_release(monkeypatch, native)
    real_architectures = secure_codex_module._macho_architectures
    swapped = False

    def inspect_then_swap(fd: int, size: int) -> frozenset[str]:
        nonlocal swapped
        architectures = real_architectures(fd, size)
        if not swapped:
            native.rename(original)
            replacement.rename(native)
            swapped = True
        return architectures

    monkeypatch.setattr(secure_codex_module, "_macho_architectures", inspect_then_swap)
    try:
        with pytest.raises(CodexCliError, match="codex cli not found"):
            resolve_codex_path(native)
    finally:
        native.unlink(missing_ok=True)
        original.rename(native)

    assert swapped is True


def test_macho_probe_close_after_release_preserves_fail_closed_result_and_reused_fd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = _write_fake_native(tmp_path / "codex")
    victim = tmp_path / "victim.txt"
    victim.write_text("must remain open", encoding="utf-8")
    real_close = os.close
    real_fstat = os.fstat
    source_fd: int | None = None
    reused_fd: int | None = None
    close_calls: list[int] = []

    def fail_fstat(fd: int) -> os.stat_result:
        nonlocal source_fd
        source_fd = fd
        raise OSError("FSTAT-PROMPT-SECRET")

    def close_then_reuse_and_raise(fd: int) -> None:
        nonlocal reused_fd
        close_calls.append(fd)
        assert fd == source_fd
        real_close(fd)
        reused_fd = os.open(victim, os.O_RDONLY)
        assert reused_fd == fd
        raise OSError("CLOSE-PATH-SECRET")

    monkeypatch.setattr(
        codex_cli_module,
        "os",
        _ModuleOsProxy(close_then_reuse_and_raise, fstat=fail_fstat),
    )
    try:
        assert codex_cli_module._is_macho_executable(executable) is False
        assert source_fd is not None
        assert close_calls == [source_fd]
        assert reused_fd is not None
        real_fstat(reused_fd)
    finally:
        if reused_fd is not None:
            real_close(reused_fd)


@pytest.mark.parametrize("kind", ["socket", "directory", "device"])
def test_resolve_codex_path_quickly_rejects_nonregular_entries(tmp_path: Path, kind: str) -> None:
    socket_handle: socket.socket | None = None
    socket_temp: tempfile.TemporaryDirectory[str] | None = None
    if kind == "socket":
        socket_temp = tempfile.TemporaryDirectory(prefix="pdf2md-socket-", dir="/private/tmp")
        candidate = Path(socket_temp.name) / "codex"
        socket_handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            socket_handle.bind(str(candidate))
        except PermissionError:
            socket_handle.close()
            socket_handle = None
            socket_temp.cleanup()
            socket_temp = None
            for system_socket in (
                Path("/private/var/run/syslog"),
                Path("/var/run/syslog"),
                Path("/run/systemd/private"),
                Path("/var/run/docker.sock"),
            ):
                try:
                    if stat.S_ISSOCK(system_socket.lstat().st_mode):
                        candidate = system_socket
                        break
                except OSError:
                    continue
            else:
                pytest.skip("no usable UNIX socket path")
    elif kind == "directory":
        candidate = tmp_path / "codex-directory"
        candidate.mkdir()
    else:
        candidate = Path("/dev/null")

    errors: list[str] = []
    unexpected: list[BaseException] = []

    def resolve() -> None:
        try:
            resolve_codex_path(candidate)
        except CodexCliError as exc:
            errors.append(str(exc))
        except BaseException as exc:
            unexpected.append(exc)

    try:
        worker = threading.Thread(target=resolve)
        worker.start()
        worker.join(timeout=0.5)
        assert not worker.is_alive()
        assert worker.daemon is False
        assert errors == ["codex cli not found"]
        assert unexpected == []
    finally:
        if socket_handle is not None:
            socket_handle.close()
        if socket_temp is not None:
            socket_temp.cleanup()


@pytest.mark.parametrize("configured", ["./codex", "codex"])
def test_resolve_codex_path_rejects_noncanonical_env_path(monkeypatch, tmp_path, configured):
    _write_fake_native(tmp_path / "codex")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_CLI_PATH", configured)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path()


def test_real_policy_accepts_entry_symlink_to_synthetic_official_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, _wrapper, native = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, native)

    assert resolve_codex_path(entry) == str(native)


def test_real_policy_rejects_one_line_shebang_wrapper_without_fixed_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, wrapper, native = _write_official_codex_install(tmp_path)
    assert wrapper.read_bytes() == b"#!/usr/bin/env node\n"
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        secure_codex_module,
        "SUPPORTED_CODEX_SHA256",
        hashlib.sha256(native.read_bytes()).hexdigest(),
    )

    with pytest.raises(CodexCliError, match="^codex cli not found$"):
        resolve_codex_path(entry)


@pytest.mark.parametrize(
    "release_file",
    ["wrapper", "wrapper-package", "native-package"],
)
def test_real_policy_rejects_release_file_byte_mutation_after_digest_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    release_file: str,
) -> None:
    entry, wrapper, native = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, native)
    targets = {
        "wrapper": wrapper,
        "wrapper-package": wrapper.parent.parent / "package.json",
        "native-package": native.parents[3] / "package.json",
    }
    target = targets[release_file]
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(CodexCliError, match="^codex cli not found$"):
        resolve_codex_path(entry)


def test_real_policy_rejects_entry_symlink_directly_to_trusted_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, _wrapper, native = _write_official_codex_install(tmp_path)
    entry.unlink()
    entry.symlink_to(native)
    _use_synthetic_official_release(monkeypatch, native)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(entry)


@pytest.mark.parametrize(
    ("package", "field", "value"),
    [
        ("wrapper", "name", "not-openai/codex"),
        ("wrapper", "version", "0.142.2"),
        ("native", "name", "@openai/codex-darwin-arm64"),
        ("native", "version", "0.142.2-darwin-arm64"),
    ],
)
def test_real_policy_rejects_inexact_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    package: str,
    field: str,
    value: str,
) -> None:
    entry, wrapper, native = _write_official_codex_install(tmp_path)
    package_json = (
        wrapper.parent.parent / "package.json"
        if package == "wrapper"
        else native.parents[3] / "package.json"
    )
    metadata = json.loads(package_json.read_text(encoding="utf-8"))
    metadata[field] = value
    package_json.write_text(json.dumps(metadata), encoding="utf-8")
    _use_synthetic_official_release(monkeypatch, native)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(entry)


def test_real_policy_redacts_invalid_package_metadata_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, wrapper, native = _write_official_codex_install(tmp_path)
    package_json = wrapper.parent.parent / "package.json"
    metadata = json.loads(package_json.read_text(encoding="utf-8"))
    secret = f"PACKAGE-PROMPT-SECRET::{package_json}"
    metadata["optionalDependencies"] = secret
    package_json.write_text(json.dumps(metadata), encoding="utf-8")
    _use_synthetic_official_release(monkeypatch, native)

    with pytest.raises(CodexCliError) as exc_info:
        resolve_codex_path(entry)

    assert str(exc_info.value) == "codex cli not found"
    assert secret not in str(exc_info.value)
    assert str(package_json) not in str(exc_info.value)


def test_real_policy_rejects_group_writable_package_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, _wrapper, native = _write_official_codex_install(tmp_path)
    native.parents[3].chmod(0o775)
    _use_synthetic_official_release(monkeypatch, native)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(entry)


def test_mutation_guard_real_policy_requires_exact_package_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = _write_fake_native(
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex-darwin-arm64"
        / "vendor"
        / "aarch64-apple-darwin"
        / "bin"
        / "codex"
    )
    _use_synthetic_official_release(monkeypatch, native)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(native)


def test_real_policy_retains_which_fallback_to_official_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry, _wrapper, native = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, native)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: str(entry) if name == "codex" else None)

    assert resolve_codex_path() == str(native)


def test_current_homebrew_official_wrapper_symlink_passes_real_native_policy() -> None:
    wrapper = Path("/opt/homebrew/bin/codex")
    if not wrapper.is_symlink():
        pytest.skip("this machine does not have the Homebrew Codex wrapper symlink")

    resolved = Path(resolve_codex_path(str(wrapper)))

    assert resolved.name == "codex"
    assert "vendor" in resolved.parts
    assert resolved.read_bytes()[:4] in {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
    assert resolved.suffix != ".js"


def test_nonofficial_native_symlink_fails_real_policy(tmp_path: Path) -> None:
    native = _write_fake_native(tmp_path / "unofficial" / "codex")
    link = tmp_path / "bin" / "codex"
    link.parent.mkdir()
    link.symlink_to(native)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(link)


def test_real_policy_rejects_group_writable_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    executable.chmod(0o720)
    _use_synthetic_official_release(monkeypatch, executable)
    monkeypatch.setenv("CODEX_CLI_PATH", str(executable))

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path()


def test_real_policy_rejects_executable_owned_by_another_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, executable)
    monkeypatch.setenv("CODEX_CLI_PATH", str(executable))
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path()


@pytest.mark.parametrize(
    "payload",
    [
        b"\xcf\xfa\xed\xfePDF2MD-CODEX-NATIVE-TEST",
        _thin_macho(0x01000007),
    ],
    ids=["magic-only", "x86_64"],
)
def test_resolve_codex_path_rejects_non_arm64_macho(payload, monkeypatch, tmp_path):
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path, payload)
    _use_synthetic_official_release(monkeypatch, executable)

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(executable)


def test_resolve_codex_path_rejects_arm64_binary_outside_official_package(tmp_path):
    executable = _write_fake_native(tmp_path / "codex")

    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(executable)


def test_resolve_codex_path_rejects_official_layout_with_wrong_release_digest(
    monkeypatch, tmp_path
):
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    assert hashlib.sha256(executable.read_bytes()).hexdigest() != (
        "df8ae76bd03329da060c4e427461dcee2faa87711e1a5d47143181ce3618b25d"
    )
    with pytest.raises(CodexCliError, match="codex cli not found"):
        resolve_codex_path(executable)


def test_executor_reads_output_file_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(
        tmp_path / "codex",
        "# intensive reading result\\nflowchart TD\\nA --> B",
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    run_dir = tmp_path / "runs"

    output = CodexCliExecutor(str(executable), run_dir).run("round-1", "# task")

    assert "flowchart TD" in output
    assert not (run_dir / "codex-round-1-input.md").exists()


def test_real_binary_policy_rejects_wrong_fixed_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, executable)
    _replace_preflight_process_probe(monkeypatch, version="codex-cli 0.142.2")

    with pytest.raises(CodexCliError, match="codex cli is not available"):
        CodexCliExecutor(str(executable), tmp_path / "runs")


@pytest.mark.parametrize(
    "features_output",
    [
        _FEATURES_OUTPUT + "\nfuture_tool                         stable             true",
        _FEATURES_OUTPUT + "\nmalformed feature output",
    ],
    ids=["unknown-enabled", "format-change"],
)
def test_real_binary_policy_feature_preflight_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    features_output: str,
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, executable)
    _replace_preflight_process_probe(monkeypatch, features_output=features_output)

    with pytest.raises(CodexCliError, match="codex cli is not available"):
        CodexCliExecutor(str(executable), tmp_path / "runs")


def test_real_binary_policy_accepts_fixed_version_and_feature_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _entry, _wrapper, executable = _write_official_codex_install(tmp_path)
    _use_synthetic_official_release(monkeypatch, executable)
    _replace_preflight_process_probe(monkeypatch)

    executor = CodexCliExecutor(str(executable), tmp_path / "runs")

    assert executor._runner.codex_version == _SUPPORTED_VERSION


def test_executor_uses_minimal_environment_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    inherited = os.open(tmp_path / "inherited.txt", os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(inherited, True)
    action = (
        f'if [ -n "${{PDF2MD_TEST_SECRET+x}}" ] || [ -e /dev/fd/{inherited} ]; then '
        "printf 'unsafe' > \"$output\"; else "
        'printf \'safe|%s|%s|%s\' "$HOME" "$TMPDIR" "$PATH" > "$output"; fi'
    )
    executable = _write_fake_codex(tmp_path / "codex", action)
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    monkeypatch.setenv("PDF2MD_TEST_SECRET", "TOP-SECRET-VALUE")
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("PATH", "/untrusted/finder/path")
    try:
        output = CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")
    finally:
        os.close(inherited)

    marker, home, task_tmp, path = output.split("|")
    assert marker == "safe"
    assert home == str(tmp_path / "home")
    assert task_tmp != str(tmp_path / "tmp")
    assert not Path(task_tmp).exists()
    assert path == "/usr/bin:/bin"
    assert "TOP-SECRET-VALUE" not in output


def test_executor_uses_verified_snapshot_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "original")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")
    _successful_codex(executable, "tampered")

    assert executor.run("round-1", "task").strip() == "original"


def test_snapshot_copies_verified_fd_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "verified")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    replacement = _successful_codex(tmp_path / "replacement", "malicious")
    saved = tmp_path / "codex.saved"
    boundaries: list[str] = []

    def swap_at_boundary(name, *, source_path, snapshot_path=None):
        boundaries.append(name)
        if name == "source_verified":
            source_path.rename(saved)
            replacement.rename(source_path)
        elif name == "snapshot_published" and saved.exists():
            source_path.unlink()
            saved.rename(source_path)

    monkeypatch.setattr(
        secure_codex_module, "_snapshot_boundary_hook", swap_at_boundary, raising=False
    )
    try:
        executor = CodexCliExecutor(str(executable), tmp_path / "runs")
        result = executor.run("round-1", "task")
    finally:
        if saved.exists():
            executable.unlink(missing_ok=True)
            saved.rename(executable)

    assert "source_verified" in boundaries
    assert "snapshot_published" in boundaries
    assert result.strip() == "verified"


def test_snapshot_close_failure_with_local_binary_policy_bypass_never_recloses_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "verified")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    source_fd, source_identity = secure_codex_module._open_verified_executable(executable)
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    snapshot = root / f"codex-{source_identity.sha256}"
    victim_path = tmp_path / "victim.txt"
    victim_path.write_text("must remain open", encoding="utf-8")
    real_close = os.close
    reused_fd: int | None = None
    injected = False

    def close_then_raise(fd: int) -> None:
        nonlocal injected, reused_fd
        if not injected and fd != source_fd and stat.S_ISREG(os.fstat(fd).st_mode):
            injected = True
            real_close(fd)
            reused_fd = os.open(victim_path, os.O_RDONLY)
            assert reused_fd == fd
            raise OSError("close reported failure after releasing fd")
        real_close(fd)

    monkeypatch.setattr(secure_codex_module, "os", _ModuleOsProxy(close_then_raise))
    try:
        with pytest.raises(SecureCodexError, match="codex cli is not available"):
            secure_codex_module._publish_snapshot(
                source_fd,
                source_identity,
                root,
                snapshot,
            )
        assert reused_fd is not None
        os.fstat(reused_fd)
    finally:
        if reused_fd is not None:
            try:
                real_close(reused_fd)
            except OSError:
                pass
        real_close(source_fd)


def test_snapshot_publish_redacts_directory_open_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _write_fake_native(tmp_path / "source")
    source_fd, source_identity = secure_codex_module._open_executable(
        source, enforce_official_policy=False
    )
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    snapshot = root / f"codex-{source_identity.sha256}"
    real_open = os.open

    def fail_root_open(*args: Any, **kwargs: Any) -> int:
        if Path(args[0]) == root:
            raise OSError(f"DIRECTORY-OPEN-PROMPT-SECRET::{root}")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(
        secure_codex_module,
        "os",
        _ModuleOsProxy(os.close, open_=fail_root_open),
    )
    try:
        with pytest.raises(SecureCodexError) as exc_info:
            secure_codex_module._publish_snapshot(
                source_fd,
                source_identity,
                root,
                snapshot,
            )
    finally:
        os.close(source_fd)

    assert str(exc_info.value) == "codex cli is not available"
    assert "SECRET" not in str(exc_info.value)
    assert str(root) not in str(exc_info.value)


def test_private_directory_redacts_fstat_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "private"
    candidate.mkdir(mode=0o700)

    def fail_fstat(_fd: int) -> os.stat_result:
        raise OSError(f"FSTAT-PROMPT-SECRET::{candidate}")

    monkeypatch.setattr(
        secure_codex_module,
        "os",
        _ModuleOsProxy(os.close, fstat=fail_fstat),
    )

    with pytest.raises(SecureCodexError) as exc_info:
        secure_codex_module._validated_private_directory(candidate)

    assert str(exc_info.value) == "codex cli is not available"
    assert "SECRET" not in str(exc_info.value)
    assert str(candidate) not in str(exc_info.value)


def test_private_directory_authoritative_open_rejects_path_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "private"
    candidate.mkdir(mode=0o700)
    original = tmp_path / "private-original"
    real_open = os.open
    swapped = False

    def open_then_swap(*args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        fd = real_open(*args, **kwargs)
        if Path(args[0]) == candidate and not swapped:
            candidate.rename(original)
            candidate.mkdir(mode=0o700)
            swapped = True
        return fd

    monkeypatch.setattr(
        secure_codex_module,
        "os",
        _ModuleOsProxy(os.close, open_=open_then_swap),
    )
    try:
        with pytest.raises(SecureCodexError, match="codex cli is not available"):
            secure_codex_module._validated_private_directory(candidate)
    finally:
        candidate.rmdir()
        original.rename(candidate)

    assert swapped is True


def test_source_swap_before_spawn_with_local_binary_policy_bypass_cannot_change_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "verified")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    replacement = _successful_codex(tmp_path / "replacement", "malicious")
    saved = tmp_path / "codex.saved"
    observed = False
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")

    def swap_and_restore(name, *, source_path, snapshot_path=None):
        nonlocal observed
        if name != "before_spawn":
            return
        observed = True
        source_path.rename(saved)
        replacement.rename(source_path)
        source_path.unlink()
        saved.rename(source_path)

    monkeypatch.setattr(secure_codex_module, "_snapshot_boundary_hook", swap_and_restore)

    assert executor.run("round-1", "task").strip() == "verified"
    assert observed is True


def test_unchanged_snapshot_reuses_hash_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "cached")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    first = CodexCliExecutor(str(executable), tmp_path / "runs-one")
    assert first.run("round-1", "task").strip() == "cached"

    def unexpected_hash(_fd: int) -> str:
        raise AssertionError("unchanged executable was fully rehashed")

    monkeypatch.setattr(secure_codex_module, "_fd_sha256", unexpected_hash)
    second = CodexCliExecutor(str(executable), tmp_path / "runs-two")

    assert second.run("round-2", "task").strip() == "cached"


def test_runner_executes_private_snapshot_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex", "snapshot")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    spawned: list[Path] = []
    real_popen = secure_codex_module.subprocess.Popen

    def wrapped_popen(args, **kwargs):
        spawned.append(Path(args[0]))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(secure_codex_module.subprocess, "Popen", wrapped_popen)

    assert CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")

    assert spawned
    snapshot = spawned[-1]
    assert snapshot != executable
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o500
    assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
    assert snapshot.read_bytes() == executable.read_bytes()


@pytest.mark.parametrize("mode", [0o711, 0o755, 0o770], ids=["0711", "0755", "0770"])
def test_runner_rejects_nonprivate_cwd_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: int,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    executions: list[Path] = []

    def execute(_argv: list[str], _payload: bytes, *, cwd: Path, **_kwargs: object) -> bytes:
        executions.append(cwd)
        return b""

    monkeypatch.setattr(runner, "_execute", execute)
    with runner.private_task() as task_dir:
        task_dir.chmod(mode)
        with pytest.raises(SecureCodexError) as exc_info:
            runner.run(
                [str(runner.path), "--version"],
                b"PROMPT-SECRET",
                cwd=task_dir,
            )

    assert str(exc_info.value) == "codex cli failed"
    assert executions == []


def test_runner_accepts_private_cwd_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    executions: list[Path] = []

    def execute(_argv: list[str], _payload: bytes, *, cwd: Path, **_kwargs: object) -> bytes:
        executions.append(cwd)
        return b"accepted"

    monkeypatch.setattr(runner, "_execute", execute)
    with runner.private_task() as task_dir:
        task_dir.chmod(0o700)
        output = runner.run([str(runner.path), "--version"], b"task", cwd=task_dir)
        assert executions == [task_dir]

    assert output == b"accepted"


def test_runner_redacts_cwd_race_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    real_validate = secure_codex_module._validated_execution_cwd
    executions: list[Path] = []

    def validate_then_remove(path: str | Path) -> Path:
        validated = real_validate(path)
        validated.rmdir()
        return validated

    def execute(_argv: list[str], _payload: bytes, *, cwd: Path, **_kwargs: object) -> bytes:
        executions.append(cwd)
        return b""

    monkeypatch.setattr(secure_codex_module, "_validated_execution_cwd", validate_then_remove)
    monkeypatch.setattr(runner, "_execute", execute)
    with runner.private_task() as task_dir:
        secret = f"PROMPT-SECRET::{task_dir}"
        with pytest.raises(SecureCodexError) as exc_info:
            runner.run([str(runner.path), "--version"], secret.encode(), cwd=task_dir)

    assert str(exc_info.value) == "codex cli failed"
    assert secret not in str(exc_info.value)
    assert str(task_dir) not in str(exc_info.value)
    assert executions == []


@pytest.mark.parametrize("mutation", ["symlink", "chmod", "replace"])
def test_runner_second_cwd_check_with_local_binary_policy_bypass_rejects_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    real_validate = secure_codex_module._validated_execution_cwd
    executions: list[Path] = []
    saved: Path | None = None

    def validate_then_mutate(path: str | Path) -> Path:
        nonlocal saved
        validated = real_validate(path)
        saved = _mutate_task_directory(validated, mutation)
        return validated

    def execute(_argv: list[str], _payload: bytes, *, cwd: Path, **_kwargs: object) -> bytes:
        executions.append(cwd)
        return b""

    monkeypatch.setattr(secure_codex_module, "_validated_execution_cwd", validate_then_mutate)
    monkeypatch.setattr(runner, "_execute", execute)
    with runner.private_task() as task_dir:
        secret = f"CWD-PROMPT-SECRET::{task_dir}"
        try:
            with pytest.raises(SecureCodexError) as exc_info:
                runner.run([str(runner.path), "--version"], secret.encode(), cwd=task_dir)
        finally:
            _restore_task_directory(task_dir, mutation, saved)

    assert str(exc_info.value) == "codex cli failed"
    assert secret not in str(exc_info.value)
    assert str(task_dir) not in str(exc_info.value)
    assert executions == []


@pytest.mark.parametrize("mutation", ["symlink", "chmod", "replace"])
def test_runner_spawn_recheck_with_local_binary_policy_bypass_rejects_cwd_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    spawned: list[Path] = []
    saved: Path | None = None
    task_dir: Path | None = None

    def mutate_before_spawn(name: str, **_kwargs: object) -> None:
        nonlocal saved
        if name == "before_spawn":
            assert task_dir is not None
            saved = _mutate_task_directory(task_dir, mutation)

    def unexpected_spawn(_argv: list[str], *, cwd: Path, **_kwargs: object) -> None:
        spawned.append(cwd)
        raise AssertionError("spawn reached with invalid cwd")

    monkeypatch.setattr(secure_codex_module, "_snapshot_boundary_hook", mutate_before_spawn)
    monkeypatch.setattr(secure_codex_module, "_spawn_isolated_process", unexpected_spawn)
    with runner.private_task() as active_task:
        task_dir = active_task
        secret = f"SPAWN-PROMPT-SECRET::{task_dir}"
        try:
            with pytest.raises(SecureCodexError) as exc_info:
                runner.run([str(runner.path), "--version"], secret.encode(), cwd=task_dir)
        finally:
            _restore_task_directory(task_dir, mutation, saved)

    assert str(exc_info.value) == "codex cli failed"
    assert secret not in str(exc_info.value)
    assert str(task_dir) not in str(exc_info.value)
    assert spawned == []


def test_executor_revalidates_hash_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _write_fake_codex(
        tmp_path / "codex",
        'printf \'result\' > "$output"\nchmod 700 "$0"\nprintf \'# changed\\n\' >> "$0"',
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)

    with pytest.raises(CodexCliError, match="codex cli is not available"):
        CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")


def test_executor_bounds_stdout_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    monkeypatch.setattr(codex_cli_module, "_MAX_STDOUT_BYTES", 32, raising=False)
    executable = _write_fake_codex(
        tmp_path / "codex",
        "printf '0123456789012345678901234567890123456789'\nprintf 'result' > \"$output\"",
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)

    with pytest.raises(CodexCliError, match="codex cli output exceeded limit"):
        CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")


def test_executor_bounds_output_file_with_local_binary_policy_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    monkeypatch.setattr(codex_cli_module, "_MAX_OUTPUT_FILE_BYTES", 32, raising=False)
    executable = _write_fake_codex(
        tmp_path / "codex",
        "printf '0123456789012345678901234567890123456789' > \"$output\"",
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)

    with pytest.raises(CodexCliError, match="codex output exceeded limit"):
        CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")


@pytest.mark.parametrize("primary_failure", [False, True], ids=["close-only", "primary-error"])
def test_output_reader_closes_both_owned_fds_and_redacts_close_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, primary_failure: bool
) -> None:
    output = tmp_path / "result.md"
    output.write_text("result", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("must remain open", encoding="utf-8")
    real_close = os.close
    real_open = os.open
    real_fstat = os.fstat
    opened_fds: list[int] = []
    close_calls: list[int] = []
    reused_fd: int | None = None

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    def close_first_then_reuse_and_raise(fd: int) -> None:
        nonlocal reused_fd
        close_calls.append(fd)
        real_close(fd)
        if len(close_calls) == 1:
            reused_fd = real_open(victim, os.O_RDONLY)
            assert reused_fd == fd
            raise OSError("CLOSE-PATH-PROMPT-SECRET")

    def fail_read(_fd: int, _size: int) -> bytes:
        raise OSError("READ-PATH-PROMPT-SECRET")

    monkeypatch.setattr(
        codex_cli_module,
        "os",
        _ModuleOsProxy(close_first_then_reuse_and_raise, tracked_open),
    )
    if primary_failure:
        monkeypatch.setattr(codex_cli_module, "_read_output_chunk", fail_read)
    try:
        with pytest.raises(CodexCliError) as exc_info:
            codex_cli_module._read_output_file(output)

        assert str(exc_info.value) == "codex output file is invalid"
        assert "SECRET" not in str(exc_info.value)
        assert len(opened_fds) == 2
        assert close_calls == [opened_fds[1], opened_fds[0]]
        assert reused_fd == opened_fds[1]
        real_fstat(reused_fd)
        with pytest.raises(OSError):
            real_fstat(opened_fds[0])
    finally:
        for fd in {*opened_fds, *(() if reused_fd is None else (reused_fd,))}:
            try:
                real_close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("boundary", ["private-directory", "snapshot-recheck"])
def test_secure_single_fd_close_after_release_fails_closed_without_reclosing_reused_fd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boundary: str
) -> None:
    candidate = tmp_path / "candidate"
    if boundary == "private-directory":
        candidate.mkdir(mode=0o700)
        identity = None
    else:
        _write_fake_native(candidate)
        identity_fd, identity = secure_codex_module._open_executable(
            candidate, enforce_official_policy=False
        )
        os.close(identity_fd)
    victim = tmp_path / "victim.txt"
    victim.write_text("must remain open", encoding="utf-8")
    real_close = os.close
    real_open = os.open
    real_fstat = os.fstat
    close_calls: list[int] = []
    reused_fd: int | None = None

    def close_then_reuse_and_raise(fd: int) -> None:
        nonlocal reused_fd
        close_calls.append(fd)
        real_close(fd)
        reused_fd = real_open(victim, os.O_RDONLY)
        assert reused_fd == fd
        raise OSError("CLOSE-PATH-PROMPT-SECRET")

    monkeypatch.setattr(secure_codex_module, "os", _ModuleOsProxy(close_then_reuse_and_raise))
    try:
        with pytest.raises(SecureCodexError) as exc_info:
            if boundary == "private-directory":
                secure_codex_module._validated_private_directory(candidate)
            else:
                assert identity is not None
                secure_codex_module._assert_snapshot_current(candidate, identity)

        assert str(exc_info.value) == "codex cli is not available"
        assert "SECRET" not in str(exc_info.value)
        assert len(close_calls) == 1
        assert reused_fd is not None
        real_fstat(reused_fd)
    finally:
        if reused_fd is not None:
            real_close(reused_fd)


def test_executor_redacts_stderr_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _write_fake_codex(
        tmp_path / "codex",
        "printf '%s' 'TOP-SECRET-VALUE /Users/example/private' >&2\nexit 7",
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)

    with pytest.raises(CodexCliError) as captured:
        CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "task")

    assert str(captured.value) == "codex cli failed"
    assert "TOP-SECRET-VALUE" not in str(captured.value)
    assert "/Users/example/private" not in str(captured.value)


def test_executor_reports_missing_output_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _write_fake_codex(tmp_path / "codex", ":")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)

    with pytest.raises(CodexCliError, match="codex output file missing"):
        CodexCliExecutor(str(executable), tmp_path / "runs").run("round-1", "# task")


def test_text_exec_uses_exact_argv_with_local_binary_policy_bypass(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")
    captured: list[list[str]] = []
    real_run = executor._runner.run

    def capture(argv, payload, **kwargs):
        captured.append(list(argv))
        return real_run(argv, payload, **kwargs)

    executor._runner.run = capture
    executor.run("round-1", "task")

    prefix = _secure_exec_prefix(str(executor._runner.path))
    task_dir = Path(captured[-1][len(prefix) + 1])
    output = task_dir / "codex-round-1-output.md"
    assert captured[-1] == _secure_exec_prefix(str(executor._runner.path)) + [
        "--cd",
        str(task_dir),
        "--output-last-message",
        str(output),
        "-",
    ]
    assert not task_dir.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--enable", "shell_tool"],
        ["--disable", "shell_tool"],
        ["--config", "features.shell_tool=true"],
        ["--profile", "unsafe"],
        ["--add-dir", "/tmp"],
    ],
)
def test_text_argv_with_local_binary_policy_bypass_cannot_add_options(
    tmp_path: Path,
    extra: list[str],
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")
    output = executor.run_dir / "codex-round-1-output.md"
    argv = _secure_exec_prefix(str(executor._runner.path)) + [
        "--cd",
        str(executor.run_dir),
        "--output-last-message",
        str(output),
        *extra,
        "-",
    ]

    with pytest.raises(SecureCodexError, match="codex cli failed"):
        validate_codex_argv(argv, executor._runner.path, cwd=executor.run_dir)


@pytest.mark.parametrize("round_key", ["../escape", "a/b", "", "x" * 65, "."])
def test_round_key_with_local_binary_policy_bypass_rejects_invalid_names(
    tmp_path: Path,
    round_key: str,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    executor = CodexCliExecutor(str(executable), tmp_path / "runs")

    with pytest.raises(CodexCliError, match="codex cli failed"):
        executor.run(round_key, "task")

    assert not (tmp_path / "escape-output.md").exists()


def test_symlinked_run_hint_with_local_binary_policy_bypass_is_ignored(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    output = CodexCliExecutor(str(executable), linked / "runs").run("round-1", "task")

    assert output.strip() == "# result"
    assert not (real / "runs").exists()


def test_existing_run_hint_with_local_binary_policy_bypass_is_unchanged(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    run_dir = tmp_path / "runs"
    run_dir.mkdir(mode=0o755)

    executor = CodexCliExecutor(str(executable), run_dir)

    assert executor.run_dir == run_dir.resolve()
    assert stat.S_IMODE(executor.run_dir.stat().st_mode) == 0o755


def test_project_run_dir_with_local_binary_policy_bypass_is_never_used_as_cwd(
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _write_fake_codex(
        tmp_path / "codex",
        'printf \'%s\' "$PWD" > "$output"',
    )
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    project = tmp_path / "course-project"
    project.mkdir(mode=0o755)
    (project / ".git").mkdir()
    run_dir = project / "runs"
    run_dir.mkdir(mode=0o755)
    before_mode = stat.S_IMODE(run_dir.stat().st_mode)

    execution_cwd = Path(CodexCliExecutor(str(executable), run_dir).run("round-1", "task"))

    assert stat.S_IMODE(run_dir.stat().st_mode) == before_mode
    assert not execution_cwd.is_relative_to(project)
    assert not execution_cwd.exists()


def test_text_result_rejects_same_inode_same_size_mutation(monkeypatch, tmp_path):
    path = tmp_path / "result.md"
    path.write_bytes(b"original")
    original = path.stat()
    real_read = os.read
    mutated = False

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(fd, size)
        if chunk and not mutated:
            mutated = True
            with path.open("r+b") as writer:
                writer.write(b"tampered")
                writer.flush()
                os.fsync(writer.fileno())
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        return chunk

    monkeypatch.setattr(codex_cli_module, "_read_output_chunk", mutate_after_read, raising=False)

    with pytest.raises(CodexCliError, match="codex output file is invalid"):
        codex_cli_module._read_output_file(path)


def test_execute_baseexception_terminates_reaps_closes_pipes_and_removes_task_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _write_fake_codex(tmp_path / "codex", "sleep 30")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    spawned: list[Any] = []
    real_spawn = secure_codex_module._spawn_isolated_process
    primary = KeyboardInterrupt("PRIMARY-PROMPT-SECRET")

    def capture_spawn(*args: Any, **kwargs: Any):
        process, process_group_id = real_spawn(*args, **kwargs)
        spawned.append(process)
        return process, process_group_id

    def interrupt(*_args: object, **_kwargs: object) -> bytes:
        raise primary

    monkeypatch.setattr(secure_codex_module, "_spawn_isolated_process", capture_spawn)
    monkeypatch.setattr(secure_codex_module, "_communicate_bounded", interrupt)
    task_path: Path | None = None

    with pytest.raises(KeyboardInterrupt) as error:
        with runner.private_task() as task_dir:
            task_path = task_dir
            output = task_dir / "codex-round-1-output.md"
            argv = _secure_exec_prefix(str(runner.path)) + [
                "--cd",
                str(task_dir),
                "--output-last-message",
                str(output),
                "-",
            ]
            runner.run(argv, b"bounded", cwd=task_dir)

    assert error.value is primary
    assert task_path is not None and not task_path.exists()
    assert len(spawned) == 1
    process = spawned[0]
    assert process.poll() is not None
    assert all(
        pipe is None or pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)
    )


def test_execute_cleanup_failures_do_not_mask_baseexception_and_all_cleanup_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_behavior_uses_local_binary_policy_bypass: _LocalFakeCodexPolicy,
) -> None:
    executable = _successful_codex(tmp_path / "codex")
    _grant_local_binary(execution_behavior_uses_local_binary_policy_bypass, executable)
    runner = CodexCliExecutor(str(executable), tmp_path / "runs")._runner
    process = object()
    primary = SystemExit("PRIMARY-PROMPT-SECRET")
    cleanup_calls: list[str] = []

    monkeypatch.setattr(
        secure_codex_module,
        "_spawn_isolated_process",
        lambda *_args, **_kwargs: (process, 424242),
    )

    def interrupt(*_args: object, **_kwargs: object) -> bytes:
        raise primary

    def fail_terminate(observed_process: object, process_group_id: int) -> None:
        assert observed_process is process
        assert process_group_id == 424242
        cleanup_calls.append("terminate")
        raise RuntimeError("cleanup path /private/tmp/secret")

    def fail_close(observed_process: object) -> None:
        assert observed_process is process
        cleanup_calls.append("close")
        raise RuntimeError("cleanup stderr secret")

    monkeypatch.setattr(secure_codex_module, "_communicate_bounded", interrupt)
    monkeypatch.setattr(secure_codex_module, "_terminate_isolated_process", fail_terminate)
    monkeypatch.setattr(secure_codex_module, "_close_process_pipes", fail_close)

    with runner.private_task() as task_dir:
        with pytest.raises(SystemExit) as error:
            runner._execute(
                [str(runner.path), "--version"],
                b"",
                cwd=task_dir,
                deadline=time.monotonic() + 1,
                cancel_event=None,
                environment={"PATH": "/usr/bin:/bin"},
                max_stdin_bytes=8,
                max_stdout_bytes=8,
                max_stderr_bytes=8,
            )

    assert error.value is primary
    assert cleanup_calls == ["terminate", "close"]


class _CommunicatorProcess:
    stdin = None
    stdout = None
    stderr = None
    returncode = 0

    @staticmethod
    def poll() -> int:
        return 0

    @staticmethod
    def wait(*, timeout: float) -> int:
        del timeout
        return 0


class _FaultingSelector:
    def __init__(
        self,
        *,
        register_error: BaseException | None = None,
        select_error: BaseException | None = None,
        close_error: BaseException | None = None,
        active: bool = False,
    ) -> None:
        self.register_error = register_error
        self.select_error = select_error
        self.close_error = close_error
        self.active = active
        self.close_calls = 0

    def register(self, *_args: object) -> None:
        if self.register_error is not None:
            raise self.register_error

    def get_map(self) -> dict[int, object]:
        return {1: object()} if self.active else {}

    def select(self, *, timeout: float) -> list[object]:
        del timeout
        if self.select_error is not None:
            raise self.select_error
        return []

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _call_faulting_communicator(process: object) -> bytes:
    return secure_codex_module._communicate_bounded(
        process,  # type: ignore[arg-type]
        b"",
        deadline=time.monotonic() + 1,
        cancel_event=None,
        max_stdin_bytes=8,
        max_stdout_bytes=8,
        max_stderr_bytes=8,
    )


def test_bounded_communicator_selector_constructor_failure_is_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = OSError("selector constructor failed")

    def fail_constructor() -> object:
        raise primary

    monkeypatch.setattr(secure_codex_module.selectors, "DefaultSelector", fail_constructor)

    with pytest.raises(OSError) as error:
        _call_faulting_communicator(_CommunicatorProcess())

    assert error.value is primary


def test_bounded_communicator_register_failure_closes_selector_without_masking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = OSError("selector register failed")
    cleanup = OSError("selector close failed")
    selector = _FaultingSelector(register_error=primary, close_error=cleanup)
    process = _CommunicatorProcess()
    process.stdin = SimpleNamespace(fileno=lambda: 17)
    monkeypatch.setattr(secure_codex_module.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(secure_codex_module.os, "set_blocking", lambda *_args: None)

    with pytest.raises(OSError) as error:
        _call_faulting_communicator(process)

    assert error.value is primary
    assert selector.close_calls == 1


def test_bounded_communicator_select_baseexception_survives_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("primary")
    cleanup = OSError("selector close failed")
    selector = _FaultingSelector(select_error=primary, close_error=cleanup, active=True)
    monkeypatch.setattr(secure_codex_module.selectors, "DefaultSelector", lambda: selector)

    with pytest.raises(KeyboardInterrupt) as error:
        _call_faulting_communicator(_CommunicatorProcess())

    assert error.value is primary
    assert selector.close_calls == 1


def test_bounded_communicator_close_failure_is_exposed_without_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = OSError("selector close failed")
    selector = _FaultingSelector(close_error=cleanup)
    monkeypatch.setattr(secure_codex_module.selectors, "DefaultSelector", lambda: selector)

    with pytest.raises(OSError) as error:
        _call_faulting_communicator(_CommunicatorProcess())

    assert error.value is cleanup
    assert selector.close_calls == 1
