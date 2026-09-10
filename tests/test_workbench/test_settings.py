import asyncio
import errno
import json
import multiprocessing
import os
import stat
import subprocess
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import parsing_core.workbench.settings as settings_module
from parsing_core.serving.api import routes_topics, routes_workbench
from parsing_core.serving.models.api import (
    BaiduSettingsRequest,
    CodexSettingsRequest,
    DeepSeekSettingsRequest,
    WorkbenchSettingsResponse,
)
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.workbench.keychain import KeychainError, mask_secret, read_secret, save_secret
from parsing_core.workbench.settings import (
    MAX_CODEX_CLI_PATH_BYTES,
    MAX_SETTINGS_FILE_BYTES,
    SettingsCommitError,
    SettingsError,
    SettingsStore,
    WorkbenchSettings,
    load_settings,
    save_settings,
    update_settings_fields,
)

INVALID_SETTINGS = "invalid workbench settings"
SAVE_FAILED = "unable to save workbench settings"
SAVE_DURABILITY_UNCERTAIN = "workbench settings saved but durability is uncertain"
SETTINGS_FILENAME = "workbench-settings.json"
SETTINGS_LOCK_FILENAME = ".workbench-settings.lock"


def _concurrent_settings_update_worker(
    trusted_root: str,
    operation: str,
    iterations: int,
    start: object,
    results: object,
) -> None:
    try:
        start.wait(5)  # type: ignore[attr-defined]
        for index in range(iterations):
            if operation == "model":
                update_settings_fields(
                    trusted_root,
                    deepseek_model="deepseek-v4-pro",
                )
            else:
                update_settings_fields(
                    trusted_root,
                    codex_cli_path=f"/usr/local/bin/codex-{index}",
                )
        results.put(None)  # type: ignore[attr-defined]
    except BaseException:
        results.put(traceback.format_exc())  # type: ignore[attr-defined]


def _persistent_eintr_worker(
    trusted_root: str,
    operation: str,
    results: object,
) -> None:
    def interrupt_forever(*_args: object, **_kwargs: object) -> object:
        raise InterruptedError

    try:
        setattr(settings_module, f"_{operation}", interrupt_forever)
        if operation == "read" or operation == "open":
            load_settings(trusted_root)
        elif operation == "flock":
            update_settings_fields(trusted_root, deepseek_model="deepseek-v4-pro")
        else:
            save_settings(trusted_root, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))
    except SettingsError as exc:
        results.put(str(exc))  # type: ignore[attr-defined]
    except BaseException:
        results.put(traceback.format_exc())  # type: ignore[attr-defined]
    else:
        results.put("unexpected success")  # type: ignore[attr-defined]


