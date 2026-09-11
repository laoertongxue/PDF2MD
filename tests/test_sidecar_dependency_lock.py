from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import multiprocessing
import os
import select
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARER = ROOT / "parsing-core-app/scripts/prepare-sidecar-python.sh"
RUNTIME_HELPER = ROOT / "parsing-core-app/scripts/sidecar_runtime.py"


def _load_helper(path: Path = RUNTIME_HELPER) -> ModuleType:
    spec = importlib.util.spec_from_file_location("pdf2md_sidecar_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for member, payload in entries:
            if payload is not None:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            else:
                archive.addfile(member)


def _file_member(name: str, payload: bytes = b"payload") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    return member, payload


def _lock_worker(
    helper_path: str,
    lock_path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    helper = _load_helper(Path(helper_path))
    with helper.secure_build_lock(Path(lock_path)):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("lock test release timed out")


_LOCK_CLEANUP_ACTIONS = (
    "lock unlock",
    "lock close",
    "parent unlock",
    "parent close",
)


@dataclass(frozen=True)
class _CleanupFaultOutcome:
    error: BaseException
    injected_errors: dict[str, OSError]
    events: tuple[str, ...]
    close_attempts: dict[str, int]
    registry_empty: bool
    guard_rejected: bool
    descriptors_closed: bool
    child_reacquired: bool


def _child_can_reacquire_build_lock(lock: Path, *, timeout: float = 2.0) -> bool:
    program = f"""
import importlib.util
import pathlib

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_lock_probe", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
with helper.secure_build_lock(pathlib.Path({str(lock)!r})):
    pass
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _exercise_build_lock_cleanup_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flock_failures: frozenset[str] = frozenset(),
    close_failures: frozenset[str] = frozenset(),
    business_error: BaseException | None = None,
) -> _CleanupFaultOutcome:
    helper = _load_helper()
    lock = tmp_path / "runtime.lock"
    guard: tuple[int, int] | None = None
    caught: BaseException | None = None
    events: list[str] = []
    close_attempts: dict[str, int] = {}
    injected_errors = {
        action: OSError(errno.EIO, f"injected {action} failure") for action in _LOCK_CLEANUP_ACTIONS
    }
    original_flock = helper.fcntl.flock
    original_close = helper.os.close

    def action_for_descriptor(descriptor: int, operation: str) -> str | None:
        if guard is None:
            return None
        if descriptor == guard[1]:
            return f"lock {operation}"
        if descriptor == guard[0]:
            return f"parent {operation}"
        return None

    def failing_flock(descriptor: int, operation: int) -> None:
        action = (
            action_for_descriptor(descriptor, "unlock")
            if operation == helper.fcntl.LOCK_UN
            else None
        )
        if action is not None:
            events.append(action)
            if action in flock_failures:
                raise injected_errors[action]
        original_flock(descriptor, operation)

    def failing_close(descriptor: int) -> None:
        action = action_for_descriptor(descriptor, "close")
        if action is not None:
            attempt = close_attempts.get(action, 0) + 1
            close_attempts[action] = attempt
            events.append(action if attempt == 1 else f"{action}:retry")
            if action in close_failures and attempt == 1:
                raise injected_errors[action]
        original_close(descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(helper.fcntl, "flock", failing_flock)
        patcher.setattr(helper.os, "close", failing_close)
        try:
            with helper.secure_build_lock(lock, cleanup_root=tmp_path) as acquired:
                guard = acquired
                helper._require_active_cleanup_lock(guard, tmp_path)
                if business_error is not None:
                    raise business_error
        except BaseException as error:
            caught = error

    assert guard is not None
    assert caught is not None
    registry_empty = helper._ACTIVE_CLEANUP_LOCKS == {}
    try:
        helper._require_active_cleanup_lock(guard, tmp_path)
    except ValueError:
        guard_rejected = True
    else:
        guard_rejected = False

    descriptors_closed = True
    for descriptor in guard:
        try:
            os.fstat(descriptor)
        except OSError as error:
            descriptors_closed = descriptors_closed and error.errno == errno.EBADF
        else:
            descriptors_closed = False

    child_reacquired = _child_can_reacquire_build_lock(lock)
    for descriptor in reversed(guard):
        try:
            original_close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise

    return _CleanupFaultOutcome(
        error=caught,
        injected_errors=injected_errors,
        events=tuple(events),
        close_attempts=dict(close_attempts),
        registry_empty=registry_empty,
        guard_rejected=guard_rejected,
        descriptors_closed=descriptors_closed,
        child_reacquired=child_reacquired,
    )


@dataclass(frozen=True)
class _DescriptorReuseOutcome:
    error: OSError
    close_calls: int
    replacement_survived: bool
    replacement_identity: tuple[int, int]


def _exercise_close_error_after_descriptor_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reopen_same_inode: bool,
) -> _DescriptorReuseOutcome:
    helper = _load_helper()
    original = tmp_path / "original.lock"
    other = tmp_path / "other.lock"
    original.write_bytes(b"original")
    other.write_bytes(b"other")
    descriptor = os.open(original, os.O_RDWR)
    reopened_path = original if reopen_same_inode else other
    original_close = helper.os.close
    injected_error = OSError(errno.EIO, "close result is indeterminate")
    close_calls = 0
    reopened_descriptor: int | None = None

    def close_then_reuse(candidate: int) -> None:
        nonlocal close_calls, reopened_descriptor
        close_calls += 1
        if close_calls == 1:
            original_close(candidate)
            reopened_descriptor = os.open(reopened_path, os.O_RDWR)
            assert reopened_descriptor == candidate
            raise injected_error
        original_close(candidate)

    cleanup_errors: list[tuple[str, Exception]] = []
    with monkeypatch.context() as patcher:
        patcher.setattr(helper.os, "close", close_then_reuse)
        helper._close_owned_lock_descriptor(
            descriptor,
            "lock close",
            cleanup_errors,
        )

    assert cleanup_errors == [("lock close", injected_error)]
    replacement_survived = False
    replacement_identity = (-1, -1)
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        replacement_survived = True
        replacement_identity = (metadata.st_dev, metadata.st_ino)
        original_close(descriptor)

    return _DescriptorReuseOutcome(
        error=injected_error,
        close_calls=close_calls,
        replacement_survived=replacement_survived,
        replacement_identity=replacement_identity,
    )


def _crash_atomic_install_after_commit(
    staged: Path, target: Path
) -> subprocess.CompletedProcess[str]:
    program = f"""
import importlib.util
import os
import pathlib

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_crash_runtime", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
original_write = helper._write_new_file

def crash_after_commit(path, payload, mode=0o600):
    identity = original_write(path, payload, mode)
    if path.suffix == ".committed":
        os._exit(92)
    return identity

helper._write_new_file = crash_after_commit
helper.atomic_install(pathlib.Path({str(staged)!r}), pathlib.Path({str(target)!r}))
"""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
    )


def _crash_recovery_before_tombstone_removal(target: Path) -> subprocess.CompletedProcess[str]:
    program = f"""
import importlib.util
import os
import pathlib

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_crash_recovery", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

def crash_before_removal(*args, **kwargs):
    os._exit(93)

helper._remove_verified_tombstone = crash_before_removal
helper.recover_install_transactions(pathlib.Path({str(target)!r}))
"""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
    )


def _crash_recovery_after_root_claim(
    target: Path,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    program = f"""
import importlib.util
import os
import pathlib

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_claim_crash", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
original_renameatx = helper._renameatx

def crash_after_claim(source_fd, source_name, destination_fd, destination_name, flags):
    original_renameatx(source_fd, source_name, destination_fd, destination_name, flags)
    marker = ".{phase}."
    if destination_name.startswith(".sidecar-runtime.claim.") and marker in destination_name:
        os._exit(94)

helper._renameatx = crash_after_claim
helper.recover_install_transactions(pathlib.Path({str(target)!r}))
"""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
    )


def _blocking_recovery_worker(
    helper_path: str,
    target_path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    helper = _load_helper(Path(helper_path))
    original_remove = helper._remove_verified_tombstone

    def wait_inside_cleanup(*args: object, **kwargs: object) -> None:
        entered.set()
        if not release.wait(10):
            raise RuntimeError("cleanup test release timed out")
        original_remove(*args, **kwargs)

    helper._remove_verified_tombstone = wait_inside_cleanup
    helper.recover_install_transactions(Path(target_path))


def _committed_cleanup_state(
    tmp_path: Path,
) -> tuple[ModuleType, Path, Path, str, object]:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.namespace"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr
    journal = next((tmp_path / ".sidecar-runtime-transactions").glob("*.json"))
    return helper, target, staged, journal.stem, helper.file_identity(staged)


def test_archive_checksum_validation_and_extraction_use_open_inode(tmp_path: Path) -> None:
    helper = _load_helper()
    archive_path = tmp_path / "runtime.tar.gz"
    replacement = tmp_path / "replacement.tar.gz"
    _tar(archive_path, [_file_member("python/original.txt", b"original")])
    _tar(replacement, [_file_member("python/replacement.txt", b"replacement")])
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    archive_fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.replace(replacement, archive_path)
        destination = tmp_path / "destination"
        helper.extract_verified_archive_fd(archive_fd, destination, expected)
    finally:
        os.close(archive_fd)

    assert (destination / "python/original.txt").read_bytes() == b"original"
    assert not (destination / "python/replacement.txt").exists()


def test_launcher_emitter_returns_the_production_launcher_without_writes(tmp_path: Path) -> None:
    helper = _load_helper()
    helper_before = RUNTIME_HELPER.read_bytes()

    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-B", str(RUNTIME_HELPER), "emit-launcher"],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == helper.LAUNCHER.encode()
    assert result.stderr == b""
    assert RUNTIME_HELPER.read_bytes() == helper_before


@pytest.mark.parametrize(
    "names",
    [
        ["python/a.txt", "python/a.txt"],
        ["python/Readme", "python/README"],
        ["python/cafe\N{COMBINING ACUTE ACCENT}", "python/caf\N{LATIN SMALL LETTER E WITH ACUTE}"],
    ],
)
def test_archive_rejects_duplicate_casefold_and_unicode_names(
    tmp_path: Path, names: list[str]
) -> None:
    helper = _load_helper()
    archive_path = tmp_path / "runtime.tar.gz"
    _tar(archive_path, [_file_member(name) for name in names])
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ValueError, match="archive member collision"):
            helper.extract_verified_archive_fd(archive_fd, tmp_path / "destination", expected)
    finally:
        os.close(archive_fd)


def test_archive_rejects_member_and_expanded_size_limits(tmp_path: Path) -> None:
    helper = _load_helper()
    archive_path = tmp_path / "runtime.tar.gz"
    _tar(
        archive_path,
        [_file_member("python/one", b"1234"), _file_member("python/two", b"5678")],
    )
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ValueError, match="archive resource limit"):
            helper.extract_verified_archive_fd(
                archive_fd,
                tmp_path / "destination",
                expected,
                max_members=1,
                max_expanded_bytes=7,
            )
    finally:
        os.close(archive_fd)


def test_archive_rejects_hardlinks_even_when_target_is_contained(tmp_path: Path) -> None:
    helper = _load_helper()
    archive_path = tmp_path / "runtime.tar.gz"
    hardlink = tarfile.TarInfo("python/copy")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "python/original"
    _tar(archive_path, [_file_member("python/original"), (hardlink, None)])
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ValueError, match="hard link"):
            helper.extract_verified_archive_fd(archive_fd, tmp_path / "destination", expected)
    finally:
        os.close(archive_fd)


def test_archive_detects_in_place_mutation_after_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    archive_path = tmp_path / "runtime.tar.gz"
    _tar(archive_path, [_file_member("python/original", b"original")])
    expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_fd = os.open(archive_path, os.O_RDWR | os.O_NOFOLLOW)
    original_fsync = helper._fsync_directory
    mutated = False

    def mutate_after_extraction(path: Path) -> None:
        nonlocal mutated
        if not mutated:
            mutated = True
            final_byte = os.pread(archive_fd, 1, os.fstat(archive_fd).st_size - 1)
            os.pwrite(archive_fd, bytes([final_byte[0] ^ 0x01]), os.fstat(archive_fd).st_size - 1)
            os.fsync(archive_fd)
        original_fsync(path)

    monkeypatch.setattr(helper, "_fsync_directory", mutate_after_extraction)
    try:
        with pytest.raises(ValueError, match="archive content changed"):
            helper.extract_verified_archive_fd(archive_fd, tmp_path / "destination", expected)
    finally:
        os.close(archive_fd)


def test_download_enforces_compressed_limit_while_streaming_and_removes_partial_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    terminated = tmp_path / "terminated"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/bash\n"
        "trap 'printf terminated > \"$PDF2MD_TERMINATED\"; exit 143' TERM\n"
        "i=0\n"
        "while (( i < 128 )); do printf '0123456789abcdef'; (( i += 1 )); done\n"
        "/bin/sleep 3\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setattr(helper, "SYSTEM_CURL", fake_curl)
    monkeypatch.setattr(helper, "DEFAULT_MAX_ARCHIVE_BYTES", 1024)
    monkeypatch.setattr(helper, "_require_system_tool", lambda _path: None)
    original_environment: Callable[[Path], dict[str, str]] = helper.sanitized_build_environment

    def environment(home: Path) -> dict[str, str]:
        result = original_environment(home)
        result["PDF2MD_TERMINATED"] = str(terminated)
        return result

    monkeypatch.setattr(helper, "sanitized_build_environment", environment)

    with helper.secure_build_lock(
        tmp_path / "download.lock",
        cleanup_root=cache,
    ) as cleanup_guard:
        with pytest.raises(ValueError, match="compressed size"):
            helper._download_archive(
                cache,
                "python.tar.gz",
                "https://github.com/example/python.tar.gz",
                "0" * 64,
                cleanup_guard,
            )

    assert terminated.read_text(encoding="utf-8") == "terminated"
    assert list(cache.glob(".python.tar.gz.download.*")) == []


def test_runtime_manifest_binds_file_mode_and_symlink_target(tmp_path: Path) -> None:
    helper = _load_helper()
    runtime = tmp_path / "python"
    runtime.mkdir()
    executable = runtime / "python3.12"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    link = runtime / "python3"
    link.symlink_to("python3.12")
    manifest = helper.runtime_manifest_bytes(runtime)

    helper.verify_runtime_manifest_bytes(runtime, manifest)
    executable.chmod(0o700)
    with pytest.raises(ValueError, match="runtime manifest mismatch"):
        helper.verify_runtime_manifest_bytes(runtime, manifest)

    executable.chmod(0o755)
    link.unlink()
    link.symlink_to("missing")
    with pytest.raises(ValueError, match="runtime manifest mismatch"):
        helper.verify_runtime_manifest_bytes(runtime, manifest)


def test_runtime_manifest_rejects_hardlinks_and_extended_attributes(tmp_path: Path) -> None:
    helper = _load_helper()
    runtime = tmp_path / "python"
    runtime.mkdir()
    source = runtime / "source"
    source.write_bytes(b"content")
    os.link(source, runtime / "alias")
    with pytest.raises(ValueError, match="hard link"):
        helper.runtime_manifest_bytes(runtime)

    (runtime / "alias").unlink()
    if hasattr(os, "setxattr"):
        try:
            os.setxattr(source, "user.pdf2md-test", b"value", follow_symlinks=False)
        except OSError:
            pytest.skip("extended attributes are unavailable on this filesystem")
        with pytest.raises(ValueError, match="extended attribute"):
            helper.runtime_manifest_bytes(runtime)


def test_atomic_install_rejects_staged_inode_swap_without_touching_target(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    staged = tmp_path / ".runtime.staged"
    staged.mkdir()
    (staged / "marker").write_text("desired", encoding="utf-8")
    expected_staged = helper.file_identity(staged)

    displaced = tmp_path / ".runtime.displaced"
    staged.rename(displaced)
    staged.mkdir()
    (staged / "marker").write_text("competitor", encoding="utf-8")

    with pytest.raises(ValueError, match="staged runtime identity changed"):
        helper.atomic_install(staged, target, expected_staged=expected_staged)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (staged / "marker").read_text(encoding="utf-8") == "competitor"


def test_atomic_install_rolls_back_before_deleting_old_runtime_when_verifier_fails(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.verify"
    staged.mkdir()
    (staged / "marker").write_text("new", encoding="utf-8")

    def reject(_published: Path) -> None:
        raise ValueError("full verification failed")

    with pytest.raises(ValueError, match="full verification failed"):
        helper.atomic_install(staged, target, verifier=reject)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (staged / "marker").read_text(encoding="utf-8") == "new"
    shutil.rmtree(staged)
    helper.recover_install_transactions(target)


def test_atomic_install_rejects_target_inode_swap_and_restores_competitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.target-race"
    staged.mkdir()
    (staged / "marker").write_text("new", encoding="utf-8")
    competitor = tmp_path / "competitor"
    competitor.mkdir()
    (competitor / "marker").write_text("competitor", encoding="utf-8")
    displaced = tmp_path / "displaced-old"
    original_renamex = helper._renamex
    raced = False

    def race_target(source: Path, destination: Path, flags: int) -> None:
        nonlocal raced
        if not raced and flags == helper.RENAME_SWAP and destination == target:
            raced = True
            target.rename(displaced)
            competitor.rename(target)
        original_renamex(source, destination, flags)

    monkeypatch.setattr(helper, "_renamex", race_target)

    with pytest.raises(ValueError, match="target runtime identity changed"):
        helper.atomic_install(staged, target)

    assert (target / "marker").read_text(encoding="utf-8") == "competitor"
    assert (staged / "marker").read_text(encoding="utf-8") == "new"
    assert (displaced / "marker").read_text(encoding="utf-8") == "old"


def test_atomic_install_with_missing_replaced_target_withdraws_new_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "runtime"
    target.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.missing-target"
    staged.mkdir()
    (staged / "marker").write_text("new", encoding="utf-8")
    original_renamex = helper._renamex
    raced = False

    def remove_replaced_target(source: Path, destination: Path, flags: int) -> None:
        nonlocal raced
        original_renamex(source, destination, flags)
        if not raced and flags == helper.RENAME_SWAP and destination == target:
            raced = True
            shutil.rmtree(source)

    monkeypatch.setattr(helper, "_renamex", remove_replaced_target)

    with pytest.raises(ValueError, match="target runtime identity changed"):
        helper.atomic_install(staged, target)

    assert not target.exists()
    assert (staged / "marker").read_text(encoding="utf-8") == "new"


def test_install_transaction_recovers_real_process_exit_after_directory_swap(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.crash"
    runtime = staged / "python"
    runtime.mkdir(parents=True)
    (runtime / "payload").write_text("new", encoding="utf-8")
    (staged / ".runtime-manifest.json").write_bytes(helper.runtime_manifest_bytes(runtime))

    crashing_helper = tmp_path / "sidecar_runtime_crash.py"
    source = RUNTIME_HELPER.read_text(encoding="utf-8")
    checkpoint = "    target_after = _identity_or_none(target)\n"
    assert source.count(checkpoint) == 1
    crashing_helper.write_text(
        source.replace(checkpoint, "    os._exit(91)\n" + checkpoint),
        encoding="utf-8",
    )
    crashed = subprocess.run(
        [
            sys.executable,
            str(crashing_helper),
            "atomic-install",
            str(staged),
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 91
    assert (target / "python/payload").read_text(encoding="utf-8") == "new"
    assert (staged / "old-runtime").read_text(encoding="utf-8") == "old"

    helper.recover_install_transactions(target)

    assert (target / "python/payload").read_text(encoding="utf-8") == "new"
    assert not staged.exists()
    commits = list((tmp_path / ".sidecar-runtime-transactions").glob("*.committed"))
    assert len(commits) == 1


def test_committed_recovery_rejects_replaced_target_and_preserves_last_known_good(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.after-commit"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")

    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr
    assert (staged / "old-runtime").read_text(encoding="utf-8") == "old"

    published = tmp_path / "published-runtime"
    target.rename(published)
    competitor = tmp_path / "competitor"
    competitor.mkdir()
    (competitor / "marker").write_text("competitor", encoding="utf-8")
    competitor.rename(target)

    with pytest.raises(ValueError, match="committed runtime target identity changed"):
        helper.recover_install_transactions(target)

    assert (target / "marker").read_text(encoding="utf-8") == "competitor"
    assert (staged / "old-runtime").read_text(encoding="utf-8") == "old"
    assert (published / "new-runtime").read_text(encoding="utf-8") == "new"


def test_committed_cleanup_rejects_tombstone_replacement_before_private_delete_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.cleanup-race"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    competitor = tmp_path / "cleanup-competitor"
    competitor.mkdir()
    (competitor / "marker").write_text("competitor", encoding="utf-8")
    displaced = tmp_path / "displaced-tombstone"
    original_renamex = helper._renamex
    raced = False

    def replace_tombstone_after_move(source: Path, destination: Path, flags: int) -> None:
        nonlocal raced
        original_renamex(source, destination, flags)
        if not raced and source == staged and ".tombstone." in destination.name:
            raced = True
            destination.rename(displaced)
            competitor.rename(destination)

    monkeypatch.setattr(helper, "_renamex", replace_tombstone_after_move)

    with pytest.raises(ValueError, match="cleanup tombstone identity changed"):
        helper.recover_install_transactions(target)

    assert raced
    assert (target / "new-runtime").read_text(encoding="utf-8") == "new"
    assert (displaced / "old-runtime").read_text(encoding="utf-8") == "old"
    tombstones = list(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    assert len(tombstones) == 1
    assert (tombstones[0] / "marker").read_text(encoding="utf-8") == "competitor"


def test_committed_cleanup_rejects_file_replacement_before_private_delete_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-file").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.file-race"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr
    old_file_identity = helper.file_identity(staged / "old-file")

    competitor = tmp_path / "file-competitor"
    competitor.write_text("old", encoding="utf-8")
    competitor_identity = helper.file_identity(competitor)
    displaced = tmp_path / "displaced-old-file"
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = helper.os.stat
    observations = 0
    raced = False

    def replace_file_after_identity_check(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal observations, raced
        metadata = original_stat(path, *args, **kwargs)
        directory_descriptor = kwargs.get("dir_fd")
        if helper._identity_from_stat(metadata) == old_file_identity:
            observations += 1
        if (
            not raced
            and observations == 2
            and isinstance(path, str)
            and isinstance(directory_descriptor, int)
        ):
            raced = True
            os.rename(
                path,
                displaced.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.rename(
                competitor.name,
                path,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        return metadata

    monkeypatch.setattr(helper.os, "stat", replace_file_after_identity_check)
    try:
        with pytest.raises(ValueError, match="cleanup .*identity changed"):
            helper.recover_install_transactions(target)
    finally:
        os.close(parent_descriptor)

    assert raced
    assert displaced.read_text(encoding="utf-8") == "old"
    cleanup_roots = list(tmp_path.glob(".sidecar-runtime.tombstone.*")) + list(
        tmp_path.glob(".sidecar-runtime.claim.*")
    )
    assert len(cleanup_roots) == 1
    remaining = [entry for entry in cleanup_roots[0].iterdir() if entry.is_file()]
    assert len(remaining) == 1
    assert remaining[0].read_text(encoding="utf-8") == "old"
    assert helper.file_identity(remaining[0]) == competitor_identity


def test_committed_cleanup_rejects_directory_replacement_before_private_delete_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    old_directory = target / "old-directory"
    old_directory.mkdir(parents=True)
    staged = tmp_path / ".sidecar-runtime.staged.directory-race"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    old_identity = helper.file_identity(staged / "old-directory")
    competitor = tmp_path / "directory-competitor"
    competitor.mkdir()
    competitor_identity = helper.file_identity(competitor)
    displaced = tmp_path / "displaced-old-directory"
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = helper.os.stat
    observations = 0
    raced = False

    def replace_directory_after_final_identity_check(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal observations, raced
        metadata = original_stat(path, *args, **kwargs)
        directory_descriptor = kwargs.get("dir_fd")
        identity = helper._identity_from_stat(metadata)
        if identity == old_identity:
            observations += 1
        if (
            not raced
            and observations == 2
            and isinstance(path, str)
            and isinstance(directory_descriptor, int)
        ):
            raced = True
            os.rename(
                path,
                displaced.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.rename(
                competitor.name,
                path,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        return metadata

    monkeypatch.setattr(helper.os, "stat", replace_directory_after_final_identity_check)
    try:
        with pytest.raises(ValueError, match="cleanup .*identity changed"):
            helper.recover_install_transactions(target)
    finally:
        os.close(parent_descriptor)

    assert raced
    assert displaced.is_dir()
    cleanup_roots = list(tmp_path.glob(".sidecar-runtime.tombstone.*")) + list(
        tmp_path.glob(".sidecar-runtime.claim.*")
    )
    assert len(cleanup_roots) == 1
    remaining = [entry for entry in cleanup_roots[0].iterdir() if entry.is_dir()]
    assert len(remaining) == 1
    assert helper.file_identity(remaining[0]) == competitor_identity


def test_committed_cleanup_rejects_root_replacement_before_private_delete_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    staged = tmp_path / ".sidecar-runtime.staged.root-race"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    competitor = tmp_path / "root-competitor"
    competitor.mkdir()
    competitor_identity = helper.file_identity(competitor)
    displaced = tmp_path / "displaced-root-tombstone"
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = helper.os.stat
    raced = False

    def replace_root_after_identity_check(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal raced
        metadata = original_stat(path, *args, **kwargs)
        directory_descriptor = kwargs.get("dir_fd")
        if (
            not raced
            and isinstance(path, str)
            and path.startswith(".sidecar-runtime.tombstone.")
            and isinstance(directory_descriptor, int)
        ):
            raced = True
            os.rename(
                path,
                displaced.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.rename(
                competitor.name,
                path,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        return metadata

    monkeypatch.setattr(helper.os, "stat", replace_root_after_identity_check)
    try:
        with pytest.raises(ValueError, match="cleanup .*identity changed"):
            helper.recover_install_transactions(target)
    finally:
        os.close(parent_descriptor)

    assert raced
    assert displaced.is_dir()
    tombstones = list(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    assert len(tombstones) == 1
    assert helper.file_identity(tombstones[0]) == competitor_identity


def test_committed_cleanup_recovers_same_tombstone_after_repeated_real_exits(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.repeated-crash"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    directory_snapshots: list[list[str]] = []
    for _ in range(3):
        recovery = _crash_recovery_before_tombstone_removal(target)
        assert recovery.returncode == 93, recovery.stderr
        directory_snapshots.append(sorted(path.name for path in tmp_path.iterdir()))

    assert directory_snapshots[0] == directory_snapshots[1] == directory_snapshots[2]
    tombstones = list(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    assert len(tombstones) == 1
    journal = next((tmp_path / ".sidecar-runtime-transactions").glob("*.json"))
    assert tombstones[0].name == f".sidecar-runtime.tombstone.{journal.stem}"

    helper.recover_install_transactions(target)

    assert not list(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    assert not list(tmp_path.glob(".sidecar-runtime.claim.*"))
    finished = list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))
    assert len(finished) == 1


def test_committed_cleanup_rejects_same_content_foreign_tombstone_inode(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("same-content", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.foreign-tombstone"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    recovery = _crash_recovery_before_tombstone_removal(target)
    assert recovery.returncode == 93, recovery.stderr
    tombstone = next(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    expected_identity = helper.file_identity(tombstone)
    displaced = tmp_path / "expected-tombstone"
    tombstone.rename(displaced)
    shutil.copytree(displaced, tombstone)
    foreign_identity = helper.file_identity(tombstone)
    assert foreign_identity != expected_identity

    with pytest.raises(ValueError, match="cleanup tombstone identity changed"):
        helper.recover_install_transactions(target)

    assert (displaced / "old-runtime").read_text(encoding="utf-8") == "same-content"
    assert (tombstone / "old-runtime").read_text(encoding="utf-8") == "same-content"
    assert helper.file_identity(tombstone) == foreign_identity


def test_committed_cleanup_recovers_after_each_root_claim_phase_exit(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "old-runtime").write_text("old", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.claim-crash"
    staged.mkdir()
    (staged / "new-runtime").write_text("new", encoding="utf-8")
    crashed = _crash_atomic_install_after_commit(staged, target)
    assert crashed.returncode == 92, crashed.stderr

    claim_counts: list[int] = []
    for phase in ("c", "d"):
        recovery = _crash_recovery_after_root_claim(target, phase)
        assert recovery.returncode == 94, recovery.stderr
        cleanup_roots = list(tmp_path.glob(".sidecar-runtime.tombstone.*")) + list(
            tmp_path.glob(".sidecar-runtime.claim.*")
        )
        claim_counts.append(len(cleanup_roots))

    assert claim_counts == [1, 1]

    helper.recover_install_transactions(target)

    assert (target / "new-runtime").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".sidecar-runtime.tombstone.*"))
    assert not list(tmp_path.glob(".sidecar-runtime.claim.*"))


@pytest.mark.parametrize("root_phase", ["candidate", "tombstone", "c", "d"])
def test_cleanup_namespace_rejects_same_transaction_root_with_mismatched_identity(
    tmp_path: Path,
    root_phase: str,
) -> None:
    helper, target, staged, transaction_id, expected = _committed_cleanup_state(tmp_path)
    preserved = tmp_path / f"preserved-old-runtime-{root_phase}"

    if root_phase == "candidate":
        staged.rename(preserved)
        shutil.copytree(preserved, staged)
    elif root_phase == "tombstone":
        staged.rename(preserved)
        shutil.copytree(
            preserved,
            tmp_path / f".sidecar-runtime.tombstone.{transaction_id}",
        )
    else:
        mismatched = helper.FileIdentity(expected.device, expected.inode + 1, expected.file_type)
        staged.rename(tmp_path / helper._cleanup_claim_name(transaction_id, root_phase, mismatched))

    with pytest.raises(ValueError, match="cleanup .*identity"):
        helper.recover_install_transactions(target)

    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))
    assert any(path.is_dir() and (path / "old-runtime").is_file() for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    "suffix",
    [
        "x.0000000000000001.0000000000000002.00004000",
        "c.not-hex.0000000000000002.00004000",
        "d.0000000000000001.short.00004000",
    ],
)
def test_cleanup_namespace_rejects_malformed_same_transaction_claim(
    tmp_path: Path,
    suffix: str,
) -> None:
    _helper, target, staged, transaction_id, _expected = _committed_cleanup_state(tmp_path)
    malformed = tmp_path / f".sidecar-runtime.claim.{transaction_id}.{suffix}"
    staged.rename(malformed)

    with pytest.raises(ValueError, match="malformed cleanup claim"):
        _helper.recover_install_transactions(target)

    assert (malformed / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


@pytest.mark.parametrize("root_kind", ["tombstone", "claim"])
def test_cleanup_namespace_rejects_malformed_transaction_id(
    tmp_path: Path,
    root_kind: str,
) -> None:
    helper, target, staged, transaction_id, expected = _committed_cleanup_state(tmp_path)
    malformed_id = f"{transaction_id[:-1]}g"
    if root_kind == "tombstone":
        malformed = tmp_path / f".sidecar-runtime.tombstone.{malformed_id}"
    else:
        malformed = tmp_path / (
            f".sidecar-runtime.claim.{malformed_id}.c."
            f"{expected.device:016x}.{expected.inode:016x}.{expected.file_type:08x}"
        )
    staged.rename(malformed)

    with pytest.raises(ValueError, match="malformed cleanup"):
        helper.recover_install_transactions(target)

    assert (malformed / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


def test_cleanup_namespace_rejects_multiple_roots_for_one_transaction(tmp_path: Path) -> None:
    helper, target, staged, transaction_id, _expected = _committed_cleanup_state(tmp_path)
    tombstone = tmp_path / f".sidecar-runtime.tombstone.{transaction_id}"
    staged.rename(tombstone)
    shutil.copytree(tombstone, staged)

    with pytest.raises(ValueError, match="multiple cleanup roots"):
        helper.recover_install_transactions(target)

    assert (tombstone / "old-runtime").read_text(encoding="utf-8") == "old"
    assert (staged / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


def test_cleanup_namespace_rejects_group_or_world_writable_root(tmp_path: Path) -> None:
    helper, target, staged, transaction_id, _expected = _committed_cleanup_state(tmp_path)
    tombstone = tmp_path / f".sidecar-runtime.tombstone.{transaction_id}"
    staged.rename(tombstone)
    tombstone.chmod(0o777)

    with pytest.raises(ValueError, match="cleanup root owner/mode"):
        helper.recover_install_transactions(target)

    assert (tombstone / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


@pytest.mark.parametrize(
    "reported_owner",
    [
        pytest.param(0, marks=pytest.mark.skipif(os.geteuid() == 0, reason="running as root")),
        os.geteuid() + 1,
    ],
)
def test_cleanup_namespace_rejects_owner_other_than_the_current_build_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_owner: int,
) -> None:
    helper, target, staged, transaction_id, _expected = _committed_cleanup_state(tmp_path)
    tombstone = tmp_path / f".sidecar-runtime.tombstone.{transaction_id}"
    staged.rename(tombstone)
    original_stat = helper.os.stat

    def report_untrusted_owner(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        metadata = original_stat(path, *args, **kwargs)
        if isinstance(path, str) and path == tombstone.name and kwargs.get("dir_fd") is not None:
            values = list(metadata)
            values[4] = reported_owner
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(helper.os, "stat", report_untrusted_owner)

    with pytest.raises(ValueError, match="cleanup root owner/mode"):
        helper.recover_install_transactions(target)

    assert (tombstone / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


@pytest.mark.parametrize("replacement_type", ["file", "symlink", "fifo"])
def test_cleanup_namespace_rejects_non_directory_root_type(
    tmp_path: Path,
    replacement_type: str,
) -> None:
    helper, target, staged, transaction_id, _expected = _committed_cleanup_state(tmp_path)
    preserved = tmp_path / "preserved-old-runtime"
    staged.rename(preserved)
    tombstone = tmp_path / f".sidecar-runtime.tombstone.{transaction_id}"
    if replacement_type == "file":
        tombstone.write_text("foreign", encoding="utf-8")
    elif replacement_type == "symlink":
        tombstone.symlink_to(preserved.name)
    else:
        os.mkfifo(tombstone, 0o600)

    with pytest.raises(ValueError, match="cleanup .*identity|cleanup root type"):
        helper.recover_install_transactions(target)

    assert (preserved / "old-runtime").read_text(encoding="utf-8") == "old"
    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


def test_cleanup_namespace_scan_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper, target, _staged, _transaction_id, _expected = _committed_cleanup_state(tmp_path)
    monkeypatch.setattr(helper, "MAX_CLEANUP_NAMESPACE_ENTRIES", 4, raising=False)
    for index in range(5):
        (tmp_path / f"unrelated-{index}").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="cleanup namespace entry limit"):
        helper.recover_install_transactions(target)

    assert not list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))


def test_destructive_cleanup_requires_an_active_exclusive_lock_guard(tmp_path: Path) -> None:
    helper = _load_helper()
    candidate = tmp_path / ".sidecar-runtime.staged.no-lock"
    candidate.mkdir()
    (candidate / "old-runtime").write_text("old", encoding="utf-8")
    expected = helper.file_identity(candidate)

    with pytest.raises(ValueError, match="active exclusive cleanup lock"):
        helper._retire_cleanup_candidate(candidate, expected, uuid.uuid4().hex)

    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ValueError, match="active exclusive cleanup lock"):
            helper._remove_private_cleanup_claim(
                parent_descriptor,
                candidate.name,
                expected,
                directory=True,
                cleanup_guard=None,
            )
    finally:
        os.close(parent_descriptor)

    assert (candidate / "old-runtime").read_text(encoding="utf-8") == "old"


def test_cooperating_recovery_processes_cannot_cleanup_concurrently(tmp_path: Path) -> None:
    _helper, target, _staged, _transaction_id, _expected = _committed_cleanup_state(tmp_path)
    context = multiprocessing.get_context("fork")
    first_entered = context.Event()
    first_release = context.Event()
    second_entered = context.Event()
    second_release = context.Event()
    second_release.set()
    first = context.Process(
        target=_blocking_recovery_worker,
        args=(str(RUNTIME_HELPER), str(target), first_entered, first_release),
    )
    second = context.Process(
        target=_blocking_recovery_worker,
        args=(str(RUNTIME_HELPER), str(target), second_entered, second_release),
    )
    first.start()
    second_started = False
    try:
        assert first_entered.wait(5)
        second.start()
        second_started = True
        assert not second_entered.wait(0.3)
    finally:
        first_release.set()
        first.join(5)
        if first.is_alive():
            first.terminate()
            first.join(5)
        if second_started:
            second.join(5)
            if second.is_alive():
                second.terminate()
                second.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert not second_entered.is_set()
    assert len(list((tmp_path / ".sidecar-runtime-transactions").glob("*.finished"))) == 1


def test_completed_install_transaction_history_is_bounded(tmp_path: Path) -> None:
    helper = _load_helper()
    assert helper.COMPLETED_TRANSACTION_RETENTION == 2
    target = tmp_path / "sidecar-runtime"

    entry_counts: list[int] = []
    for index in range(8):
        staged = tmp_path / f".sidecar-runtime.staged.retention-{index}"
        staged.mkdir()
        (staged / "marker").write_text(str(index), encoding="utf-8")
        helper.atomic_install(staged, target)
        transaction_directory = tmp_path / ".sidecar-runtime-transactions"
        entry_counts.append(
            len([path for path in transaction_directory.iterdir() if path.name != ".cleanup.lock"])
        )

    transaction_directory = tmp_path / ".sidecar-runtime-transactions"
    assert entry_counts[-3:] == [6, 6, 6]
    assert len(list(transaction_directory.glob("*.json"))) == 2
    assert len(list(transaction_directory.glob("*.committed"))) == 2
    assert len(list(transaction_directory.glob("*.finished"))) == 2
    assert (target / "marker").read_text(encoding="utf-8") == "7"


def test_completed_transaction_reclaim_recovers_after_real_exit(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    for index in range(helper.COMPLETED_TRANSACTION_RETENTION):
        staged = tmp_path / f".sidecar-runtime.staged.seed-{index}"
        staged.mkdir()
        (staged / "marker").write_text(str(index), encoding="utf-8")
        helper.atomic_install(staged, target)

    staged = tmp_path / ".sidecar-runtime.staged.reclaim-crash"
    staged.mkdir()
    (staged / "marker").write_text("latest", encoding="utf-8")
    program = f"""
import importlib.util
import os
import pathlib

helper_path = pathlib.Path({str(RUNTIME_HELPER)!r})
spec = importlib.util.spec_from_file_location("pdf2md_reclaim_crash", helper_path)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
original_capture = helper._capture_transaction_family_member
crashed = False

def crash_after_capture(*args, **kwargs):
    global crashed
    result = original_capture(*args, **kwargs)
    if not crashed:
        crashed = True
        os._exit(95)
    return result

helper._capture_transaction_family_member = crash_after_capture
helper.atomic_install(pathlib.Path({str(staged)!r}), pathlib.Path({str(target)!r}))
"""
    crashed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 95, crashed.stderr
    helper.recover_install_transactions(target)

    transaction_directory = tmp_path / ".sidecar-runtime-transactions"
    assert len(list(transaction_directory.glob("*.json"))) == 2
    assert len(list(transaction_directory.glob("*.committed"))) == 2
    assert len(list(transaction_directory.glob("*.finished"))) == 2
    assert not list(transaction_directory.glob(".sidecar-runtime.transaction-claim.*"))
    assert (target / "marker").read_text(encoding="utf-8") == "latest"


def test_transaction_namespace_rejects_foreign_claim_before_publication(tmp_path: Path) -> None:
    helper = _load_helper()
    target = tmp_path / "sidecar-runtime"
    target.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    transaction_directory = tmp_path / ".sidecar-runtime-transactions"
    transaction_directory.mkdir(mode=0o700)
    foreign = transaction_directory / (
        f".sidecar-runtime.transaction-claim.{uuid.uuid4().hex}.unknown."
        "0000000000000001.0000000000000002.00008000"
    )
    foreign.write_text("preserve", encoding="utf-8")
    staged = tmp_path / ".sidecar-runtime.staged.foreign-claim"
    staged.mkdir()
    (staged / "marker").write_text("new", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed transaction cleanup claim"):
        helper.atomic_install(staged, target)

    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (staged / "marker").read_text(encoding="utf-8") == "new"


def test_install_transaction_namespace_scan_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(helper, "MAX_CLEANUP_NAMESPACE_ENTRIES", 4, raising=False)
    target = tmp_path / "sidecar-runtime"
    transaction_directory = tmp_path / ".sidecar-runtime-transactions"
    transaction_directory.mkdir(mode=0o700)
    for _ in range(5):
        (transaction_directory / f"{uuid.uuid4().hex}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="install transaction namespace entry limit"):
        helper.recover_install_transactions(target)


def test_install_transaction_namespace_stops_consuming_at_limit_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(helper, "MAX_CLEANUP_NAMESPACE_ENTRIES", 4, raising=False)
    target = tmp_path / "sidecar-runtime"
    transaction_directory = tmp_path / ".sidecar-runtime-transactions"
    transaction_directory.mkdir(mode=0o700)
    for _ in range(12):
        (transaction_directory / f"{uuid.uuid4().hex}.json").write_text(
            "{}",
            encoding="utf-8",
        )
    original_scandir = helper.os.scandir
    consumed = 0

    class CountingScandir:
        def __init__(self, directory: object) -> None:
            self._context = original_scandir(directory)
            self._iterator: object | None = None

        def __enter__(self) -> CountingScandir:
            self._iterator = self._context.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._context.__exit__(*args)

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> object:
            nonlocal consumed
            assert self._iterator is not None
            entry = next(self._iterator)
            consumed += 1
            return entry

    monkeypatch.setattr(helper.os, "scandir", CountingScandir)

    with pytest.raises(ValueError, match="install transaction namespace entry limit"):
        helper.recover_install_transactions(target)

    assert consumed == 5


@pytest.mark.parametrize(
    (
        "flock_failures",
        "close_failures",
        "primary_action",
        "noted_actions",
        "descriptors_closed",
    ),
    [
        pytest.param(
            frozenset({"lock unlock"}),
            frozenset(),
            "lock unlock",
            (),
            True,
            id="lock-unlock",
        ),
        pytest.param(
            frozenset({"parent unlock"}),
            frozenset(),
            "parent unlock",
            (),
            True,
            id="parent-unlock",
        ),
        pytest.param(
            frozenset({"lock unlock", "parent unlock"}),
            frozenset(),
            "lock unlock",
            ("parent unlock",),
            True,
            id="both-unlocks",
        ),
        pytest.param(
            frozenset(),
            frozenset({"lock close"}),
            "lock close",
            (),
            False,
            id="lock-close",
        ),
        pytest.param(
            frozenset(),
            frozenset({"parent close"}),
            "parent close",
            (),
            False,
            id="parent-close",
        ),
    ],
)
def test_build_lock_cleanup_attempts_every_step_after_system_call_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flock_failures: frozenset[str],
    close_failures: frozenset[str],
    primary_action: str,
    noted_actions: tuple[str, ...],
    descriptors_closed: bool,
) -> None:
    outcome = _exercise_build_lock_cleanup_faults(
        tmp_path,
        monkeypatch,
        flock_failures=flock_failures,
        close_failures=close_failures,
    )

    assert outcome.events == _LOCK_CLEANUP_ACTIONS
    assert outcome.close_attempts == {"lock close": 1, "parent close": 1}
    assert outcome.error is outcome.injected_errors[primary_action]
    notes = tuple(getattr(outcome.error, "__notes__", ()))
    for action in noted_actions:
        assert any(action in note for note in notes)
    assert outcome.registry_empty
    assert outcome.guard_rejected
    assert outcome.descriptors_closed is descriptors_closed
    assert outcome.child_reacquired


def test_build_lock_cleanup_preserves_business_error_and_notes_all_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_error = RuntimeError("business operation failed")

    outcome = _exercise_build_lock_cleanup_faults(
        tmp_path,
        monkeypatch,
        flock_failures=frozenset({"lock unlock", "parent unlock"}),
        close_failures=frozenset({"lock close", "parent close"}),
        business_error=business_error,
    )

    assert outcome.events == _LOCK_CLEANUP_ACTIONS
    assert outcome.close_attempts == {"lock close": 1, "parent close": 1}
    assert outcome.error is business_error
    notes = tuple(getattr(outcome.error, "__notes__", ()))
    for action in _LOCK_CLEANUP_ACTIONS:
        assert any(action in note for note in notes)
    assert outcome.registry_empty
    assert outcome.guard_rejected
    assert not outcome.descriptors_closed
    assert not outcome.child_reacquired


def test_build_lock_close_error_preserves_different_file_reusing_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_close_error_after_descriptor_reuse(
        tmp_path,
        monkeypatch,
        reopen_same_inode=False,
    )

    expected = (tmp_path / "other.lock").stat()
    assert outcome.close_calls == 1
    assert outcome.replacement_survived
    assert outcome.replacement_identity == (expected.st_dev, expected.st_ino)


def test_build_lock_close_error_preserves_same_inode_reusing_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_close_error_after_descriptor_reuse(
        tmp_path,
        monkeypatch,
        reopen_same_inode=True,
    )

    expected = (tmp_path / "original.lock").stat()
    assert outcome.close_calls == 1
    assert outcome.replacement_survived
    assert outcome.replacement_identity == (expected.st_dev, expected.st_ino)


def test_build_lock_close_ebadf_is_reported_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    lock = tmp_path / "descriptor.lock"
    lock.write_bytes(b"")
    descriptor = os.open(lock, os.O_RDWR)
    original_close = helper.os.close
    injected_error = OSError(errno.EBADF, "close result is indeterminate")
    close_calls = 0

    def close_then_report_ebadf(candidate: int) -> None:
        nonlocal close_calls
        close_calls += 1
        original_close(candidate)
        raise injected_error

    cleanup_errors: list[tuple[str, Exception]] = []
    with monkeypatch.context() as patcher:
        patcher.setattr(helper.os, "close", close_then_report_ebadf)
        helper._close_owned_lock_descriptor(
            descriptor,
            "lock close",
            cleanup_errors,
        )

    assert close_calls == 1
    assert cleanup_errors == [("lock close", injected_error)]
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_build_lock_resists_lock_path_replacement(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    lock = tmp_path / "runtime.lock"
    first_entered = context.Event()
    first_release = context.Event()
    second_entered = context.Event()
    second_release = context.Event()

    first = context.Process(
        target=_lock_worker,
        args=(str(RUNTIME_HELPER), str(lock), first_entered, first_release),
    )
    first.start()
    assert first_entered.wait(5)
    displaced = tmp_path / "runtime.lock.displaced"
    lock.rename(displaced)
    lock.write_bytes(b"replacement")
    lock.chmod(0o600)

    second = context.Process(
        target=_lock_worker,
        args=(str(RUNTIME_HELPER), str(lock), second_entered, second_release),
    )
    second.start()
    assert not second_entered.wait(0.25)

    first_release.set()
    first.join(5)
    assert first.exitcode == 0
    assert second_entered.wait(5)
    second_release.set()
    second.join(5)
    assert second.exitcode == 0


def test_build_child_retains_lock_after_wrapper_is_killed(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    ready = tmp_path / "child-ready"
    finished = tmp_path / "child-finished"
    gate = tmp_path / "child-gate"
    acquired = tmp_path / "second-acquired"
    os.mkfifo(gate, 0o600)
    os.mkfifo(acquired, 0o600)
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, sys\n"
        "ready, gate, finished = map(pathlib.Path, sys.argv[1:])\n"
        "ready.write_text('ready')\n"
        "with gate.open() as stream: stream.read(1)\n"
        "finished.write_text('finished')\n",
        encoding="utf-8",
    )
    second_command = tmp_path / "second.py"
    second_command.write_text(
        "import pathlib, sys\n"
        "with pathlib.Path(sys.argv[1]).open('w') as stream: stream.write('acquired')\n",
        encoding="utf-8",
    )
    first = subprocess.Popen(
        [
            sys.executable,
            str(RUNTIME_HELPER),
            "run-with-lock",
            str(lock),
            "--",
            sys.executable,
            str(child),
            str(ready),
            str(gate),
            str(finished),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        if first.poll() is not None:
            break
        time.sleep(0.01)
    assert ready.exists(), first.stderr.read() if first.stderr else ""
    first.kill()
    first.wait(5)

    reader = os.open(acquired, os.O_RDONLY | os.O_NONBLOCK)
    second = subprocess.Popen(
        [
            sys.executable,
            str(RUNTIME_HELPER),
            "run-with-lock",
            str(lock),
            "--",
            sys.executable,
            str(second_command),
            str(acquired),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        readable, _, _ = select.select([reader], [], [], 0.25)
        assert readable == []
        with gate.open("w", encoding="utf-8") as release:
            release.write("x")
        readable, _, _ = select.select([reader], [], [], 5)
        assert readable == [reader]
        assert os.read(reader, 32) == b"acquired"
    finally:
        os.close(reader)
    assert second.wait(5) == 0, second.stderr.read() if second.stderr else ""
    deadline = time.monotonic() + 5
    while not finished.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finished.exists()


def test_build_input_digest_changes_with_source_and_helper(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "repo"
    package = repo / "src/parsing_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='parsing-core'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    prepare = repo / "prepare.sh"
    runtime_helper = repo / "runtime.py"
    prepare.write_text("prepare-v1\n", encoding="utf-8")
    runtime_helper.write_text("helper-v1\n", encoding="utf-8")

    initial = helper.build_input_digest(repo, prepare, runtime_helper)
    (package / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")
    source_changed = helper.build_input_digest(repo, prepare, runtime_helper)
    runtime_helper.write_text("helper-v2\n", encoding="utf-8")
    helper_changed = helper.build_input_digest(repo, prepare, runtime_helper)

    assert len({initial, source_changed, helper_changed}) == 3


def test_uv_tool_is_sealed_by_inode_and_rejects_writable_input(tmp_path: Path) -> None:
    helper = _load_helper()
    source = tmp_path / "uv"
    source.write_text("#!/bin/bash\nprintf 'uv 0.12.3 fixture\\n'\n", encoding="utf-8")
    source.chmod(0o755)
    sealed = tmp_path / "private/uv"

    tool = helper.seal_uv_tool(source, sealed, "0.12.3")

    assert len(tool.sha256) == 64
    assert sealed.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o500
    os.close(tool.descriptor)
    source.chmod(0o775)
    with pytest.raises(ValueError, match="group/world writable"):
        helper.seal_uv_tool(source, tmp_path / "other/uv", "0.12.3")


def test_uv_tool_rejects_hardlinked_input(tmp_path: Path) -> None:
    helper = _load_helper()
    source = tmp_path / "uv"
    source.write_text("#!/bin/bash\nprintf 'uv 0.12.3 fixture\\n'\n", encoding="utf-8")
    source.chmod(0o755)
    os.link(source, tmp_path / "uv-alias")

    with pytest.raises(ValueError, match="singly-linked"):
        helper.seal_uv_tool(source, tmp_path / "private/uv", "0.12.3")


def test_uv_sealing_detects_complete_source_observation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    source = tmp_path / "uv"
    source.write_text("#!/bin/bash\nprintf 'uv 0.12.3 fixture\\n'\n", encoding="utf-8")
    source.chmod(0o755)
    source_identity = source.stat()
    original_read: Callable[[int, int], bytes] = helper.os.read
    mutated = False

    def mutate_at_eof(descriptor: int, length: int) -> bytes:
        nonlocal mutated
        block = original_read(descriptor, length)
        metadata = os.fstat(descriptor)
        if not mutated and not block and metadata.st_ino == source_identity.st_ino:
            mutated = True
            writer = os.open(source, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                first = os.pread(descriptor, 1, 0)
                os.pwrite(writer, first, 0)
                os.fsync(writer)
            finally:
                os.close(writer)
        return block

    monkeypatch.setattr(helper.os, "read", mutate_at_eof)

    with pytest.raises(ValueError, match="uv source changed while sealing"):
        helper.seal_uv_tool(source, tmp_path / "private/uv", "0.12.3")

    assert mutated


@pytest.mark.parametrize("mutation", ["replace", "in-place"])
def test_uv_sealing_detects_mutation_after_version_before_export(
    tmp_path: Path,
    mutation: str,
) -> None:
    helper = _load_helper()
    source = tmp_path / "uv"
    if mutation == "replace":
        action = (
            'replacement="${0}.replacement"\n'
            "printf '#!/bin/bash\\nexit 73\\n' > \"$replacement\"\n"
            'chmod 500 "$replacement"\n'
            'mv "$replacement" "$0"\n'
        )
    else:
        action = 'chmod 700 "$0"\nprintf \'# changed\\n\' >> "$0"\n'
    source.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "if [[ ${1:-} == --version ]]; then\n"
        f"{action}"
        "  printf 'uv 0.12.3 fixture\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    source.chmod(0o755)

    with pytest.raises(ValueError, match="sealed uv changed after version check"):
        helper.seal_uv_tool(source, tmp_path / "private/uv", "0.12.3")


def test_build_environment_discards_index_and_python_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    poisoned = {
        "PATH": "/attacker",
        "PYTHONPATH": "/attacker/python",
        "PIP_INDEX_URL": "https://attacker.invalid/simple",
        "UV_INDEX_URL": "https://attacker.invalid/simple",
        "UV_EXTRA_INDEX_URL": "https://attacker.invalid/extra",
        "VIRTUAL_ENV": "/attacker/venv",
    }
    for name, value in poisoned.items():
        monkeypatch.setenv(name, value)

    environment = helper.sanitized_build_environment(tmp_path / "home")

    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"
    assert not (poisoned.keys() & environment.keys()) - {"PATH"}


def test_prepare_ignores_legacy_bypass_and_path_injection(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "parsing-core-app/scripts"
    scripts.mkdir(parents=True)
    (repo / "src/parsing_core").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='parsing-core'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    prepare = scripts / PREPARER.name
    shutil.copy2(PREPARER, prepare)
    shutil.copy2(RUNTIME_HELPER, scripts / RUNTIME_HELPER.name)
    log = tmp_path / "path-tools.log"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in ("bash", "python3", "shasum", "file", "curl", "tar", "uv", "uname"):
        tool = fake_bin / name
        tool.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/bash
                printf '%s\\n' {name!r} >> {str(log)!r}
                exit 73
                """
            ),
            encoding="utf-8",
        )
        tool.chmod(0o755)

    environment = os.environ.copy()
    environment.pop("PDF2MD_UV_BIN", None)
    environment.pop("PDF2MD_WHEELHOUSE_ROOT", None)
    environment.update(
        {
            "BASH_ENV": str(tmp_path / "bash-env"),
            "PATH": str(fake_bin),
            "PDF2MD_MACHINE": "arm64",
            "PDF2MD_RUNTIME_LOCKED": "forged",
            "PDF2MD_TEST_PYTHON_SHA256": "0" * 64,
        }
    )
    (tmp_path / "bash-env").write_text(f"printf 'BASH_ENV\\n' >> {str(log)!r}\n", encoding="utf-8")
    result = subprocess.run(
        [str(prepare)],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "PDF2MD_UV_BIN" in result.stderr
    assert not log.exists()


def test_release_workflow_passes_prefetched_canonical_paths_to_preparer() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'uv_path="$(/usr/bin/python3 -I -S -B -c' in workflow
    assert "sidecar_runtime.py prefetch-wheelhouse" in workflow
    assert "printf 'PDF2MD_UV_BIN=%s\\n'" in workflow
    assert "printf 'PDF2MD_WHEELHOUSE_ROOT=%s\\n'" in workflow
    assert 'PDF2MD_UV_BIN="$PDF2MD_UV_BIN"' in workflow
    assert 'PDF2MD_WHEELHOUSE_ROOT="$PDF2MD_WHEELHOUSE_ROOT"' in workflow
    assert "prepare-sidecar-python.sh" in workflow
