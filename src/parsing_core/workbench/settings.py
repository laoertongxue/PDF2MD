from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

from parsing_core.storage.fs_layout import FsLayout, UnsafeTaskPathError

DEEPSEEK_MODEL = "deepseek-v4-pro"
MAX_CODEX_CLI_PATH_BYTES = 4096
MAX_SETTINGS_FILE_BYTES = 64 * 1024
SETTINGS_FILENAME = "workbench-settings.json"
SETTINGS_LOCK_FILENAME = ".workbench-settings.lock"

_INVALID_SETTINGS = "invalid workbench settings"
_LOAD_FAILED = "unable to load workbench settings"
_SAVE_FAILED = "unable to save workbench settings"
_SAVE_DURABILITY_UNCERTAIN = "workbench settings saved but durability is uncertain"
_ALLOWED_FIELDS = frozenset({"deepseek_model", "codex_cli_path"})
_READ_CHUNK_BYTES = 16 * 1024
_PATH_LOOKUP_ATTEMPTS = 4
_TEMP_CREATE_ATTEMPTS = 16
_EINTR_ATTEMPTS = 8
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01

_MISSING = object()
_UNCHANGED = object()
_T = TypeVar("_T")

# Local indirections keep failure-path tests scoped to this module.
_close = os.close
_fchmod = os.fchmod
_flock = fcntl.flock
_fsync = os.fsync
_monotonic = time.monotonic
_open = os.open
_pread = os.pread
_read = os.read
_replace = os.replace
_sleep = time.sleep
_unlink = os.unlink
_write = os.write


class SettingsError(ValueError):
    """Raised when workbench settings cannot be validated or persisted safely."""


class SettingsCommitError(SettingsError):
    """The settings file was replaced, but durability or identity is uncertain."""

    committed = True
    durability_uncertain = True

    def __init__(self, *, identity_uncertain: bool = False) -> None:
        super().__init__(_SAVE_DURABILITY_UNCERTAIN)
        self.identity_uncertain = identity_uncertain


class _RootIdentityError(SettingsError):
    pass


class _LockIdentityError(SettingsError):
    pass


@dataclass(frozen=True)
class WorkbenchSettings:
    deepseek_model: str = DEEPSEEK_MODEL
    codex_cli_path: str | None = None

    def __post_init__(self) -> None:
        deepseek_model = _exact_builtin_str(self.deepseek_model)
        if deepseek_model != DEEPSEEK_MODEL:
            raise SettingsError(_INVALID_SETTINGS)
        codex_cli_path = _validate_codex_cli_path(self.codex_cli_path)
        object.__setattr__(self, "deepseek_model", deepseek_model)
        object.__setattr__(self, "codex_cli_path", codex_cli_path)


@dataclass
class _TrustedRoot:
    path: str
    identity: tuple[int, int]
    fd: int
    layout: FsLayout | None = None


@dataclass
class _SettingsLock:
    fd: int
    identity: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _VerifiedTempPath(os.PathLike[str]):
    parent_fd: int
    name: str
    fd: int
    expected_inode: tuple[int, int]
    payload: bytes | None

    def __fspath__(self) -> str:
        _assert_named_temp(
            self.parent_fd,
            self.name,
            self.fd,
            self.expected_inode,
            self.payload,
        )
        return self.name