def _fifo_open_race_worker(trusted_root: str, results: object) -> None:
    path = Path(trusted_root) / SETTINGS_FILENAME
    path.write_text(
        '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/old"}',
        encoding="utf-8",
    )
    path.chmod(0o600)
    real_open = settings_module._open
    swapped = False

    def swap_regular_file_for_fifo(
        target: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if os.fspath(target) == SETTINGS_FILENAME and dir_fd is not None and not swapped:
            path.unlink()
            os.mkfifo(path, 0o600)
            swapped = True
        return real_open(target, flags, mode, dir_fd=dir_fd)

    try:
        settings_module._open = swap_regular_file_for_fifo
        load_settings(trusted_root)
    except SettingsError as exc:
        results.put(str(exc))  # type: ignore[attr-defined]
    except BaseException:
        results.put(traceback.format_exc())  # type: ignore[attr-defined]
    else:
        results.put("unexpected success")  # type: ignore[attr-defined]


def _lock_split_update_worker(
    trusted_root: str,
    role: str,
    split_ready: object,
    second_started: object,
    second_done: object,
    release_first: object,
    results: object,
) -> None:
    try:
        if role == "first":
            real_flock = settings_module._flock
            split = False

            def split_lock_after_acquire(fd: int, operation: int) -> None:
                nonlocal split
                real_flock(fd, operation)
                if operation & settings_module.fcntl.LOCK_EX and not split:
                    split = True
                    root = Path(trusted_root)
                    lock_path = root / SETTINGS_LOCK_FILENAME
                    displaced = root / ".displaced-settings.lock"
                    os.replace(lock_path, displaced)
                    replacement_fd = os.open(
                        lock_path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    try:
                        os.fchmod(replacement_fd, 0o600)
                    finally:
                        os.close(replacement_fd)
                    split_ready.set()  # type: ignore[attr-defined]
                    if not release_first.wait(5):  # type: ignore[attr-defined]
                        raise TimeoutError("first lock-domain update was not released")

            settings_module._flock = split_lock_after_acquire
            update_settings_fields(trusted_root, codex_cli_path="/usr/bin/first")
        else:
            if not split_ready.wait(5):  # type: ignore[attr-defined]
                raise TimeoutError("lock split was not created")
            second_started.set()  # type: ignore[attr-defined]
            try:
                update_settings_fields(trusted_root, codex_cli_path="/usr/bin/second")
            finally:
                second_done.set()  # type: ignore[attr-defined]
    except SettingsError as exc:
        results.put((role, str(exc)))  # type: ignore[attr-defined]
    except BaseException:
        results.put((role, traceback.format_exc()))  # type: ignore[attr-defined]
    else:
        results.put((role, None))  # type: ignore[attr-defined]


def _temp_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_settings_routes_pass_scheduler_fs_layout_as_bound_root(
    tmp_path: Path,
) -> None:
    layout = FsLayout(str(tmp_path / "app-data"))
    scheduler = SimpleNamespace(_query_orch=SimpleNamespace(fs=layout))

    assert routes_workbench._settings_root(cast(object, scheduler)) is layout
    assert routes_topics._settings_root(cast(object, scheduler)) is layout


def test_settings_store_accepts_fs_layout_bound_root(tmp_path: Path) -> None:
    layout = FsLayout(str(tmp_path / "anchor" / "managed" / "app-data"))
    expected = WorkbenchSettings(codex_cli_path="/usr/bin/codex")

    save_settings(layout, expected)

    assert load_settings(layout) == expected
    assert update_settings_fields(layout, deepseek_model="deepseek-v4-pro") == expected


@pytest.mark.parametrize("operation", ["load", "save", "update"])
def test_settings_store_bound_root_rejects_runtime_ancestor_replacement(
    tmp_path: Path,
    operation: str,
) -> None:
    anchor = tmp_path / "anchor"
    layout = FsLayout(str(anchor / "managed" / "app-data"))
    original = WorkbenchSettings(codex_cli_path="/usr/bin/original")
    save_settings(layout, original)

    displaced = tmp_path / "anchor-original"
    anchor.rename(displaced)
    replacement_base = anchor / "managed" / "app-data"
    replacement_base.mkdir(parents=True, mode=0o700)
    anchor.chmod(0o700)
    (anchor / "managed").chmod(0o700)
    replacement_base.chmod(0o700)
    attacker_path = replacement_base / SETTINGS_FILENAME
    attacker_path.write_text(
        '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/attacker"}',
        encoding="utf-8",
    )
    attacker_path.chmod(0o600)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        if operation == "load":
            load_settings(layout)
        elif operation == "save":
            save_settings(layout, WorkbenchSettings(codex_cli_path="/usr/bin/new"))
        else:
            update_settings_fields(layout, codex_cli_path="/usr/bin/new")

    assert json.loads(attacker_path.read_text(encoding="utf-8"))["codex_cli_path"] == (
        "/usr/bin/attacker"
    )


def test_deepseek_settings_route_preserves_unmodified_fields_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = FsLayout(str(tmp_path / "app-data"))
    scheduler = SimpleNamespace(_query_orch=SimpleNamespace(fs=layout))
    save_settings(
        layout.base_dir,
        WorkbenchSettings(codex_cli_path="/usr/local/bin/codex-existing"),
    )
    monkeypatch.setattr(routes_workbench, "_read_masked_deepseek_key", lambda: None)

    response = asyncio.run(
        routes_workbench.save_deepseek_settings(
            DeepSeekSettingsRequest(model="deepseek-v4-pro", api_key=None),
            cast(object, scheduler),
        )
    )

    assert response.deepseek_model == "deepseek-v4-pro"
    assert load_settings(layout.base_dir).codex_cli_path == ("/usr/local/bin/codex-existing")


def test_settings_store_uses_a_fixed_file_below_explicit_trusted_root(
    tmp_path: Path,
) -> None:
    settings = WorkbenchSettings(codex_cli_path="/usr/bin/codex")

    save_settings(tmp_path, settings)

    path = tmp_path / SETTINGS_FILENAME
    assert path.is_file()
    assert load_settings(tmp_path) == settings


@pytest.mark.parametrize(
    "trusted_root",
    [Path("relative-root"), Path("/tmp/pdf2md-safe/../escaped-root")],
)
def test_settings_store_rejects_noncanonical_trusted_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_root: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(trusted_root)


def test_settings_update_is_atomic_across_repeated_concurrent_processes(
    tmp_path: Path,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_settings_update_worker,
            args=(str(tmp_path), operation, 40, start, results),
        )
        for operation in ("model", "path")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert not process.is_alive(), "concurrent settings update exceeded hard timeout"
        assert process.exitcode == 0

    assert [results.get(timeout=1) for _process in processes] == [None, None]
    assert load_settings(tmp_path) == WorkbenchSettings(
        deepseek_model="deepseek-v4-pro",
        codex_cli_path="/usr/local/bin/codex-39",
    )
    assert json.loads((tmp_path / SETTINGS_FILENAME).read_text(encoding="utf-8")) == {
        "codex_cli_path": "/usr/local/bin/codex-39",
        "deepseek_model": "deepseek-v4-pro",
    }
    assert stat.S_IMODE((tmp_path / SETTINGS_LOCK_FILENAME).stat().st_mode) == 0o600


def test_settings_update_api_rejects_external_callback_before_locking(
    tmp_path: Path,
) -> None:
    callback_ran = False

    def external_callback(_current: WorkbenchSettings) -> WorkbenchSettings:
        nonlocal callback_ran
        callback_ran = True
        return WorkbenchSettings()

    assert not hasattr(SettingsStore, "update")
    with pytest.raises(TypeError):
        update_settings_fields(tmp_path, external_callback)  # type: ignore[call-arg]

    assert not callback_ran
    assert not (tmp_path / SETTINGS_LOCK_FILENAME).exists()

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        update_settings_fields(tmp_path, codex_cli_path=external_callback)

    assert not callback_ran
    assert not (tmp_path / SETTINGS_LOCK_FILENAME).exists()


def test_settings_update_normalizes_string_subclasses_before_locking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_held = False
    real_flock = settings_module._flock

    class ObservedStr(str):
        def _observe(self, operation: str) -> None:
            if lock_held:
                raise AssertionError(f"external string {operation} ran while locked")

        def __eq__(self, other: object) -> bool:
            self._observe("comparison")
            return super().__eq__(other)

        def __ne__(self, other: object) -> bool:
            self._observe("comparison")
            return super().__ne__(other)

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            self._observe("encode")
            return super().encode(encoding, errors)

        def startswith(self, *args: object, **kwargs: object) -> bool:
            self._observe("startswith")
            return super().startswith(*args, **kwargs)  # type: ignore[arg-type]

        def strip(self, *args: object) -> str:
            self._observe("strip")
            return super().strip(*args)  # type: ignore[arg-type]

    def track_lock(fd: int, operation: int) -> None:
        nonlocal lock_held
        real_flock(fd, operation)
        if operation & settings_module.fcntl.LOCK_EX:
            lock_held = True
        elif operation & settings_module.fcntl.LOCK_UN:
            lock_held = False

    monkeypatch.setattr(settings_module, "_flock", track_lock)

    updated = update_settings_fields(
        tmp_path,
        deepseek_model=ObservedStr("deepseek-v4-pro"),
        codex_cli_path=ObservedStr("/usr/bin/codex"),
    )

    assert type(updated.deepseek_model) is str
    assert type(updated.codex_cli_path) is str
    assert updated.codex_cli_path == "/usr/bin/codex"


def test_settings_lock_rename_recreate_cannot_split_update_domain(
    tmp_path: Path,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    context = multiprocessing.get_context("spawn")
    split_ready = context.Event()
    second_started = context.Event()
    second_done = context.Event()
    release_first = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_lock_split_update_worker,
            args=(
                str(tmp_path),
                role,
                split_ready,
                second_started,
                second_done,
                release_first,
                results,
            ),
        )
        for role in ("first", "second")
    ]
    for process in processes:
        process.start()
    assert split_ready.wait(5), "first process did not create the lock split"
    assert second_started.wait(5), "second process did not enter the update"
    second_completed_while_first_held_lock = second_done.wait(0.5)
    release_first.set()
    for process in processes:
        process.join(8)
        assert not process.is_alive(), "split lock-domain update exceeded hard timeout"
        assert process.exitcode == 0

    outcomes = dict(results.get(timeout=1) for _process in processes)
    assert not second_completed_while_first_held_lock
    assert outcomes == {"first": INVALID_SETTINGS, "second": None}
    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/second"


def test_settings_update_reports_uncertain_if_lock_replaced_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    lock_path = tmp_path / SETTINGS_LOCK_FILENAME
    displaced = tmp_path / ".displaced-settings.lock"
    real_replace = settings_module._replace
    split = False

    def replace_then_split_lock(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal split
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if os.fspath(destination) == SETTINGS_FILENAME and not split:
            split = True
            os.replace(lock_path, displaced)
            replacement_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.fchmod(replacement_fd, 0o600)
            finally:
                os.close(replacement_fd)

    monkeypatch.setattr(settings_module, "_replace", replace_then_split_lock)

    with pytest.raises(SettingsCommitError) as error:
        update_settings_fields(tmp_path, codex_cli_path="/usr/bin/committed")

    assert error.value.identity_uncertain
    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/committed"


@pytest.mark.parametrize("operation", ["open", "pread", "fsync", "flock"])
def test_settings_retries_a_single_interrupted_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    original = getattr(settings_module, f"_{operation}")
    interrupted = False

    def interrupt_once(*args: object, **kwargs: object) -> object:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return original(*args, **kwargs)

    monkeypatch.setattr(settings_module, f"_{operation}", interrupt_once)

    if operation == "flock":
        updated = update_settings_fields(
            tmp_path,
            codex_cli_path="/usr/bin/codex",
        )
    else:
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))
        updated = load_settings(tmp_path)

    assert interrupted
    assert updated.codex_cli_path == "/usr/bin/codex"


