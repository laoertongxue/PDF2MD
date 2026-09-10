from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TypeVar

_EINTR_ATTEMPTS = 8
_POLICY_ANCESTRY = "ancestry"
_POLICY_ANCHOR = "anchor"
_POLICY_PRIVATE = "private"
MAX_TASK_SCAN_BATCH = 256
MAX_TREE_DELETE_DEPTH = 32
MAX_TREE_DELETE_ENTRIES = 100_000
MAX_MANAGED_NAME_BYTES = 255
_T = TypeVar("_T")


def _retry_eintr(operation: Callable[[], _T]) -> _T:  # noqa: UP047
    last_error: InterruptedError | None = None
    for _attempt in range(_EINTR_ATTEMPTS):
        try:
            return operation()
        except InterruptedError as error:
            last_error = error
    assert last_error is not None
    raise last_error


class UnsafeTaskPathError(RuntimeError):
    """A managed directory cannot be used without following an unsafe path."""


@dataclass(frozen=True)
class _DirectoryBinding:
    name: str
    identity: tuple[int, int]
    owner: int
    mode: int
    policy: str


@dataclass(frozen=True)
class TaskDirectoryEntry:
    name: str
    identity: tuple[int, int]
    mode: int


@dataclass
class TaskDirectoryCapability:
    layout: FsLayout
    task_id: str
    entry_name: str
    tasks_fd: int
    task_fd: int
    task_identity: tuple[int, int]
    images_fd: int | None = None
    images_identity: tuple[int, int] | None = None
    _closed: bool = False

    @property
    def task_path(self) -> str:
        return str(Path(self.layout.base_dir) / "tasks" / self.task_id)

    @property
    def directory_path(self) -> str:
        return str(Path(self.layout.base_dir) / "tasks" / self.entry_name)

    @property
    def images_path(self) -> str:
        if self.images_fd is None:
            raise UnsafeTaskPathError("unsafe images directory: capability is unavailable")
        return str(Path(self.directory_path) / "images")

    def verify(self) -> None:
        if self._closed:
            raise UnsafeTaskPathError("unsafe task directory: capability is closed")
        live_tasks_fd = self.layout.open_tasks_fd()
        try:
            if _directory_identity(os.fstat(live_tasks_fd)) != _directory_identity(
                os.fstat(self.tasks_fd)
            ):
                raise UnsafeTaskPathError("unsafe tasks directory: identity changed")
        finally:
            close_error = _close_once(live_tasks_fd)
            if close_error is not None:
                raise UnsafeTaskPathError(
                    "unsafe tasks directory: could not close directory"
                ) from close_error
        _assert_private_directory_entry(
            self.tasks_fd,
            self.entry_name,
            self.task_fd,
            self.task_identity,
            "task directory",
        )
        if self.images_fd is not None:
            images_identity = self.images_identity
            if images_identity is None:
                raise UnsafeTaskPathError("unsafe images directory: missing identity")
            _assert_private_directory_entry(
                self.task_fd,
                "images",
                self.images_fd,
                images_identity,
                "images directory",
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors = [
            error
            for error in (
                _close_once(self.images_fd) if self.images_fd is not None else None,
                _close_once(self.task_fd),
                _close_once(self.tasks_fd),
            )
            if error is not None
        ]
        if errors:
            raise UnsafeTaskPathError(
                "unsafe task directory: could not close capability"
            ) from errors[0]


@dataclass
class _TreeDeleteBudget:
    entries: int = 0

    def consume(self) -> None:
        self.entries += 1
        if self.entries > MAX_TREE_DELETE_ENTRIES:
            raise UnsafeTaskPathError("unsafe tree entry: entry limit exceeded")


class FsLayout:
    """Create private ``base/tasks/<task>/images`` directories via bound dirfds."""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        if base_dir is None:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
            base_dir = str(Path(base) / "parsing-core")
        self.base_dir = _canonical_base_directory(base_dir)
        self._base_bindings, self._tasks_identity = self._prepare_directory_tree()

    @staticmethod
    def validate_task_id(task_id: str) -> None:
        _validated_task_id(task_id)

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @classmethod
    def _open_directory(
        cls,
        path: str,
        label: str,
        *,
        dir_fd: int | None = None,
    ) -> int:
        try:
            directory_fd = _retry_eintr(
                lambda: os.open(path, cls._directory_flags(), dir_fd=dir_fd)
            )
        except OSError as error:
            raise UnsafeTaskPathError(f"unsafe {label}: expected a real directory") from error
        try:
            directory_stat = _retry_eintr(lambda: os.fstat(directory_fd))
            _validate_directory_type(directory_stat, label)
        except BaseException as primary:
            _close_owned_after_error(directory_fd, primary, label)
            raise
        return directory_fd

    @classmethod
    def _open_existing_directory(
        cls,
        parent_fd: int,
        name: str,
        label: str,
    ) -> tuple[int, os.stat_result]:
        path_stat = _stat_directory_entry(parent_fd, name, label)
        _validate_directory_type(path_stat, label)
        directory_fd = cls._open_directory(name, label, dir_fd=parent_fd)
        try:
            opened_stat = _retry_eintr(lambda: os.fstat(directory_fd))
            _validate_directory_type(opened_stat, label)
            if _directory_identity(path_stat) != _directory_identity(opened_stat):
                raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")
            current_stat = _stat_directory_entry(parent_fd, name, label)
            if _directory_identity(current_stat) != _directory_identity(opened_stat):
                raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")
        except BaseException as primary:
            _close_owned_after_error(directory_fd, primary, label)
            raise
        return directory_fd, opened_stat

    @classmethod
    def _open_or_create_private_directory(
        cls,
        parent_fd: int,
        name: str,
        label: str,
    ) -> tuple[int, tuple[int, int]]:
        created = False
        created_identity: tuple[int, int] | None = None
        try:
            _retry_eintr(lambda: os.mkdir(name, mode=0o700, dir_fd=parent_fd))
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise UnsafeTaskPathError(f"unsafe {label}: could not create directory") from error

        if created:
            created_identity = _make_created_entry_accessible(parent_fd, name, label)

        try:
            directory_fd, opened_stat = cls._open_existing_directory(parent_fd, name, label)
        except FileNotFoundError as error:
            raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed") from error
        try:
            if created_identity is not None and (
                _directory_identity(opened_stat) != created_identity
            ):
                raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")
            _secure_opened_directory(directory_fd, opened_stat, label, created=created)
            final_stat = _retry_eintr(lambda: os.fstat(directory_fd))
            identity = _directory_identity(final_stat)
            _assert_private_directory_entry(
                parent_fd,
                name,
                directory_fd,
                identity,
                label,
            )
        except BaseException as primary:
            _close_owned_after_error(directory_fd, primary, label)
            raise
        return directory_fd, identity

    def _prepare_directory_tree(
        self,
    ) -> tuple[tuple[_DirectoryBinding, ...], tuple[int, int]]:
        components = _absolute_components(self.base_dir)
        bindings: list[_DirectoryBinding] = []
        missing_chain = False

        with _directory_fd_stack("base directory") as directory_fds:
            root_fd = self._open_directory(os.sep, "base directory")
            directory_fds.append(root_fd)
            root_stat = _retry_eintr(lambda: os.fstat(root_fd))
            bindings.append(_binding(os.sep, root_stat, _POLICY_ANCESTRY))

            for index, name in enumerate(components):
                parent_fd = directory_fds[-1]
                is_base = index + 1 == len(components)
                if not missing_chain:
                    try:
                        child_fd, child_stat = self._open_existing_directory(
                            parent_fd,
                            name,
                            "base directory",
                        )
                    except FileNotFoundError:
                        anchor_stat = _retry_eintr(partial(os.fstat, parent_fd))
                        _validate_trusted_anchor(anchor_stat, "base directory")
                        bindings[-1] = _binding(
                            bindings[-1].name,
                            anchor_stat,
                            _POLICY_ANCHOR,
                        )
                        missing_chain = True
                        child_fd, _identity = self._open_or_create_private_directory(
                            parent_fd,
                            name,
                            "base directory",
                        )
                        child_stat = _retry_eintr(partial(os.fstat, child_fd))
                        policy = _POLICY_PRIVATE
                    else:
                        directory_fds.append(child_fd)
                        if is_base:
                            _secure_opened_directory(
                                child_fd,
                                child_stat,
                                "base directory",
                                created=False,
                            )
                            child_stat = _retry_eintr(partial(os.fstat, child_fd))
                            _assert_private_directory_entry(
                                parent_fd,
                                name,
                                child_fd,
                                _directory_identity(child_stat),
                                "base directory",
                            )
                            policy = _POLICY_PRIVATE
                        else:
                            policy = _POLICY_ANCESTRY
                        bindings.append(_binding(name, child_stat, policy))
                        continue
                else:
                    child_fd, _identity = self._open_or_create_private_directory(
                        parent_fd,
                        name,
                        "base directory",
                    )
                    child_stat = _retry_eintr(partial(os.fstat, child_fd))
                    policy = _POLICY_PRIVATE

                directory_fds.append(child_fd)
                bindings.append(_binding(name, child_stat, policy))

            if not missing_chain:
                anchor_index = len(bindings) - 2
                if anchor_index < 0:
                    raise UnsafeTaskPathError("unsafe base directory: no trusted anchor")
                anchor_stat = _retry_eintr(lambda: os.fstat(directory_fds[anchor_index]))
                _validate_trusted_anchor(anchor_stat, "base directory")
                bindings[anchor_index] = _binding(
                    bindings[anchor_index].name,
                    anchor_stat,
                    _POLICY_ANCHOR,
                )

            _assert_binding_chain(directory_fds, bindings, "base directory")
            base_fd = directory_fds[-1]
            tasks_fd, tasks_identity = self._open_or_create_private_directory(
                base_fd,
                "tasks",
                "tasks directory",
            )
            directory_fds.append(tasks_fd)
            _assert_private_directory_entry(
                base_fd,
                "tasks",
                tasks_fd,
                tasks_identity,
                "tasks directory",
            )
            _assert_binding_chain(
                directory_fds[: len(bindings)],
                bindings,
                "base directory",
            )
            return tuple(bindings), tasks_identity

    @contextmanager
    def _open_bound_base_chain(self) -> Iterator[list[int]]:
        with _directory_fd_stack("base directory") as directory_fds:
            root_binding = self._base_bindings[0]
            root_fd = self._open_directory(os.sep, "base directory")
            directory_fds.append(root_fd)
            _assert_bound_directory(root_fd, root_binding, "base directory")

            for binding in self._base_bindings[1:]:
                try:
                    child_fd, _child_stat = self._open_existing_directory(
                        directory_fds[-1],
                        binding.name,
                        "base directory",
                    )
                except FileNotFoundError as error:
                    raise UnsafeTaskPathError(
                        "unsafe base directory: directory identity changed"
                    ) from error
                directory_fds.append(child_fd)
                _assert_bound_directory(child_fd, binding, "base directory")

            _assert_binding_chain(directory_fds, self._base_bindings, "base directory")
            try:
                yield directory_fds
            except BaseException:
                raise
            else:
                _assert_binding_chain(
                    directory_fds[: len(self._base_bindings)],
                    self._base_bindings,
                    "base directory",
                )

    def _open_bound_tasks(self, base_fd: int) -> int:
        try:
            tasks_fd, _tasks_stat = self._open_existing_directory(
                base_fd,
                "tasks",
                "tasks directory",
            )
        except FileNotFoundError as error:
            raise UnsafeTaskPathError("unsafe tasks directory: identity changed") from error
        try:
            _assert_private_directory_entry(
                base_fd,
                "tasks",
                tasks_fd,
                self._tasks_identity,
                "tasks directory",
            )
        except BaseException as primary:
            _close_owned_after_error(tasks_fd, primary, "tasks directory")
            raise
        return tasks_fd

    def _assert_base_chain(self, directory_fds: list[int]) -> None:
        _assert_binding_chain(
            directory_fds[: len(self._base_bindings)],
            self._base_bindings,
            "base directory",
        )

    def open_base_fd(self) -> int:
        """Return an owned FD for the originally bound base directory."""

        duplicate_fd = -1
        try:
            with self._open_bound_base_chain() as directory_fds:
                duplicate_fd = _retry_eintr(lambda: os.dup(directory_fds[-1]))
                _assert_bound_directory(
                    duplicate_fd,
                    self._base_bindings[-1],
                    "base directory",
                )
            result_fd = duplicate_fd
            duplicate_fd = -1
            return result_fd
        except BaseException as primary:
            if duplicate_fd >= 0:
                _close_owned_after_error(duplicate_fd, primary, "base directory")
            raise

    def verify_base_fd(self, base_fd: int) -> None:
        """Verify an owned base FD against both its binding and the live chain."""

        try:
            _assert_bound_directory(base_fd, self._base_bindings[-1], "base directory")
            with self._open_bound_base_chain() as directory_fds:
                if _directory_identity(os.fstat(directory_fds[-1])) != _directory_identity(
                    os.fstat(base_fd)
                ):
                    raise UnsafeTaskPathError("unsafe base directory: directory identity changed")
        except UnsafeTaskPathError:
            raise
        except OSError as error:
            raise UnsafeTaskPathError(
                "unsafe base directory: directory identity changed"
            ) from error

    def open_tasks_fd(self) -> int:
        """Return an owned FD for the bound ``tasks`` directory."""

        duplicate_fd = -1
        try:
            with self._open_bound_base_chain() as directory_fds:
                base_fd = directory_fds[-1]
                tasks_fd = self._open_bound_tasks(base_fd)
                directory_fds.append(tasks_fd)
                duplicate_fd = _retry_eintr(lambda: os.dup(tasks_fd))
                _assert_private_directory_entry(
                    base_fd,
                    "tasks",
                    duplicate_fd,
                    self._tasks_identity,
                    "tasks directory",
                )
                self._assert_base_chain(directory_fds)
            result_fd = duplicate_fd
            duplicate_fd = -1
            return result_fd
        except BaseException as primary:
            if duplicate_fd >= 0:
                _close_owned_after_error(duplicate_fd, primary, "tasks directory")
            raise

    def iter_task_entries(
        self,
        *,
        batch_size: int = 128,
    ) -> Iterator[tuple[TaskDirectoryEntry, ...]]:
        """Yield bounded snapshots of direct children below the bound tasks root."""

        if type(batch_size) is not int or not 1 <= batch_size <= MAX_TASK_SCAN_BATCH:
            raise ValueError(f"batch_size must be between 1 and {MAX_TASK_SCAN_BATCH}")
        tasks_fd = self.open_tasks_fd()
        batch: list[TaskDirectoryEntry] = []
        try:
            with os.scandir(tasks_fd) as entries:
                for entry in entries:
                    name = entry.name
                    if type(name) is not str:
                        continue
                    try:
                        encoded = name.encode("utf-8", "strict")
                    except UnicodeError:
                        continue
                    if not encoded or len(encoded) > 255 or name in {".", ".."}:
                        continue
                    try:
                        entry_stat = _retry_eintr(
                            partial(
                                os.stat,
                                name,
                                dir_fd=tasks_fd,
                                follow_symlinks=False,
                            )
                        )
                    except FileNotFoundError:
                        continue
                    batch.append(
                        TaskDirectoryEntry(
                            name=name,
                            identity=_directory_identity(entry_stat),
                            mode=entry_stat.st_mode,
                        )
                    )
                    if len(batch) == batch_size:
                        yield tuple(batch)
                        batch.clear()
                if batch:
                    yield tuple(batch)
        finally:
            close_error = _close_once(tasks_fd)
            if close_error is not None:
                raise UnsafeTaskPathError(
                    "unsafe tasks directory: could not close directory"
                ) from close_error

    def task_identity(self, task_id: str) -> tuple[int, int] | None:
        """Return the current direct task-directory identity without following links."""

        task_id = _validated_task_id(task_id)
        tasks_fd = self.open_tasks_fd()
        try:
            try:
                task_stat = _retry_eintr(
                    lambda: os.stat(task_id, dir_fd=tasks_fd, follow_symlinks=False)
                )
            except FileNotFoundError:
                return None
            _validate_directory_candidate(task_stat, "task directory")
            return _directory_identity(task_stat)
        finally:
            close_error = _close_once(tasks_fd)
            if close_error is not None:
                raise UnsafeTaskPathError(
                    "unsafe tasks directory: could not close directory"
                ) from close_error

    def remove_task_tree(
        self,
        task_id: str,
        *,
        expected_identity: tuple[int, int] | None = None,
        allow_unsafe_entries: bool = False,
    ) -> bool:
        task_id = _validated_task_id(task_id)
        return self._remove_managed_tree(
            task_id,
            expected_identity=expected_identity,
            label="task directory",
            allow_unsafe_entries=allow_unsafe_entries,
        )

    def remove_staging_tree(
        self,
        name: str,
        *,
        expected_identity: tuple[int, int],
    ) -> bool:
        name = _validated_staging_name(name)
        return self._remove_managed_tree(
            name,
            expected_identity=expected_identity,
            label="task staging directory",
        )

    def _remove_managed_tree(
        self,
        name: str,
        *,
        expected_identity: tuple[int, int] | None,
        label: str,
        allow_unsafe_entries: bool = False,
    ) -> bool:
        tasks_fd = self.open_tasks_fd()
        source_fd: int | None = None
        quarantine_fd: int | None = None
        quarantine_name: str | None = None
        try:
            try:
                source_fd, source_stat = self._open_existing_directory(
                    tasks_fd,
                    name,
                    label,
                )
            except FileNotFoundError:
                return False
            _secure_opened_directory(source_fd, source_stat, label, created=False)
            source_stat = _retry_eintr(lambda: os.fstat(source_fd))
            identity = _directory_identity(source_stat)
            if expected_identity is not None and identity != expected_identity:
                raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")

            quarantine_name = f".quarantine-{uuid.uuid4().hex}"
            quarantine_target = quarantine_name
            try:
                _retry_eintr(
                    lambda: os.rename(
                        name,
                        quarantine_target,
                        src_dir_fd=tasks_fd,
                        dst_dir_fd=tasks_fd,
                    )
                )
            except OSError as error:
                raise UnsafeTaskPathError(f"unsafe {label}: quarantine failed") from error

            try:
                quarantine_fd, quarantine_stat = self._open_existing_directory(
                    tasks_fd,
                    quarantine_name,
                    f"{label} quarantine",
                )
            except BaseException:
                _restore_quarantine_entry(
                    tasks_fd,
                    quarantine_name,
                    name,
                    None,
                )
                raise
            quarantine_identity = _directory_identity(quarantine_stat)
            if quarantine_identity != identity:
                _restore_quarantine_entry(
                    tasks_fd,
                    quarantine_name,
                    name,
                    quarantine_identity,
                )
                raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")

            _retry_eintr(lambda: os.fsync(tasks_fd))
            _delete_directory_contents(
                quarantine_fd,
                _TreeDeleteBudget(),
                depth=0,
                allow_unsafe_entries=allow_unsafe_entries,
            )
            _assert_private_directory_entry(
                tasks_fd,
                quarantine_name,
                quarantine_fd,
                identity,
                f"{label} quarantine",
            )
            removal_target = quarantine_name
            _retry_eintr(lambda: os.rmdir(removal_target, dir_fd=tasks_fd))
            quarantine_name = None
            _retry_eintr(lambda: os.fsync(tasks_fd))
            return True
        except UnsafeTaskPathError:
            raise
        except OSError as error:
            raise UnsafeTaskPathError(f"unsafe {label}: removal failed") from error
        finally:
            close_errors = [
                error
                for error in (
                    _close_once(quarantine_fd) if quarantine_fd is not None else None,
                    _close_once(source_fd) if source_fd is not None else None,
                    _close_once(tasks_fd),
                )
                if error is not None
            ]
            if close_errors and quarantine_name is None:
                raise UnsafeTaskPathError(
                    f"unsafe {label}: could not close managed directory"
                ) from close_errors[0]

    def open_task_fd(self, task_id: str, *, create: bool = False) -> int:
        """Return an owned FD for one validated task directory."""

        task_id = _validated_task_id(task_id)
        duplicate_fd = -1
        try:
            with self._open_bound_base_chain() as directory_fds:
                base_fd = directory_fds[-1]
                tasks_fd = self._open_bound_tasks(base_fd)
                directory_fds.append(tasks_fd)
                if create:
                    task_fd, task_identity = self._open_or_create_private_directory(
                        tasks_fd,
                        task_id,
                        "task directory",
                    )
                else:
                    task_fd, task_stat = self._open_existing_directory(
                        tasks_fd,
                        task_id,
                        "task directory",
                    )
                    task_identity = _directory_identity(task_stat)
                    _validate_exact_private_directory(task_stat, "task directory")
                directory_fds.append(task_fd)
                _assert_private_directory_entry(
                    tasks_fd,
                    task_id,
                    task_fd,
                    task_identity,
                    "task directory",
                )
                duplicate_fd = _retry_eintr(lambda: os.dup(task_fd))
                _assert_private_directory_entry(
                    tasks_fd,
                    task_id,
                    duplicate_fd,
                    task_identity,
                    "task directory",
                )
                _assert_private_directory_entry(
                    base_fd,
                    "tasks",
                    tasks_fd,
                    self._tasks_identity,
                    "tasks directory",
                )
                self._assert_base_chain(directory_fds)
            result_fd = duplicate_fd
            duplicate_fd = -1
            return result_fd
        except BaseException as primary:
            if duplicate_fd >= 0:
                _close_owned_after_error(duplicate_fd, primary, "task directory")
            raise

    def open_task_capability(
        self,
        task_id: str,
        *,
        create: bool = False,
        with_images: bool = False,
    ) -> TaskDirectoryCapability:
        """Bind one task (and optionally images) for a complete operation."""

        task_id = _validated_task_id(task_id)
        tasks_fd = self.open_tasks_fd()
        task_fd: int | None = None
        images_fd: int | None = None
        try:
            if create:
                task_fd, task_identity = self._open_or_create_private_directory(
                    tasks_fd,
                    task_id,
                    "task directory",
                )
            else:
                task_fd, task_stat = self._open_existing_directory(
                    tasks_fd,
                    task_id,
                    "task directory",
                )
                _secure_opened_directory(task_fd, task_stat, "task directory", created=False)
                task_identity = _directory_identity(os.fstat(task_fd))
            images_identity: tuple[int, int] | None = None
            if with_images:
                images_fd, images_identity = self._open_or_create_private_directory(
                    task_fd,
                    "images",
                    "images directory",
                )
            capability = TaskDirectoryCapability(
                layout=self,
                task_id=task_id,
                entry_name=task_id,
                tasks_fd=tasks_fd,
                task_fd=task_fd,
                task_identity=task_identity,
                images_fd=images_fd,
                images_identity=images_identity,
            )
            capability.verify()
            tasks_fd = -1
            task_fd = None
            images_fd = None
            return capability
        except BaseException as primary:
            for owned_fd, label in (
                (images_fd, "images directory"),
                (task_fd, "task directory"),
                (tasks_fd if tasks_fd >= 0 else None, "tasks directory"),
            ):
                if owned_fd is not None:
                    _close_owned_after_error(owned_fd, primary, label)
            raise

    def create_task_staging_capability(self, task_id: str) -> TaskDirectoryCapability:
        task_id = _validated_task_id(task_id)
        tasks_fd = self.open_tasks_fd()
        staging_fd: int | None = None
        try:
            for _attempt in range(16):
                nonce = uuid.uuid4().hex
                prefix = task_id
                candidate = f".{prefix}.{nonce}.tmp"
                if len(candidate.encode("utf-8")) > MAX_MANAGED_NAME_BYTES:
                    prefix = uuid.uuid5(uuid.NAMESPACE_OID, task_id).hex
                    candidate = f".{prefix}.{nonce}.tmp"
                try:
                    staging_fd, identity = self._open_or_create_private_directory(
                        tasks_fd,
                        candidate,
                        "task staging directory",
                    )
                except UnsafeTaskPathError as error:
                    if isinstance(error.__cause__, FileExistsError):
                        continue
                    raise
                capability = TaskDirectoryCapability(
                    layout=self,
                    task_id=task_id,
                    entry_name=candidate,
                    tasks_fd=tasks_fd,
                    task_fd=staging_fd,
                    task_identity=identity,
                )
                capability.verify()
                tasks_fd = -1
                staging_fd = None
                return capability
            raise UnsafeTaskPathError("unsafe task staging directory: name allocation failed")
        except BaseException as primary:
            if staging_fd is not None:
                _close_owned_after_error(staging_fd, primary, "task staging directory")
            if tasks_fd >= 0:
                _close_owned_after_error(tasks_fd, primary, "tasks directory")
            raise

    def open_task_staging_capability(
        self,
        name: str,
        task_id: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> TaskDirectoryCapability:
        name = _validated_staging_name(name)
        task_id = _validated_task_id(task_id)
        tasks_fd = self.open_tasks_fd()
        staging_fd: int | None = None
        try:
            staging_fd, staging_stat = self._open_existing_directory(
                tasks_fd,
                name,
                "task staging directory",
            )
            _secure_opened_directory(
                staging_fd,
                staging_stat,
                "task staging directory",
                created=False,
            )
            identity = _directory_identity(os.fstat(staging_fd))
            if expected_identity is not None and identity != expected_identity:
                raise UnsafeTaskPathError(
                    "unsafe task staging directory: directory identity changed"
                )
            capability = TaskDirectoryCapability(
                layout=self,
                task_id=task_id,
                entry_name=name,
                tasks_fd=tasks_fd,
                task_fd=staging_fd,
                task_identity=identity,
            )
            capability.verify()
            tasks_fd = -1
            staging_fd = None
            return capability
        except BaseException as primary:
            if staging_fd is not None:
                _close_owned_after_error(staging_fd, primary, "task staging directory")
            if tasks_fd >= 0:
                _close_owned_after_error(tasks_fd, primary, "tasks directory")
            raise

    def publish_task_staging(self, capability: TaskDirectoryCapability) -> None:
        if capability.layout is not self or capability.entry_name == capability.task_id:
            raise UnsafeTaskPathError("unsafe task staging directory: invalid capability")
        capability.verify()
        try:
            os.stat(
                capability.task_id,
                dir_fd=capability.tasks_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise UnsafeTaskPathError("unsafe cache publication target already exists")
        staging_name = capability.entry_name
        _retry_eintr(
            lambda: os.rename(
                staging_name,
                capability.task_id,
                src_dir_fd=capability.tasks_fd,
                dst_dir_fd=capability.tasks_fd,
            )
        )
        capability.entry_name = capability.task_id
        _retry_eintr(lambda: os.fsync(capability.tasks_fd))
        capability.verify()

    def tasks_dir(self) -> str:
        tasks_fd = self.open_tasks_fd()
        close_error = _close_once(tasks_fd)
        if close_error is not None:
            raise UnsafeTaskPathError(
                "unsafe tasks directory: could not close directory"
            ) from close_error
        return str(Path(self.base_dir) / "tasks")

    def task_path(self, task_id: str) -> str:
        task_id = _validated_task_id(task_id)
        return str(Path(self.tasks_dir()) / task_id)

    def task_dir(self, task_id: str) -> str:
        task_id = _validated_task_id(task_id)
        with self._open_bound_base_chain() as directory_fds:
            base_fd = directory_fds[-1]
            tasks_fd = self._open_bound_tasks(base_fd)
            directory_fds.append(tasks_fd)
            task_fd, task_identity = self._open_or_create_private_directory(
                tasks_fd,
                task_id,
                "task directory",
            )
            directory_fds.append(task_fd)
            _assert_private_directory_entry(
                tasks_fd,
                task_id,
                task_fd,
                task_identity,
                "task directory",
            )
            _assert_private_directory_entry(
                base_fd,
                "tasks",
                tasks_fd,
                self._tasks_identity,
                "tasks directory",
            )
            self._assert_base_chain(directory_fds)
        return str(Path(self.base_dir) / "tasks" / task_id)

    def section_raw_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.raw.md")

    def section_ai_path(self, task_id: str, seq: int) -> str:
        return str(Path(self.task_dir(task_id)) / f"{seq}.ai.md")

    def merged_path(self, task_id: str) -> str:
        return str(Path(self.task_dir(task_id)) / "merged.md")

    def images_dir(self, task_id: str) -> str:
        task_id = _validated_task_id(task_id)
        with self._open_bound_base_chain() as directory_fds:
            base_fd = directory_fds[-1]
            tasks_fd = self._open_bound_tasks(base_fd)
            directory_fds.append(tasks_fd)
            task_fd, task_identity = self._open_or_create_private_directory(
                tasks_fd,
                task_id,
                "task directory",
            )
            directory_fds.append(task_fd)
            images_fd, images_identity = self._open_or_create_private_directory(
                task_fd,
                "images",
                "images directory",
            )
            directory_fds.append(images_fd)
            _assert_private_directory_entry(
                task_fd,
                "images",
                images_fd,
                images_identity,
                "images directory",
            )
            _assert_private_directory_entry(
                tasks_fd,
                task_id,
                task_fd,
                task_identity,
                "task directory",
            )
            _assert_private_directory_entry(
                base_fd,
                "tasks",
                tasks_fd,
                self._tasks_identity,
                "tasks directory",
            )
            self._assert_base_chain(directory_fds)
        return str(Path(self.base_dir) / "tasks" / task_id / "images")


def _canonical_base_directory(base_dir: str | os.PathLike[str]) -> str:
    try:
        raw_path = os.fspath(base_dir)
    except TypeError as error:
        raise UnsafeTaskPathError("unsafe base directory: invalid path") from error
    if not isinstance(raw_path, str):
        raise UnsafeTaskPathError("unsafe base directory: invalid path")
    try:
        path = bytes.decode(str.encode(raw_path, "utf-8", "strict"), "utf-8", "strict")
    except UnicodeError as error:
        raise UnsafeTaskPathError("unsafe base directory: invalid path") from error
    if (
        not path
        or not os.path.isabs(path)
        or path.startswith("//")
        or os.path.normpath(path) != path
        or any(not character.isprintable() for character in path)
    ):
        raise UnsafeTaskPathError("unsafe base directory: invalid path")
    return path


def _absolute_components(path: str) -> tuple[str, ...]:
    parts = Path(path).parts
    if len(parts) < 2 or parts[0] != os.sep:
        raise UnsafeTaskPathError("unsafe base directory: invalid path")
    return tuple(parts[1:])


def _validated_task_id(task_id: object) -> str:
    if not isinstance(task_id, str):
        raise UnsafeTaskPathError("task ID must be a safe single path component")
    try:
        task_id = bytes.decode(str.encode(task_id, "utf-8", "strict"), "utf-8", "strict")
        encoded = task_id.encode("utf-8")
    except UnicodeError as error:
        raise UnsafeTaskPathError("task ID must be a safe single path component") from error
    if (
        not task_id
        or task_id in {".", ".."}
        or task_id.startswith(".")
        or len(encoded) > 255
        or "\x00" in task_id
        or "/" in task_id
        or "\\" in task_id
    ):
        raise UnsafeTaskPathError("task ID must be a safe single path component")
    return task_id


def _validated_staging_name(name: object) -> str:
    if type(name) is not str:
        raise UnsafeTaskPathError("task staging name must be a safe path component")
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeError as error:
        raise UnsafeTaskPathError("task staging name must be a safe path component") from error
    if (
        not name.startswith(".")
        or not name.endswith(".tmp")
        or len(encoded) > MAX_MANAGED_NAME_BYTES
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise UnsafeTaskPathError("task staging name must be a safe path component")
    task_id, separator, nonce = name[1:-4].rpartition(".")
    if not separator or not nonce or len(nonce.encode("utf-8")) > 64:
        raise UnsafeTaskPathError("task staging name must be a safe path component")
    _validated_task_id(task_id)
    return name


def _restore_quarantine_entry(
    parent_fd: int,
    quarantine_name: str,
    original_name: str,
    quarantine_identity: tuple[int, int] | None,
) -> None:
    try:
        os.stat(original_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        return
    else:
        return
    if quarantine_identity is not None:
        try:
            current = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return
        if _directory_identity(current) != quarantine_identity:
            return
    try:
        _retry_eintr(
            lambda: os.rename(
                quarantine_name,
                original_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        )
    except OSError:
        return


def _validated_tree_entry_name(name: object) -> str:
    if type(name) is not str or name in {"", ".", ".."}:
        raise UnsafeTaskPathError("unsafe tree entry: invalid name")
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeError as error:
        raise UnsafeTaskPathError("unsafe tree entry: invalid name") from error
    if len(encoded) > MAX_MANAGED_NAME_BYTES or "/" in name or "\\" in name or "\x00" in name:
        raise UnsafeTaskPathError("unsafe tree entry: invalid name")
    return name


def _delete_directory_contents(
    directory_fd: int,
    budget: _TreeDeleteBudget,
    *,
    depth: int,
    allow_unsafe_entries: bool = False,
) -> None:
    if depth > MAX_TREE_DELETE_DEPTH:
        raise UnsafeTaskPathError("unsafe tree entry: depth limit exceeded")
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            name = _validated_tree_entry_name(entry.name)
            budget.consume()
            try:
                entry_stat = _retry_eintr(
                    partial(
                        os.stat,
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                )
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry_stat.st_mode):
                if not allow_unsafe_entries:
                    raise UnsafeTaskPathError("unsafe tree entry: symbolic link")
                _delete_leaf_entry(directory_fd, name, entry_stat)
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                _delete_child_directory(
                    directory_fd,
                    name,
                    entry_stat,
                    budget,
                    depth=depth + 1,
                    allow_unsafe_entries=allow_unsafe_entries,
                )
                continue
            if stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_nlink != 1 and not allow_unsafe_entries:
                    raise UnsafeTaskPathError("unsafe tree entry: hard link")
                _delete_regular_file(
                    directory_fd,
                    name,
                    entry_stat,
                    allow_multiple_links=allow_unsafe_entries,
                )
                continue
            if not allow_unsafe_entries:
                raise UnsafeTaskPathError("unsafe tree entry: special file")
            _delete_leaf_entry(directory_fd, name, entry_stat)


def _delete_child_directory(
    parent_fd: int,
    name: str,
    expected_stat: os.stat_result,
    budget: _TreeDeleteBudget,
    *,
    depth: int,
    allow_unsafe_entries: bool = False,
) -> None:
    _validate_directory_candidate(expected_stat, "tree entry")
    child_fd = FsLayout._open_directory(name, "tree entry", dir_fd=parent_fd)
    isolated_name = f".delete-{uuid.uuid4().hex}"
    isolated = False
    try:
        opened_stat = _retry_eintr(lambda: os.fstat(child_fd))
        if _directory_identity(opened_stat) != _directory_identity(expected_stat):
            raise UnsafeTaskPathError("unsafe tree entry: directory identity changed")
        _retry_eintr(
            lambda: os.rename(
                name,
                isolated_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        )
        isolated = True
        isolated_stat = _retry_eintr(
            lambda: os.stat(isolated_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if _directory_identity(isolated_stat) != _directory_identity(expected_stat):
            _restore_quarantine_entry(
                parent_fd,
                isolated_name,
                name,
                _directory_identity(isolated_stat),
            )
            isolated = False
            raise UnsafeTaskPathError("unsafe tree entry: directory identity changed")
        _delete_directory_contents(
            child_fd,
            budget,
            depth=depth,
            allow_unsafe_entries=allow_unsafe_entries,
        )
        current = _retry_eintr(
            lambda: os.stat(isolated_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if _directory_identity(current) != _directory_identity(expected_stat):
            raise UnsafeTaskPathError("unsafe tree entry: directory identity changed")
        _retry_eintr(lambda: os.rmdir(isolated_name, dir_fd=parent_fd))
        isolated = False
    except OSError as error:
        raise UnsafeTaskPathError("unsafe tree entry: directory removal failed") from error
    finally:
        close_error = _close_once(child_fd)
        if close_error is not None and not isolated:
            raise UnsafeTaskPathError("unsafe tree entry: directory close failed") from close_error


def _delete_regular_file(
    parent_fd: int,
    name: str,
    expected_stat: os.stat_result,
    *,
    allow_multiple_links: bool = False,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    file_fd = -1
    isolated_name = f".delete-{uuid.uuid4().hex}"
    isolated = False
    try:
        file_fd = _retry_eintr(lambda: os.open(name, flags, dir_fd=parent_fd))
        opened_stat = _retry_eintr(lambda: os.fstat(file_fd))
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or (opened_stat.st_nlink != 1 and not allow_multiple_links)
            or _directory_identity(opened_stat) != _directory_identity(expected_stat)
        ):
            raise UnsafeTaskPathError("unsafe tree entry: file identity changed")
        _retry_eintr(
            lambda: os.rename(
                name,
                isolated_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        )
        isolated = True
        isolated_stat = _retry_eintr(
            lambda: os.stat(isolated_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if _directory_identity(isolated_stat) != _directory_identity(expected_stat) or (
            isolated_stat.st_nlink != 1 and not allow_multiple_links
        ):
            _restore_quarantine_entry(
                parent_fd,
                isolated_name,
                name,
                _directory_identity(isolated_stat),
            )
            isolated = False
            raise UnsafeTaskPathError("unsafe tree entry: file identity changed")
        _retry_eintr(lambda: os.unlink(isolated_name, dir_fd=parent_fd))
        isolated = False
    except OSError as error:
        raise UnsafeTaskPathError("unsafe tree entry: file removal failed") from error
    finally:
        if file_fd >= 0:
            close_error = _close_once(file_fd)
            if close_error is not None and not isolated:
                raise UnsafeTaskPathError("unsafe tree entry: file close failed") from close_error


def _delete_leaf_entry(
    parent_fd: int,
    name: str,
    expected_stat: os.stat_result,
) -> None:
    """安全删除一个非目录条目（符号链接、FIFO 等特殊文件）。

    仅移除条目本身，绝不跟随引用目标。先原子重命名到隔离名，
    再用 lstat 校验身份未被替换，最后 unlink。
    """

    isolated_name = f".delete-{uuid.uuid4().hex}"
    try:
        _retry_eintr(
            lambda: os.rename(
                name,
                isolated_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        )
        isolated_stat = _retry_eintr(
            lambda: os.stat(isolated_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if stat.S_ISDIR(isolated_stat.st_mode) or _directory_identity(
            isolated_stat
        ) != _directory_identity(expected_stat):
            _restore_quarantine_entry(
                parent_fd,
                isolated_name,
                name,
                _directory_identity(isolated_stat),
            )
            raise UnsafeTaskPathError("unsafe tree entry: leaf identity changed")
        _retry_eintr(lambda: os.unlink(isolated_name, dir_fd=parent_fd))
    except OSError as error:
        raise UnsafeTaskPathError("unsafe tree entry: leaf removal failed") from error


def _binding(name: str, directory_stat: os.stat_result, policy: str) -> _DirectoryBinding:
    return _DirectoryBinding(
        name=name,
        identity=_directory_identity(directory_stat),
        owner=directory_stat.st_uid,
        mode=stat.S_IMODE(directory_stat.st_mode),
        policy=policy,
    )


def _make_created_entry_accessible(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, int]:
    before = _stat_directory_entry(parent_fd, name, label)
    _validate_directory_owner_type(before, label)
    identity = _directory_identity(before)
    try:
        _retry_eintr(
            lambda: os.chmod(
                name,
                0o700,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        )
    except OSError as error:
        raise UnsafeTaskPathError(f"unsafe {label}: could not set private permissions") from error
    after = _stat_directory_entry(parent_fd, name, label)
    _validate_directory_owner_type(after, label)
    if _directory_identity(after) != identity:
        raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")
    return identity


def _stat_directory_entry(parent_fd: int, name: str, label: str) -> os.stat_result:
    try:
        return _retry_eintr(lambda: os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except FileNotFoundError:
        raise
    except OSError as error:
        raise UnsafeTaskPathError(f"unsafe {label}: expected a real directory") from error


def _secure_opened_directory(
    directory_fd: int,
    directory_stat: os.stat_result,
    label: str,
    *,
    created: bool,
) -> None:
    _validate_directory_owner_type(directory_stat, label)
    if not created:
        _validate_directory_candidate(directory_stat, label)
    mode = stat.S_IMODE(directory_stat.st_mode)
    if created or mode != 0o700:
        try:
            _retry_eintr(lambda: os.fchmod(directory_fd, 0o700))
        except OSError as error:
            raise UnsafeTaskPathError(
                f"unsafe {label}: could not set private permissions"
            ) from error
    final_stat = _retry_eintr(lambda: os.fstat(directory_fd))
    _validate_exact_private_directory(final_stat, label)
    if _directory_identity(final_stat) != _directory_identity(directory_stat):
        raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")


def _validate_directory_type(directory_stat: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise UnsafeTaskPathError(f"unsafe {label}: expected a real directory")


def _validate_directory_owner_type(directory_stat: os.stat_result, label: str) -> None:
    _validate_directory_type(directory_stat, label)
    if directory_stat.st_uid != os.getuid():
        raise UnsafeTaskPathError(f"unsafe {label}: unexpected owner")


def _validate_trusted_anchor(directory_stat: os.stat_result, label: str) -> None:
    _validate_directory_owner_type(directory_stat, label)
    mode = stat.S_IMODE(directory_stat.st_mode)
    if mode & 0o022:
        raise UnsafeTaskPathError(f"unsafe {label}: group or world writable anchor")
    if mode & 0o700 != 0o700:
        raise UnsafeTaskPathError(f"unsafe {label}: unusable anchor permissions")


def _validate_directory_candidate(directory_stat: os.stat_result, label: str) -> None:
    _validate_directory_owner_type(directory_stat, label)
    mode = stat.S_IMODE(directory_stat.st_mode)
    if mode & 0o022:
        raise UnsafeTaskPathError(f"unsafe {label}: group or world writable")
    if mode & 0o700 != 0o700:
        raise UnsafeTaskPathError(f"unsafe {label}: permissions cannot be tightened")


def _validate_exact_private_directory(
    directory_stat: os.stat_result,
    label: str,
) -> None:
    _validate_directory_owner_type(directory_stat, label)
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise UnsafeTaskPathError(f"unsafe {label}: expected mode 0700")


def _assert_bound_directory(
    directory_fd: int,
    binding: _DirectoryBinding,
    label: str,
) -> None:
    directory_stat = _retry_eintr(lambda: os.fstat(directory_fd))
    _assert_bound_stat(directory_stat, binding, label)


def _assert_binding_chain(
    directory_fds: list[int] | tuple[int, ...],
    bindings: list[_DirectoryBinding] | tuple[_DirectoryBinding, ...],
    label: str,
) -> None:
    if len(directory_fds) != len(bindings):
        raise UnsafeTaskPathError(f"unsafe {label}: incomplete directory chain")
    for index, binding in enumerate(bindings):
        directory_fd = directory_fds[index]
        _assert_bound_directory(directory_fd, binding, label)
        if index == 0:
            continue
        path_stat = _stat_directory_entry(
            directory_fds[index - 1],
            binding.name,
            label,
        )
        _assert_bound_stat(path_stat, binding, label)


def _assert_bound_stat(
    directory_stat: os.stat_result,
    binding: _DirectoryBinding,
    label: str,
) -> None:
    _validate_directory_type(directory_stat, label)
    if (
        _directory_identity(directory_stat) != binding.identity
        or directory_stat.st_uid != binding.owner
        or stat.S_IMODE(directory_stat.st_mode) != binding.mode
    ):
        raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")
    if binding.policy == _POLICY_PRIVATE:
        _validate_exact_private_directory(directory_stat, label)
    elif binding.policy == _POLICY_ANCHOR:
        _validate_trusted_anchor(directory_stat, label)


def _assert_private_directory_entry(
    parent_fd: int,
    name: str,
    directory_fd: int,
    expected_identity: tuple[int, int],
    label: str,
) -> None:
    path_stat = _stat_directory_entry(parent_fd, name, label)
    opened_stat = _retry_eintr(lambda: os.fstat(directory_fd))
    _validate_exact_private_directory(path_stat, label)
    _validate_exact_private_directory(opened_stat, label)
    if (
        _directory_identity(path_stat) != expected_identity
        or _directory_identity(opened_stat) != expected_identity
    ):
        raise UnsafeTaskPathError(f"unsafe {label}: directory identity changed")


def _directory_identity(directory_stat: os.stat_result) -> tuple[int, int]:
    return directory_stat.st_dev, directory_stat.st_ino


@contextmanager
def _directory_fd_stack(label: str) -> Iterator[list[int]]:
    directory_fds: list[int] = []
    try:
        yield directory_fds
    except BaseException as primary:
        close_error = _close_fd_stack(directory_fds)
        if close_error is not None:
            _add_cleanup_note(primary, close_error, label)
        raise
    else:
        close_error = _close_fd_stack(directory_fds)
        if close_error is not None:
            raise UnsafeTaskPathError(f"unsafe {label}: could not close directory") from close_error


def _close_owned_after_error(
    directory_fd: int,
    primary: BaseException,
    label: str,
) -> None:
    close_error = _close_once(directory_fd)
    if close_error is not None:
        _add_cleanup_note(primary, close_error, label)


def _close_fd_stack(directory_fds: list[int]) -> OSError | None:
    first_error: OSError | None = None
    while directory_fds:
        directory_fd = directory_fds.pop()
        close_error = _close_once(directory_fd)
        if first_error is None:
            first_error = close_error
    return first_error


def _add_cleanup_note(primary: BaseException, cleanup: OSError, label: str) -> None:
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(f"secondary {label} close failure: {cleanup}")


def _close_once(directory_fd: int) -> OSError | None:
    try:
        os.close(directory_fd)
    except OSError as error:
        return error
    return None