class SettingsStore:
    """Persist one fixed settings file beneath a bound, private trusted root.

    The root must be a canonical absolute path to a directory owned by the
    current user with mode 0700. Settings and lock files are fixed direct
    children; callers cannot select either path.
    """

    def __init__(self, trusted_root: str | Path | FsLayout) -> None:
        self._layout: FsLayout | None
        if type(trusted_root) is FsLayout:
            self._layout = trusted_root
            self._root_path, self._root_identity = _bind_layout_root(trusted_root)
        else:
            self._layout = None
            self._root_path, self._root_identity = _bind_trusted_root(
                cast(str | Path, trusted_root)
            )

    @property
    def trusted_root(self) -> Path:
        return Path(self._root_path)

    def load(self) -> WorkbenchSettings:
        root: _TrustedRoot | None = None
        result = WorkbenchSettings()
        primary_error: SettingsError | None = None
        primary_cause: BaseException | None = None
        try:
            root = _open_store_root(self._root_path, self._root_identity, self._layout)
            result = _load_settings_from_root(root)
            _assert_root_current(root)
        except _RootIdentityError as exc:
            primary_error = exc
        except SettingsError as exc:
            primary_error = exc
        except OSError as exc:
            primary_error = SettingsError(_LOAD_FAILED)
            primary_cause = exc
        finally:
            cleanup_error = _close_root(root)

        if primary_error is not None:
            if primary_cause is not None:
                raise primary_error from primary_cause
            raise primary_error
        if cleanup_error is not None:
            raise SettingsError(_LOAD_FAILED) from cleanup_error
        return result

    def save(self, settings: WorkbenchSettings) -> None:
        payload = _settings_payload(settings)

        def persist(root: _TrustedRoot) -> None:
            _write_settings_to_root(root, payload)

        self._run_exclusive(persist)

    def update_fields(
        self,
        *,
        deepseek_model: object = _UNCHANGED,
        codex_cli_path: object = _UNCHANGED,
    ) -> WorkbenchSettings:
        """Merge explicit non-secret fields while holding the process lock.

        Values are validated before the trusted root or lock is opened. No
        caller-supplied code executes inside the critical section.
        """

        validated_model, validated_codex_path = _validate_settings_patch(
            deepseek_model=deepseek_model,
            codex_cli_path=codex_cli_path,
        )

        def persist(root: _TrustedRoot) -> WorkbenchSettings:
            current = _load_settings_from_root(root)
            _assert_root_current(root)
            updated = WorkbenchSettings(
                deepseek_model=(
                    current.deepseek_model
                    if validated_model is _UNCHANGED
                    else cast(str, validated_model)
                ),
                codex_cli_path=(
                    current.codex_cli_path
                    if validated_codex_path is _UNCHANGED
                    else cast(str | None, validated_codex_path)
                ),
            )
            payload = _settings_payload(updated)
            _write_settings_to_root(root, payload)
            return updated

        return self._run_exclusive(persist)

    def _run_exclusive(self, operation: Callable[[_TrustedRoot], _T]) -> _T:
        root: _TrustedRoot | None = None
        lock: _SettingsLock | None = None
        locked = False
        committed = False
        result: object = _MISSING
        primary_error: SettingsError | None = None
        primary_cause: BaseException | None = None
        primary_control: BaseException | None = None

        try:
            root = _open_store_root(self._root_path, self._root_identity, self._layout)
            lock = _open_lock_file(root)
            _acquire_exclusive_lock(root.fd)
            locked = True
            _assert_lock_current(root, lock)
            _assert_root_current(root)
            _assert_lock_current(root, lock)
            result = operation(root)
            committed = True
            _assert_lock_current(root, lock)
            _assert_root_current(root)
        except (_RootIdentityError, _LockIdentityError) as exc:
            if committed:
                primary_error = SettingsCommitError(identity_uncertain=True)
                primary_cause = exc
            else:
                primary_error = exc
                primary_cause = exc.__cause__
        except SettingsError as exc:
            primary_error = exc
            primary_cause = exc.__cause__
        except Exception as exc:
            primary_error = SettingsError(_SAVE_FAILED)
            primary_cause = exc
        except BaseException as exc:
            if committed:
                primary_error = SettingsCommitError(identity_uncertain=True)
            else:
                primary_control = exc
        finally:
            cleanup_error = _release_locked_root(root, lock, locked)
            lock = None

        if primary_control is not None:
            raise primary_control.with_traceback(primary_control.__traceback__) from None
        if primary_error is not None:
            if primary_cause is not None:
                raise primary_error from primary_cause
            raise primary_error from None
        if cleanup_error is not None:
            if committed:
                raise SettingsCommitError() from None
            raise SettingsError(_SAVE_FAILED) from cleanup_error
        if result is _MISSING:  # pragma: no cover - operation returned or raised
            raise SettingsError(_SAVE_FAILED)
        return cast(_T, result)


def load_settings(trusted_root: str | Path | FsLayout) -> WorkbenchSettings:
    """Load settings from the fixed file below ``trusted_root``."""

    return SettingsStore(trusted_root).load()


def save_settings(
    trusted_root: str | Path | FsLayout,
    settings: WorkbenchSettings,
) -> None:
    """Atomically replace settings below ``trusted_root`` under the store lock."""

    SettingsStore(trusted_root).save(settings)