@pytest.mark.parametrize(
    "replace_error",
    [InterruptedError(errno.EINTR, "replace interrupted"), OSError(errno.EIO, "replace failed")],
)
def test_settings_replace_that_commits_then_raises_reports_commit_error_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_error: OSError,
) -> None:
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    intended = WorkbenchSettings(codex_cli_path="/usr/bin/intended-codex")
    save_settings(tmp_path, old)
    real_replace = settings_module._replace
    replace_calls = 0

    def replace_then_raise(*args: object, **kwargs: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        real_replace(*args, **kwargs)
        raise replace_error

    monkeypatch.setattr(settings_module, "_replace", replace_then_raise)

    with pytest.raises(SettingsCommitError) as captured:
        save_settings(tmp_path, intended)

    assert captured.value.committed
    assert replace_calls == 1
    assert load_settings(tmp_path) == intended


@pytest.mark.parametrize("control_signal", [KeyboardInterrupt, SystemExit])
def test_settings_replace_that_commits_then_raises_control_signal_is_classified_chainlessly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_signal: type[BaseException],
) -> None:
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    intended = WorkbenchSettings(codex_cli_path="/usr/bin/intended-codex")
    save_settings(tmp_path, old)
    real_replace = settings_module._replace

    def replace_then_raise(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)
        raise control_signal()

    monkeypatch.setattr(settings_module, "_replace", replace_then_raise)

    with pytest.raises(SettingsCommitError) as captured:
        save_settings(tmp_path, intended)

    assert captured.value.committed
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert load_settings(tmp_path) == intended


@pytest.mark.parametrize("control_signal", [KeyboardInterrupt, SystemExit])
def test_settings_control_signal_before_replace_is_preserved_and_temp_is_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_signal: type[BaseException],
) -> None:
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    intended = WorkbenchSettings(codex_cli_path="/usr/bin/intended-codex")
    save_settings(tmp_path, old)

    def raise_before_replace(*_args: object, **_kwargs: object) -> None:
        raise control_signal()

    monkeypatch.setattr(settings_module, "_replace", raise_before_replace)

    with pytest.raises(control_signal):
        save_settings(tmp_path, intended)

    assert load_settings(tmp_path) == old
    assert _temp_files(tmp_path / SETTINGS_FILENAME) == []


@pytest.mark.parametrize(
    "operation",
    ["open", "read", "pread", "write", "fsync", "replace", "flock"],
)
def test_settings_persistent_eintr_fails_within_hard_process_timeout(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_persistent_eintr_worker,
        args=(str(tmp_path), operation, results),
    )
    process.start()
    process.join(3)
    if process.is_alive():
        process.terminate()
        process.join(3)
        pytest.fail(f"persistent EINTR in {operation} exceeded hard timeout")

    assert process.exitcode == 0
    result = results.get(timeout=1)
    assert result in {SAVE_FAILED, "unable to load workbench settings"}


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / SETTINGS_FILENAME
    settings = WorkbenchSettings(
        deepseek_model="deepseek-v4-pro",
        codex_cli_path="/opt/homebrew/bin/codex",
    )
    save_settings(tmp_path, settings)

    assert load_settings(tmp_path) == settings
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "codex_cli_path": "/opt/homebrew/bin/codex",
        "deepseek_model": "deepseek-v4-pro",
    }


def test_settings_default_when_missing(tmp_path: Path) -> None:
    assert load_settings(tmp_path) == WorkbenchSettings(
        deepseek_model="deepseek-v4-pro",
        codex_cli_path=None,
    )