def update_settings_fields(
    trusted_root: str | Path | FsLayout,
    *,
    deepseek_model: object = _UNCHANGED,
    codex_cli_path: object = _UNCHANGED,
) -> WorkbenchSettings:
    """Atomically merge validated fields, preserving every omitted field."""

    return SettingsStore(trusted_root).update_fields(
        deepseek_model=deepseek_model,
        codex_cli_path=codex_cli_path,
    )


def _validate_settings_patch(
    *,
    deepseek_model: object,
    codex_cli_path: object,
) -> tuple[object, object]:
    if deepseek_model is not _UNCHANGED:
        deepseek_model = _exact_builtin_str(deepseek_model)
        if deepseek_model != DEEPSEEK_MODEL:
            raise SettingsError(_INVALID_SETTINGS)
    if codex_cli_path is not _UNCHANGED:
        codex_cli_path = _validate_codex_cli_path(codex_cli_path)
    return deepseek_model, codex_cli_path


def _validate_codex_cli_path(value: object) -> str | None:
    if value is None:
        return None
    value = _exact_builtin_str(value)
    encoded = value.encode("utf-8")
    if (
        not value
        or value != value.strip()
        or _has_control_character(value)
        or len(encoded) > MAX_CODEX_CLI_PATH_BYTES
        or not os.path.isabs(value)
        or value.startswith("//")
        or os.path.normpath(value) != value
    ):
        raise SettingsError(_INVALID_SETTINGS)
    return value


def _exact_builtin_str(value: object) -> str:
    if not isinstance(value, str):
        raise SettingsError(_INVALID_SETTINGS)
    try:
        return bytes.decode(str.encode(value, "utf-8", "strict"), "utf-8", "strict")
    except UnicodeError as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc


def _has_control_character(value: str) -> bool:
    return any(not character.isprintable() for character in value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate settings field")
        result[key] = value
    return result


def _decode_settings(raw: bytes | None) -> WorkbenchSettings:
    if raw is None:
        return WorkbenchSettings()
    try:
        decoded = raw.decode("utf-8")
        data = json.loads(decoded, object_pairs_hook=_unique_object)
    except RecursionError:
        raise SettingsError(_INVALID_SETTINGS) from None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc

    if not isinstance(data, dict) or not set(data).issubset(_ALLOWED_FIELDS):
        raise SettingsError(_INVALID_SETTINGS)
    deepseek_model = data.get("deepseek_model", DEEPSEEK_MODEL)
    codex_cli_path = data.get("codex_cli_path")
    if not isinstance(deepseek_model, str):
        raise SettingsError(_INVALID_SETTINGS)
    return WorkbenchSettings(
        deepseek_model=deepseek_model,
        codex_cli_path=codex_cli_path,
    )


def _settings_payload(settings: WorkbenchSettings) -> bytes:
    if not isinstance(settings, WorkbenchSettings):
        raise SettingsError(_INVALID_SETTINGS)
    validated = WorkbenchSettings(
        deepseek_model=settings.deepseek_model,
        codex_cli_path=settings.codex_cli_path,
    )
    payload = (
        json.dumps(
            {
                "deepseek_model": validated.deepseek_model,
                "codex_cli_path": validated.codex_cli_path,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_SETTINGS_FILE_BYTES:
        raise SettingsError(_INVALID_SETTINGS)
    return payload


def _bind_trusted_root(trusted_root: str | Path) -> tuple[str, tuple[int, int]]:
    root_path = _trusted_root_path(trusted_root)
    try:
        root_stat = _retry_eintr(lambda: os.stat(root_path, follow_symlinks=False))
    except OSError as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc
    _validate_trusted_root(root_stat)
    return root_path, _directory_snapshot(root_stat)


def _bind_layout_root(layout: FsLayout) -> tuple[str, tuple[int, int]]:
    root_fd = -1
    try:
        root_fd = layout.open_base_fd()
        root_stat = _retry_eintr(lambda: os.fstat(root_fd))
        _validate_trusted_root(root_stat)
        layout.verify_base_fd(root_fd)
        return layout.base_dir, _directory_snapshot(root_stat)
    except (OSError, SettingsError, UnsafeTaskPathError):
        raise SettingsError(_INVALID_SETTINGS) from None
    finally:
        if root_fd >= 0:
            root_to_close = root_fd
            root_fd = -1
            close_error = _close_once(root_to_close)
            if close_error is not None:
                raise SettingsError(_INVALID_SETTINGS) from None


def _open_store_root(
    root_path: str,
    expected_identity: tuple[int, int],
    layout: FsLayout | None,
) -> _TrustedRoot:
    if layout is None:
        return _open_trusted_root(root_path, expected_identity)

    root_fd = -1
    try:
        root_fd = layout.open_base_fd()
        opened_stat = _retry_eintr(lambda: os.fstat(root_fd))
        _validate_trusted_root(opened_stat)
        if _directory_snapshot(opened_stat) != expected_identity:
            raise _RootIdentityError(_INVALID_SETTINGS)
        layout.verify_base_fd(root_fd)
        result_fd = root_fd
        root_fd = -1
        return _TrustedRoot(root_path, expected_identity, result_fd, layout)
    except _RootIdentityError:
        raise
    except (OSError, SettingsError, UnsafeTaskPathError):
        raise _RootIdentityError(_INVALID_SETTINGS) from None
    finally:
        if root_fd >= 0:
            root_to_close = root_fd
            root_fd = -1
            _close_once(root_to_close)


def _open_trusted_root(
    root_path: str,
    expected_identity: tuple[int, int],
) -> _TrustedRoot:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        path_stat = _retry_eintr(lambda: os.stat(root_path, follow_symlinks=False))
    except OSError as exc:
        raise _RootIdentityError(_INVALID_SETTINGS) from exc
    _validate_trusted_root(path_stat)
    if _directory_snapshot(path_stat) != expected_identity:
        raise _RootIdentityError(_INVALID_SETTINGS)

    root_fd = -1
    try:
        root_fd = _retry_eintr(lambda: _open(root_path, flags))
        opened_stat = _retry_eintr(lambda: os.fstat(root_fd))
        _validate_trusted_root(opened_stat)
        if _directory_snapshot(opened_stat) != expected_identity:
            raise _RootIdentityError(_INVALID_SETTINGS)
        result_fd = root_fd
        root_fd = -1
        return _TrustedRoot(root_path, expected_identity, result_fd)
    except SettingsError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _RootIdentityError(_INVALID_SETTINGS) from exc
        raise
    finally:
        if root_fd >= 0:
            root_to_close = root_fd
            root_fd = -1
            _close_once(root_to_close)


def _assert_root_current(root: _TrustedRoot) -> None:
    if root.layout is not None:
        try:
            opened_stat = _retry_eintr(lambda: os.fstat(root.fd))
            _validate_trusted_root(opened_stat)
            if _directory_snapshot(opened_stat) != root.identity:
                raise _RootIdentityError(_INVALID_SETTINGS)
            root.layout.verify_base_fd(root.fd)
        except _RootIdentityError:
            raise
        except (OSError, SettingsError, UnsafeTaskPathError):
            raise _RootIdentityError(_INVALID_SETTINGS) from None
        return
    try:
        opened_stat = _retry_eintr(lambda: os.fstat(root.fd))
        path_stat = _retry_eintr(lambda: os.stat(root.path, follow_symlinks=False))
        _validate_trusted_root(opened_stat)
        _validate_trusted_root(path_stat)
    except (OSError, SettingsError) as exc:
        raise _RootIdentityError(_INVALID_SETTINGS) from exc
    if (
        _directory_snapshot(opened_stat) != root.identity
        or _directory_snapshot(path_stat) != root.identity
    ):
        raise _RootIdentityError(_INVALID_SETTINGS)


def _close_root(root: _TrustedRoot | None) -> OSError | None:
    if root is None or root.fd < 0:
        return None
    root_to_close = root.fd
    root.fd = -1
    return _close_once(root_to_close)


def _release_locked_root(
    root: _TrustedRoot | None,
    lock: _SettingsLock | None,
    locked: bool,
) -> BaseException | None:
    cleanup_error: BaseException | None = None
    if locked and lock is not None and root is not None:
        try:
            _assert_lock_current(root, lock)
        except BaseException as exc:
            cleanup_error = exc
        try:
            _retry_eintr(lambda: _flock(root.fd, fcntl.LOCK_UN))
        except OSError as exc:
            if cleanup_error is None:
                cleanup_error = exc
    if lock is not None and lock.fd >= 0:
        lock_to_close = lock.fd
        lock.fd = -1
        close_error = _close_once(lock_to_close)
        if cleanup_error is None:
            cleanup_error = close_error
    root_close_error = _close_root(root)
    if cleanup_error is None:
        cleanup_error = root_close_error
    return cleanup_error


def _trusted_root_path(trusted_root: str | Path) -> str:
    try:
        raw_path = os.fspath(trusted_root)
    except TypeError as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc
    raw_path = _exact_builtin_str(raw_path)
    if (
        not raw_path
        or _has_control_character(raw_path)
        or not os.path.isabs(raw_path)
        or raw_path.startswith("//")
        or os.path.normpath(raw_path) != raw_path
    ):
        raise SettingsError(_INVALID_SETTINGS)
    return raw_path


def _validate_trusted_root(root_stat: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise SettingsError(_INVALID_SETTINGS)


def _directory_snapshot(directory_stat: os.stat_result) -> tuple[int, int]:
    return directory_stat.st_dev, directory_stat.st_ino


def _load_settings_from_root(root: _TrustedRoot) -> WorkbenchSettings:
    _assert_root_current(root)
    raw = _read_bounded_regular_file(root.fd, SETTINGS_FILENAME)
    _assert_root_current(root)
    settings = _decode_settings(raw)
    _assert_root_current(root)
    return settings


def _write_settings_to_root(root: _TrustedRoot, payload: bytes) -> None:
    temp_fd = -1
    temp_name: str | None = None
    temp_inode: tuple[int, int] | None = None
    committed = False
    primary_error: SettingsError | None = None
    primary_cause: BaseException | None = None
    primary_control: BaseException | None = None
    cleanup_error: OSError | None = None

    try:
        _assert_root_current(root)
        _validate_destination(root.fd, SETTINGS_FILENAME)
        temp_fd, temp_name = _create_temp_file(root.fd, SETTINGS_FILENAME)
        _retry_eintr(lambda: _fchmod(temp_fd, 0o600))
        created_stat = _retry_eintr(lambda: os.fstat(temp_fd))
        _validate_private_regular_file(created_stat)
        temp_inode = _inode_snapshot(created_stat)
        _write_all(temp_fd, payload)
        _retry_eintr(lambda: _fsync(temp_fd))
        _assert_root_current(root)
        source_name = temp_name
        if source_name is None:  # pragma: no cover - temp creation returned a name
            raise SettingsError(_SAVE_FAILED)
        _assert_named_payload(
            root.fd,
            source_name,
            temp_fd,
            temp_inode,
            payload,
        )
        _validate_destination(root.fd, SETTINGS_FILENAME)
        _replace_verified_temp(
            root.fd,
            source_name,
            SETTINGS_FILENAME,
            temp_fd,
            temp_inode,
            payload,
        )
        committed = True
        temp_name = None
        _assert_named_payload(
            root.fd,
            SETTINGS_FILENAME,
            temp_fd,
            temp_inode,
            payload,
        )
        _assert_root_current(root)
        _retry_eintr(lambda: _fsync(root.fd))
        _assert_named_payload(
            root.fd,
            SETTINGS_FILENAME,
            temp_fd,
            temp_inode,
            payload,
        )
        _assert_root_current(root)

        temp_to_close = temp_fd
        temp_fd = -1
        close_error = _close_once(temp_to_close)
        if close_error is not None:
            raise close_error
    except SettingsCommitError as exc:
        primary_error = exc
    except _RootIdentityError as exc:
        if committed:
            primary_error = SettingsCommitError(identity_uncertain=True)
            primary_cause = exc
        else:
            primary_error = exc
    except SettingsError as exc:
        if committed:
            primary_error = SettingsCommitError(identity_uncertain=True)
            primary_cause = exc
        else:
            primary_error = exc
    except Exception as exc:
        if committed:
            primary_error = SettingsCommitError()
        else:
            primary_error = SettingsError(_SAVE_FAILED)
        primary_cause = exc
    except BaseException as exc:
        if committed:
            primary_error = SettingsCommitError(identity_uncertain=True)
        else:
            primary_control = exc
    finally:
        if temp_name is not None and temp_fd >= 0:
            unlink_error = _unlink_owned_temp(
                root.fd,
                temp_name,
                temp_fd,
                temp_inode,
            )
            if cleanup_error is None:
                cleanup_error = unlink_error
        if temp_fd >= 0:
            temp_to_close = temp_fd
            temp_fd = -1
            close_error = _close_once(temp_to_close)
            if cleanup_error is None:
                cleanup_error = close_error

    if primary_control is not None:
        raise primary_control.with_traceback(primary_control.__traceback__) from None
    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error from None
    if cleanup_error is not None:
        if committed:
            raise SettingsCommitError() from cleanup_error
        raise SettingsError(_SAVE_FAILED) from cleanup_error


def _open_lock_file(root: _TrustedRoot) -> _SettingsLock:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    try:
        lock_fd = _retry_eintr(lambda: _open(SETTINGS_LOCK_FILENAME, flags, 0o600, dir_fd=root.fd))
        created = True
    except FileExistsError:
        lock_fd = _retry_eintr(
            lambda: _open(
                SETTINGS_LOCK_FILENAME,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root.fd,
            )
        )

    try:
        if created:
            _retry_eintr(lambda: _fchmod(lock_fd, 0o600))
        lock_stat = _retry_eintr(lambda: os.fstat(lock_fd))
        _validate_lock_file(lock_stat)
        result = _SettingsLock(lock_fd, _file_snapshot(lock_stat))
        _assert_lock_current(root, result)
        lock_fd = -1
        return result
    finally:
        if lock_fd >= 0:
            lock_to_close = lock_fd
            lock_fd = -1
            _close_once(lock_to_close)


def _assert_lock_current(root: _TrustedRoot, lock: _SettingsLock) -> None:
    try:
        opened_stat = _retry_eintr(lambda: os.fstat(lock.fd))
        path_stat = _retry_eintr(
            lambda: os.stat(
                SETTINGS_LOCK_FILENAME,
                dir_fd=root.fd,
                follow_symlinks=False,
            )
        )
        _validate_lock_file(opened_stat)
        _validate_lock_file(path_stat)
    except (OSError, SettingsError) as exc:
        raise _LockIdentityError(_INVALID_SETTINGS) from exc
    if _file_snapshot(opened_stat) != lock.identity or _file_snapshot(path_stat) != lock.identity:
        raise _LockIdentityError(_INVALID_SETTINGS)


def _validate_lock_file(file_stat: os.stat_result) -> None:
    _validate_private_regular_file(file_stat)
    if file_stat.st_size != 0:
        raise SettingsError(_INVALID_SETTINGS)


def _acquire_exclusive_lock(lock_fd: int) -> None:
    deadline = _monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _retry_eintr(lambda: _flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB))
            return
        except BlockingIOError as exc:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise OSError(errno.ETIMEDOUT, "settings lock acquisition timed out") from exc
            _sleep(min(_LOCK_POLL_SECONDS, remaining))


def _read_bounded_regular_file(parent_fd: int, name: str) -> bytes | None:
    opened = _open_regular_settings_file(parent_fd, name)
    if opened is None:
        return None
    fd, opened_stat = opened

    result: bytes | None = None
    primary_error: SettingsError | None = None
    primary_cause: BaseException | None = None
    try:
        chunks = bytearray()
        while len(chunks) <= MAX_SETTINGS_FILE_BYTES:
            remaining = MAX_SETTINGS_FILE_BYTES + 1 - len(chunks)
            chunk_size = min(_READ_CHUNK_BYTES, remaining)
            chunk = _retry_eintr(partial(_read, fd, chunk_size))
            if not chunk:
                break
            if len(chunk) > remaining:
                raise SettingsError(_INVALID_SETTINGS)
            chunks.extend(chunk)
        if len(chunks) > MAX_SETTINGS_FILE_BYTES:
            raise SettingsError(_INVALID_SETTINGS)

        final_stat = _retry_eintr(lambda: os.fstat(fd))
        _validate_regular_settings_file(final_stat)
        path_stat = _stat_regular_settings_path(parent_fd, name)
        if (
            _file_snapshot(opened_stat) != _file_snapshot(final_stat)
            or _file_snapshot(final_stat) != _file_snapshot(path_stat)
            or len(chunks) != final_stat.st_size
        ):
            raise SettingsError(_INVALID_SETTINGS)
        result = bytes(chunks)
    except SettingsError as exc:
        primary_error = exc
    except OSError as exc:
        primary_error = SettingsError(_LOAD_FAILED)
        primary_cause = exc
    finally:
        fd_to_close = fd
        fd = -1
        close_error = _close_once(fd_to_close)
        if primary_error is None and close_error is not None:
            primary_error = SettingsError(_LOAD_FAILED)
            primary_cause = close_error

    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    return result


def _open_regular_settings_file(
    parent_fd: int,
    name: str,
) -> tuple[int, os.stat_result] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for attempt in range(_PATH_LOOKUP_ATTEMPTS):
        try:
            entry_stat: os.stat_result | None = _retry_eintr(
                lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            )
        except FileNotFoundError:
            entry_stat = None
        except OSError as exc:
            raise SettingsError(_INVALID_SETTINGS) from exc
        if entry_stat is not None:
            _validate_regular_settings_file(entry_stat)

        try:
            fd = _retry_eintr(lambda: _open(name, flags, dir_fd=parent_fd))
        except FileNotFoundError as exc:
            if attempt + 1 < _PATH_LOOKUP_ATTEMPTS:
                continue
            try:
                appeared_stat = _retry_eintr(
                    lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                )
            except FileNotFoundError:
                if entry_stat is None:
                    try:
                        fd = _retry_eintr(lambda: _open(name, flags, dir_fd=parent_fd))
                    except FileNotFoundError:
                        return None
                    except OSError as final_open_error:
                        raise SettingsError(_INVALID_SETTINGS) from final_open_error
                    return _verify_opened_settings_file(parent_fd, name, fd, None)
                raise SettingsError(_INVALID_SETTINGS) from exc
            except OSError as stat_exc:
                raise SettingsError(_INVALID_SETTINGS) from stat_exc
            _validate_regular_settings_file(appeared_stat)
            raise SettingsError(_INVALID_SETTINGS) from exc
        except OSError as exc:
            raise SettingsError(_INVALID_SETTINGS) from exc

        return _verify_opened_settings_file(parent_fd, name, fd, entry_stat)

    raise SettingsError(_INVALID_SETTINGS)  # pragma: no cover - bounded loop returns


def _verify_opened_settings_file(
    parent_fd: int,
    name: str,
    fd: int,
    entry_stat: os.stat_result | None,
) -> tuple[int, os.stat_result]:
    try:
        opened_stat = _retry_eintr(partial(os.fstat, fd))
        _validate_regular_settings_file(opened_stat)
        if entry_stat is not None and _file_snapshot(entry_stat) != _file_snapshot(opened_stat):
            raise SettingsError(_INVALID_SETTINGS)
        path_stat = _stat_regular_settings_path(parent_fd, name)
        if _file_snapshot(opened_stat) != _file_snapshot(path_stat):
            raise SettingsError(_INVALID_SETTINGS)
    except BaseException:
        fd_to_close = fd
        fd = -1
        _close_once(fd_to_close)
        raise
    return fd, opened_stat


def _stat_regular_settings_path(parent_fd: int, name: str) -> os.stat_result:
    try:
        path_stat = _retry_eintr(lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except OSError as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc
    _validate_regular_settings_file(path_stat)
    return path_stat


def _validate_regular_settings_file(file_stat: os.stat_result) -> None:
    _validate_private_regular_file(file_stat)
    if file_stat.st_size < 0 or file_stat.st_size > MAX_SETTINGS_FILE_BYTES:
        raise SettingsError(_INVALID_SETTINGS)


def _validate_private_regular_file(file_stat: os.stat_result) -> None:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_nlink != 1
    ):
        raise SettingsError(_INVALID_SETTINGS)


def _file_snapshot(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_uid,
        stat.S_IMODE(file_stat.st_mode),
        file_stat.st_nlink,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _inode_snapshot(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _validate_destination(parent_fd: int, name: str) -> None:
    try:
        file_stat = _retry_eintr(lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SettingsError(_INVALID_SETTINGS) from exc
    _validate_regular_settings_file(file_stat)


def _create_temp_file(parent_fd: int, destination_name: str) -> tuple[int, str]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(_TEMP_CREATE_ATTEMPTS):
        name = f".{destination_name}.{secrets.token_hex(8)}.tmp"
        try:
            return (
                _retry_eintr(partial(_open, name, flags, 0o600, dir_fd=parent_fd)),
                name,
            )
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "unable to allocate settings temporary file")


def _assert_named_payload(
    parent_fd: int,
    name: str,
    fd: int,
    expected_inode: tuple[int, int],
    payload: bytes,
) -> None:
    _assert_named_temp(parent_fd, name, fd, expected_inode, payload)


def _assert_named_temp(
    parent_fd: int,
    name: str,
    fd: int,
    expected_inode: tuple[int, int],
    payload: bytes | None,
) -> None:
    try:
        opened_stat = _retry_eintr(lambda: os.fstat(fd))
        path_stat = _retry_eintr(lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        _validate_private_regular_file(opened_stat)
        _validate_private_regular_file(path_stat)
        if (
            _inode_snapshot(opened_stat) != expected_inode
            or _file_snapshot(opened_stat) != _file_snapshot(path_stat)
            or (payload is not None and opened_stat.st_size != len(payload))
        ):
            raise SettingsError(_SAVE_FAILED)
        if payload is not None:
            _assert_fd_payload(fd, payload)
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError(_SAVE_FAILED) from exc


def _replace_verified_temp(
    parent_fd: int,
    source_name: str,
    destination_name: str,
    temp_fd: int,
    temp_inode: tuple[int, int],
    payload: bytes,
) -> None:
    verified_source = _VerifiedTempPath(
        parent_fd,
        source_name,
        temp_fd,
        temp_inode,
        payload,
    )
    replace_error: BaseException | None = None
    try:
        _replace(
            verified_source,
            destination_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except BaseException as exc:
        replace_error = exc

    if replace_error is None:
        return
    if isinstance(replace_error, SettingsError):
        # Path verification rejected the replace before the syscall ran, so
        # nothing was committed: report a clean save failure, not uncertainty.
        raise SettingsError(_SAVE_FAILED) from None
    if _named_payload_matches(
        parent_fd,
        destination_name,
        temp_fd,
        temp_inode,
        payload,
    ):
        raise SettingsCommitError() from None
    if _named_payload_matches(
        parent_fd,
        source_name,
        temp_fd,
        temp_inode,
        payload,
    ):
        if isinstance(replace_error, Exception):
            raise SettingsError(_SAVE_FAILED) from replace_error
        raise replace_error.with_traceback(replace_error.__traceback__) from None
    raise SettingsCommitError(identity_uncertain=True) from None


def _named_payload_matches(
    parent_fd: int,
    name: str,
    fd: int,
    expected_inode: tuple[int, int],
    payload: bytes,
) -> bool:
    try:
        _assert_named_payload(parent_fd, name, fd, expected_inode, payload)
    except SettingsError:
        return False
    return True


def _assert_fd_payload(fd: int, payload: bytes) -> None:
    before = _retry_eintr(lambda: os.fstat(fd))
    _validate_private_regular_file(before)
    if before.st_size != len(payload):
        raise SettingsError(_SAVE_FAILED)

    chunks = bytearray()
    offset = 0
    while offset < len(payload):
        size = min(_READ_CHUNK_BYTES, len(payload) - offset)
        chunk = _retry_eintr(partial(_pread, fd, size, offset))
        if not chunk or len(chunk) > size:
            raise SettingsError(_SAVE_FAILED)
        chunks.extend(chunk)
        offset += len(chunk)
    if bytes(chunks) != payload:
        raise SettingsError(_SAVE_FAILED)

    after = _retry_eintr(lambda: os.fstat(fd))
    _validate_private_regular_file(after)
    if _file_snapshot(before) != _file_snapshot(after):
        raise SettingsError(_SAVE_FAILED)


def _unlink_owned_temp(
    parent_fd: int,
    name: str,
    fd: int,
    expected_inode: tuple[int, int] | None,
) -> OSError | None:
    if expected_inode is None:
        return None
    verified_name = _VerifiedTempPath(parent_fd, name, fd, expected_inode, None)
    try:
        _retry_eintr(lambda: _unlink(verified_name, dir_fd=parent_fd))
    except FileNotFoundError:
        return None
    except SettingsError:
        return None
    except OSError as exc:
        return exc
    return None


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        current = remaining
        written = _retry_eintr(partial(_write, fd, current))
        if written <= 0 or written > len(current):
            raise OSError(errno.EIO, "short settings write made no progress")
        remaining = current[written:]


def _retry_eintr(operation: Callable[[], _T]) -> _T:  # noqa: UP047
    last_error: InterruptedError | None = None
    for _attempt in range(_EINTR_ATTEMPTS):
        try:
            return operation()
        except InterruptedError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _close_once(fd: int) -> OSError | None:
    try:
        _close(fd)
    except OSError as exc:
        return exc
    return None