def test_settings_load_retries_when_missing_entry_appears_during_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    real_stat = settings_module.os.stat
    injected = False

    def report_missing_while_creating_file(
        target: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal injected
        if os.fspath(target) == SETTINGS_FILENAME and dir_fd is not None and not injected:
            path.write_text(
                '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/appeared"}',
                encoding="utf-8",
            )
            path.chmod(0o600)
            injected = True
            raise FileNotFoundError(errno.ENOENT, "simulated missing entry")
        return real_stat(target, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(settings_module.os, "stat", report_missing_while_creating_file)

    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/appeared"


def test_settings_load_does_not_return_default_when_final_missing_check_creates_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    real_stat = settings_module.os.stat
    target_stat_calls = 0

    def create_file_during_final_missing_check(
        target: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal target_stat_calls
        if os.fspath(target) == SETTINGS_FILENAME and dir_fd is not None:
            target_stat_calls += 1
            if target_stat_calls == settings_module._PATH_LOOKUP_ATTEMPTS + 1:
                path.write_text(
                    '{"deepseek_model":"deepseek-v4-pro",'
                    '"codex_cli_path":"/usr/bin/appeared-last"}',
                    encoding="utf-8",
                )
                path.chmod(0o600)
                raise FileNotFoundError(errno.ENOENT, "simulated stale missing result")
        return real_stat(target, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(settings_module.os, "stat", create_file_during_final_missing_check)

    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/appeared-last"


def test_settings_load_rejects_path_replaced_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    replacement = tmp_path / "replacement.json"
    displaced = tmp_path / "displaced.json"
    original = '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/old"}'
    path.write_text(original, encoding="utf-8")
    replacement.write_text(
        '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/new"}',
        encoding="utf-8",
    )
    path.chmod(0o600)
    replacement.chmod(0o600)
    real_fstat = settings_module.os.fstat
    swapped = False

    def replace_name_after_final_fd_check(fd: int) -> os.stat_result:
        nonlocal swapped
        file_stat = real_fstat(fd)
        if (
            not swapped
            and stat.S_ISREG(file_stat.st_mode)
            and file_stat.st_size == len(original.encode("utf-8"))
            and os.lseek(fd, 0, os.SEEK_CUR) == file_stat.st_size
        ):
            os.replace(path, displaced)
            os.replace(replacement, path)
            swapped = True
        return file_stat

    monkeypatch.setattr(settings_module.os, "fstat", replace_name_after_final_fd_check)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_fifo_open_race_fails_within_hard_process_timeout(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_fifo_open_race_worker,
        args=(str(tmp_path), results),
    )
    process.start()
    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
        pytest.fail("settings FIFO open race exceeded hard timeout")

    assert process.exitcode == 0
    assert results.get(timeout=1) == INVALID_SETTINGS


def test_settings_loads_legacy_deepseek_only_file(tmp_path: Path) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)

    assert load_settings(tmp_path) == WorkbenchSettings()


@pytest.mark.parametrize(
    "content",
    [
        b"{not-json",
        b"[]",
        b'"not-an-object"',
        b'{"deepseek_model":"deepseek-chat"}',
        b'{"deepseek_model":"deepseek-v4-pro","api_key":"secret"}',
        b'{"deepseek_model":"deepseek-v4-pro","deepseek_model":"deepseek-v4-pro"}',
        b"\xff",
    ],
)
def test_settings_rejects_invalid_content_with_stable_error(
    tmp_path: Path,
    content: bytes,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_rejects_oversize_file(tmp_path: Path) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_bytes(b" " * (MAX_SETTINGS_FILE_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_does_not_follow_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path = tmp_path / SETTINGS_FILENAME
    path.symlink_to(target)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


@pytest.mark.parametrize("special_path", ["fifo", "directory"])
def test_settings_load_rejects_special_file_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    special_path: str,
) -> None:
    path = tmp_path / "workbench-settings.json"
    if special_path == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    real_open = settings_module._open

    def reject_final_open(
        target: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        target_text = os.fspath(target)
        if target_text in {os.fspath(path), path.name}:
            raise AssertionError("special settings file must be rejected before open")
        return real_open(target_text, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(settings_module, "_open", reject_final_open)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(linked_parent)


def test_settings_load_rejects_untrusted_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(parent)


def test_settings_save_does_not_follow_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    original = '{"owner":"keep-me"}'
    target.write_text(original, encoding="utf-8")
    path = tmp_path / "workbench-settings.json"
    path.symlink_to(target)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert target.read_text(encoding="utf-8") == original
    assert path.is_symlink()
    assert _temp_files(path) == []


def test_settings_save_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        save_settings(
            linked_parent,
            WorkbenchSettings(codex_cli_path="/usr/bin/codex"),
        )

    assert not (real_parent / "workbench-settings.json").exists()


def test_settings_save_rejects_untrusted_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        save_settings(
            parent,
            WorkbenchSettings(codex_cli_path="/usr/bin/codex"),
        )


def test_settings_save_fails_closed_when_root_is_replaced_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "settings"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "settings-original"
    path = parent / "workbench-settings.json"
    real_write = settings_module._write
    replaced = False

    def replace_parent_then_write(fd: int, data: memoryview) -> int:
        nonlocal replaced
        if not replaced:
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            replaced = True
        return real_write(fd, data)

    monkeypatch.setattr(settings_module, "_write", replace_parent_then_write)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        save_settings(parent, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert not path.exists()
    assert not (moved_parent / path.name).exists()
    assert _temp_files(moved_parent / path.name) == []


def test_settings_load_fails_closed_when_root_is_replaced_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "settings"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "settings-original"
    save_settings(parent, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))
    real_read = settings_module._read
    replaced = False

    def replace_root_after_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(fd, size)
        if chunk and not replaced:
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            replaced = True
        return chunk

    monkeypatch.setattr(settings_module, "_read", replace_root_after_read)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(parent)


def test_settings_save_reports_uncertain_when_root_is_replaced_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "settings"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "settings-original"
    real_replace = settings_module._replace

    def replace_file_then_root(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)
        parent.rename(moved_parent)
        parent.mkdir(mode=0o700)

    monkeypatch.setattr(settings_module, "_replace", replace_file_then_root)

    with pytest.raises(SettingsError, match=f"^{SAVE_DURABILITY_UNCERTAIN}$"):
        save_settings(parent, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert not (parent / SETTINGS_FILENAME).exists()
    assert load_settings(moved_parent).codex_cli_path == "/usr/bin/codex"


@pytest.mark.parametrize(
    "codex_cli_path",
    [
        "",
        "   ",
        "relative/codex",
        "/usr/bin/../bin/codex",
        "/usr//bin/codex",
        "/usr/bin/codex/",
        " /usr/bin/codex",
        "/usr/bin/codex\x00",
        "/usr/bin/co\tdex",
        "/usr/bin/co\u200bdex",
        123,
    ],
)
def test_settings_rejects_noncanonical_codex_path(codex_cli_path: object) -> None:
    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        WorkbenchSettings(codex_cli_path=cast(str, codex_cli_path))


def test_settings_bounds_codex_path_by_utf8_bytes() -> None:
    max_path = "/" + ("a" * (MAX_CODEX_CLI_PATH_BYTES - 1))
    assert WorkbenchSettings(codex_cli_path=max_path).codex_cli_path == max_path

    oversized_path = "/" + ("界" * ((MAX_CODEX_CLI_PATH_BYTES // 3) + 1))
    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        WorkbenchSettings(codex_cli_path=oversized_path)


def test_settings_model_is_fixed() -> None:
    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        WorkbenchSettings(deepseek_model="deepseek-chat")


def test_settings_save_handles_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    real_write = settings_module._write

    def short_write(fd: int, data: memoryview) -> int:
        return real_write(fd, data[:3])

    monkeypatch.setattr(settings_module, "_write", short_write)

    settings = WorkbenchSettings(codex_cli_path="/usr/bin/codex")
    save_settings(tmp_path, settings)

    assert load_settings(tmp_path) == settings
    assert _temp_files(path) == []


def test_settings_save_retries_interrupted_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = settings_module._write
    interrupted = False

    def interrupt_once(fd: int, data: memoryview) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_write(fd, data)

    monkeypatch.setattr(settings_module, "_write", interrupt_once)

    settings = WorkbenchSettings(codex_cli_path="/usr/bin/codex")
    save_settings(tmp_path, settings)

    assert load_settings(tmp_path) == settings


def test_settings_save_rejects_overlong_write_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    monkeypatch.setattr(settings_module, "_write", lambda _fd, data: len(data) + 1)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert not path.exists()
    assert _temp_files(path) == []


def test_settings_save_reports_uncertain_without_double_closing_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep-open", encoding="utf-8")
    real_close = settings_module._close
    victim_fd: int | None = None
    injected = False

    def close_then_raise(fd: int) -> None:
        nonlocal injected, victim_fd
        if not injected and stat.S_ISREG(os.fstat(fd).st_mode):
            injected = True
            real_close(fd)
            victim_fd = os.open(victim, os.O_RDONLY)
            assert victim_fd == fd
            raise OSError("close reported failure after closing")
        real_close(fd)

    monkeypatch.setattr(settings_module, "_close", close_then_raise)

    with pytest.raises(SettingsCommitError):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert victim_fd is not None
    try:
        assert os.read(victim_fd, 9) == b"keep-open"
        assert load_settings(tmp_path).codex_cli_path == "/usr/bin/codex"
    finally:
        os.close(victim_fd)


def test_settings_load_reports_file_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    real_close = settings_module._close
    injected = False

    def close_regular_then_raise(fd: int) -> None:
        nonlocal injected
        is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
        real_close(fd)
        if is_regular and not injected:
            injected = True
            raise OSError("settings file close failed")

    monkeypatch.setattr(settings_module, "_close", close_regular_then_raise)

    with pytest.raises(SettingsError, match="^unable to load workbench settings$"):
        load_settings(tmp_path)


def test_settings_load_reports_root_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    real_close = settings_module._close
    injected = False

    def close_directory_then_raise(fd: int) -> None:
        nonlocal injected
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not injected:
            injected = True
            raise OSError("settings root close failed")

    monkeypatch.setattr(settings_module, "_close", close_directory_then_raise)

    with pytest.raises(SettingsError, match="^unable to load workbench settings$"):
        load_settings(tmp_path)


def test_settings_save_reports_uncertain_on_root_close_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = settings_module._close
    injected = False

    def close_directory_then_raise(fd: int) -> None:
        nonlocal injected
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory and not injected:
            injected = True
            raise OSError("settings root close failed")

    monkeypatch.setattr(settings_module, "_close", close_directory_then_raise)

    with pytest.raises(SettingsError, match=f"^{SAVE_DURABILITY_UNCERTAIN}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/codex"


def test_settings_update_reports_uncertain_on_lock_close_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_settings(tmp_path, WorkbenchSettings())
    real_close = settings_module._close
    injected = False

    def close_lock_then_raise(fd: int) -> None:
        nonlocal injected
        file_stat = os.fstat(fd)
        is_lock = stat.S_ISREG(file_stat.st_mode) and file_stat.st_size == 0
        real_close(fd)
        if is_lock and not injected:
            injected = True
            raise OSError("settings lock close failed")

    monkeypatch.setattr(settings_module, "_close", close_lock_then_raise)

    with pytest.raises(SettingsError, match=f"^{SAVE_DURABILITY_UNCERTAIN}$"):
        update_settings_fields(
            tmp_path,
            codex_cli_path="/usr/bin/codex",
        )

    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/codex"


def test_settings_save_uses_mode_0600_and_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    real_fsync = settings_module._fsync
    synced_types: list[int] = []

    def tracking_fsync(fd: int) -> None:
        synced_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(settings_module, "_fsync", tracking_fsync)

    save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IFREG in synced_types
    assert stat.S_IFDIR in synced_types


def test_settings_save_cleans_temp_and_preserves_old_file_on_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    save_settings(tmp_path, old)
    monkeypatch.setattr(settings_module, "_write", lambda _fd, _data: 0)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/new-codex"))

    assert load_settings(tmp_path) == old
    assert _temp_files(path) == []


def test_settings_save_cleans_temp_and_preserves_old_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    save_settings(tmp_path, old)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(settings_module, "_replace", fail_replace)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/new-codex"))

    assert load_settings(tmp_path) == old
    assert _temp_files(path) == []


def test_settings_save_cleans_temp_and_preserves_old_file_when_file_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    save_settings(tmp_path, old)
    real_fsync = settings_module._fsync

    def fail_regular_file_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(settings_module, "_fsync", fail_regular_file_fsync)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/new-codex"))

    assert load_settings(tmp_path) == old
    assert _temp_files(path) == []


def test_settings_save_rejects_temp_name_swapped_after_file_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    save_settings(tmp_path, old)
    real_fsync = settings_module._fsync
    displaced = tmp_path / ".displaced-owned-temp"
    attacker_payload = b'{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/attacker"}'
    swapped_temp: Path | None = None

    def swap_temp_after_fsync(fd: int) -> None:
        nonlocal swapped_temp
        real_fsync(fd)
        file_stat = os.fstat(fd)
        if stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0 and swapped_temp is None:
            temp_files = _temp_files(path)
            assert len(temp_files) == 1
            swapped_temp = temp_files[0]
            os.replace(swapped_temp, displaced)
            swapped_temp.write_bytes(attacker_payload)
            swapped_temp.chmod(0o600)

    monkeypatch.setattr(settings_module, "_fsync", swap_temp_after_fsync)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$"):
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/new-codex"))

    assert load_settings(tmp_path) == old
    assert swapped_temp is not None and swapped_temp.read_bytes() == attacker_payload
    assert displaced.exists()


def test_settings_save_rejects_temp_swap_at_replace_syscall_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    intended = WorkbenchSettings(codex_cli_path="/usr/bin/intended-codex")
    save_settings(tmp_path, old)
    real_replace = settings_module._replace
    displaced = tmp_path / ".displaced-owned-temp"
    attacker_payload = b'{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/attacker"}'
    swapped_temp: Path | None = None

    def swap_temp_then_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped_temp
        if os.fspath(destination) == SETTINGS_FILENAME and swapped_temp is None:
            temp_files = _temp_files(path)
            assert len(temp_files) == 1
            swapped_temp = temp_files[0]
            os.replace(swapped_temp, displaced)
            swapped_temp.write_bytes(attacker_payload)
            swapped_temp.chmod(0o600)
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(settings_module, "_replace", swap_temp_then_replace)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$") as captured:
        save_settings(tmp_path, intended)

    assert not isinstance(captured.value, SettingsCommitError)
    assert load_settings(tmp_path) == old
    assert swapped_temp is not None and swapped_temp.read_bytes() == attacker_payload
    assert displaced.exists()


def test_settings_cleanup_does_not_unlink_object_swapped_at_unlink_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    displaced = tmp_path / ".displaced-owned-temp"
    victim_payload = b"do-not-delete"
    victim: Path | None = None
    real_unlink = settings_module._unlink

    def fail_write(_fd: int, _data: memoryview) -> int:
        raise OSError("primary write failure")

    def swap_temp_then_unlink(
        target: str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal victim
        assert dir_fd is not None
        temp_files = _temp_files(path)
        assert len(temp_files) == 1
        victim = temp_files[0]
        os.replace(victim, displaced)
        victim.write_bytes(victim_payload)
        victim.chmod(0o600)
        real_unlink(target, dir_fd=dir_fd)

    monkeypatch.setattr(settings_module, "_write", fail_write)
    monkeypatch.setattr(settings_module, "_unlink", swap_temp_then_unlink)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$") as captured:
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert captured.value.__cause__ is not None
    assert "primary write failure" in str(captured.value.__cause__)
    assert victim is not None and victim.read_bytes() == victim_payload
    assert displaced.exists()


def test_settings_save_reports_uncertain_when_target_swapped_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    intended = WorkbenchSettings(codex_cli_path="/usr/bin/intended")
    attacker = tmp_path / "attacker-settings.json"
    displaced = tmp_path / "displaced-committed-settings.json"
    save_settings(tmp_path, old)
    attacker.write_text(
        '{"deepseek_model":"deepseek-v4-pro","codex_cli_path":"/usr/bin/attacker"}',
        encoding="utf-8",
    )
    attacker.chmod(0o600)
    real_replace = settings_module._replace
    swapped = False

    def replace_then_swap_target(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if os.fspath(destination) == SETTINGS_FILENAME and not swapped:
            swapped = True
            os.replace(path, displaced)
            os.replace(attacker, path)

    monkeypatch.setattr(settings_module, "_replace", replace_then_swap_target)

    with pytest.raises(SettingsCommitError) as error:
        save_settings(tmp_path, intended)

    assert error.value.identity_uncertain
    assert load_settings(tmp_path).codex_cli_path == "/usr/bin/attacker"
    assert json.loads(displaced.read_text(encoding="utf-8")) == {
        "codex_cli_path": "/usr/bin/intended",
        "deepseek_model": "deepseek-v4-pro",
    }


def test_settings_save_reports_committed_but_uncertain_when_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    old = WorkbenchSettings(codex_cli_path="/usr/bin/old-codex")
    new = WorkbenchSettings(codex_cli_path="/usr/bin/new-codex")
    save_settings(tmp_path, old)
    real_fsync = settings_module._fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(settings_module, "_fsync", fail_directory_fsync)

    with pytest.raises(SettingsError, match=f"^{SAVE_DURABILITY_UNCERTAIN}$"):
        save_settings(tmp_path, new)

    assert load_settings(tmp_path) == new
    assert _temp_files(path) == []


def test_settings_save_cleanup_failure_does_not_hide_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(_fd: int, _data: memoryview) -> int:
        raise OSError("primary write failure")

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("secondary cleanup failure")

    monkeypatch.setattr(settings_module, "_write", fail_write)
    monkeypatch.setattr(settings_module, "_unlink", fail_cleanup)

    with pytest.raises(SettingsError, match=f"^{SAVE_FAILED}$") as captured:
        save_settings(tmp_path, WorkbenchSettings(codex_cli_path="/usr/bin/codex"))

    assert captured.value.__cause__ is not None
    assert "primary write failure" in str(captured.value.__cause__)


def test_settings_load_retries_interrupted_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    real_read = settings_module._read
    interrupted = False

    def interrupt_once(fd: int, size: int) -> bytes:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_read(fd, size)

    monkeypatch.setattr(settings_module, "_read", interrupt_once)

    assert load_settings(tmp_path) == WorkbenchSettings()


def test_settings_load_rejects_short_read_that_still_forms_valid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "workbench-settings.json"
    valid = b'{"deepseek_model":"deepseek-v4-pro"}'
    path.write_bytes(valid + b" trailing-bytes")
    path.chmod(0o600)
    real_read = settings_module._read
    calls = 0

    def stop_early(fd: int, _size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_read(fd, len(valid))
        return b""

    monkeypatch.setattr(settings_module, "_read", stop_early)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_rejects_file_changed_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "workbench-settings.json"
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    real_read = settings_module._read
    changed = False

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            with path.open("ab") as output:
                output.write(b" ")
            changed = True
        return chunk

    monkeypatch.setattr(settings_module, "_read", mutate_after_read)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_redacts_recursion_error_from_deep_valid_json(tmp_path: Path) -> None:
    depth = 10_000
    raw = ('{"deepseek_model":' + "[" * depth + '"deepseek-v4-pro"' + "]" * depth + "}").encode(
        "utf-8"
    )
    assert len(raw) < MAX_SETTINGS_FILE_BYTES
    path = tmp_path / SETTINGS_FILENAME
    path.write_bytes(raw)
    path.chmod(0o600)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$") as captured:
        load_settings(tmp_path)

    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


def test_settings_load_rejects_concurrent_growth_beyond_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "workbench-settings.json"
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    real_read = settings_module._read
    changed = False

    def grow_after_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            with path.open("ab") as output:
                output.write(b" " * (MAX_SETTINGS_FILE_BYTES + 1))
            changed = True
        return chunk

    monkeypatch.setattr(settings_module, "_read", grow_after_read)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_rejects_group_writable_file(tmp_path: Path) -> None:
    writable = tmp_path / SETTINGS_FILENAME
    writable.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    writable.chmod(0o660)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


@pytest.mark.parametrize("file_mode", [0o400, 0o640, 0o644])
def test_settings_load_requires_exact_file_mode_0600(
    tmp_path: Path,
    file_mode: int,
) -> None:
    path = tmp_path / SETTINGS_FILENAME
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(file_mode)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_rejects_hardlinked_file(tmp_path: Path) -> None:
    original = tmp_path / SETTINGS_FILENAME
    original.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    original.chmod(0o600)
    linked = tmp_path / "linked.json"
    os.link(original, linked)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_load_rejects_foreign_owned_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "workbench-settings.json"
    path.write_text('{"deepseek_model":"deepseek-v4-pro"}', encoding="utf-8")
    path.chmod(0o600)
    real_fstat = os.fstat

    def foreign_fstat(fd: int) -> os.stat_result:
        file_stat = real_fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return file_stat
        values = list(file_stat)
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign_fstat)

    with pytest.raises(SettingsError, match=f"^{INVALID_SETTINGS}$"):
        load_settings(tmp_path)


def test_settings_api_models_are_bounded_and_forbid_extra_fields() -> None:
    assert CodexSettingsRequest(path=" /opt/homebrew/bin/codex ").path == (
        "/opt/homebrew/bin/codex"
    )
    assert BaiduSettingsRequest().api_key is None
    assert BaiduSettingsRequest(api_key=" baidu-key ").api_key == "baidu-key"
    assert DeepSeekSettingsRequest(api_key=" deepseek-key ").api_key == "deepseek-key"

    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": "/usr/bin/codex", "extra": True})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": "key", "extra": True})
    with pytest.raises(ValidationError):
        DeepSeekSettingsRequest.model_validate({"api_key": "key", "extra": True})
    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": ""})
    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": "relative/codex"})
    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": "/usr/bin/co\tdex"})
    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": "/usr/bin/co\u200bdex"})
    with pytest.raises(ValidationError):
        CodexSettingsRequest.model_validate({"path": "/" + ("界" * 1_366)})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": ""})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": "  "})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": "key\nvalue"})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": "key\u200bvalue"})
    with pytest.raises(ValidationError):
        BaiduSettingsRequest.model_validate({"api_key": "界" * 1_366})
    with pytest.raises(ValidationError):
        DeepSeekSettingsRequest.model_validate({"api_key": "\x00"})
    with pytest.raises(ValidationError):
        DeepSeekSettingsRequest.model_validate({"api_key": "界" * 1_366})
    with pytest.raises(ValidationError):
        DeepSeekSettingsRequest.model_validate({"api_key": "\ud800"})


def test_settings_response_exposes_non_secret_configuration_only() -> None:
    response = WorkbenchSettingsResponse(
        deepseek_model="deepseek-v4-pro",
        deepseek_key_masked="sk-****1234",
        codex_cli_path="/usr/bin/codex",
        baidu_key_masked="bce-****5678",
    )

    assert response.model_dump() == {
        "deepseek_model": "deepseek-v4-pro",
        "deepseek_key_masked": "sk-****1234",
        "codex_cli_path": "/usr/bin/codex",
        "baidu_key_masked": "bce-****5678",
    }


def test_settings_response_masks_raw_credentials_instead_of_serializing_them() -> None:
    deepseek_key = "sk-live-secret-1234"
    baidu_key = "baidu-live-secret-5678"

    response = WorkbenchSettingsResponse(
        deepseek_model="deepseek-v4-pro",
        deepseek_key_masked=deepseek_key,
        baidu_key_masked=baidu_key,
    )

    payload = response.model_dump()
    assert payload["deepseek_key_masked"] == "sk-****1234"
    assert payload["baidu_key_masked"] == "bai****5678"
    assert deepseek_key not in json.dumps(payload)
    assert baidu_key not in json.dumps(payload)


def test_mask_secret() -> None:
    assert mask_secret("sk-1234567890") == "sk-****7890"
    assert mask_secret("") is None


def test_keychain_save_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "find-generic-password" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="sk-test\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    save_secret("pdf2md.deepseek", "api-key", "sk-test")
    assert read_secret("pdf2md.deepseek", "api-key") == "sk-test"
    assert any("add-generic-password" in cmd for cmd in calls)


def test_keychain_read_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(KeychainError):
        read_secret("pdf2md.deepseek", "api-key")
