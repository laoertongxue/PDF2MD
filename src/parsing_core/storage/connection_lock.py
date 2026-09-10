from __future__ import annotations

import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Concatenate, Protocol, cast
from uuid import uuid4


class _LockedRepository(Protocol):
    _connection_lock: threading.RLock


class _AtomicRepository(_LockedRepository, Protocol):
    conn: sqlite3.Connection


class _ConnectionFinalizer(Protocol):
    @property
    def alive(self) -> bool: ...

    def __call__(self, _info: object = None) -> object | None: ...


type _SqliteValue = str | bytes | int | float | None


@dataclass
class _ConnectionLockEntry:
    connection: sqlite3.Connection
    lock: threading.RLock
    users: int


_connection_locks_guard = threading.RLock()
_connection_locks: dict[int, _ConnectionLockEntry] = {}


def register_connection_lock(
    owner: object,
    conn: sqlite3.Connection,
) -> tuple[threading.RLock, _ConnectionFinalizer]:
    key = id(conn)
    with _connection_locks_guard:
        entry = _connection_locks.get(key)
        if entry is None:
            entry = _ConnectionLockEntry(conn, threading.RLock(), 0)
            _connection_locks[key] = entry
        elif entry.connection is not conn:
            raise RuntimeError("sqlite connection identity collision")
        entry.users += 1

    finalizer = weakref.finalize(owner, _unregister_connection_lock, key)
    return entry.lock, cast(_ConnectionFinalizer, finalizer)


def _unregister_connection_lock(key: int) -> None:
    with _connection_locks_guard:
        entry = _connection_locks.get(key)
        if entry is None:
            return
        entry.users -= 1
        if entry.users == 0:
            # sqlite3.Connection cannot be weak-referenced. Repository finalizers
            # bound the registry to connections that still have live repositories.
            del _connection_locks[key]


def lock_repository_methods[C](cls: type[C]) -> type[C]:
    for name, method in vars(cls).items():
        if name.startswith("_") or not callable(method):
            continue
        setattr(cls, name, _locked_method(cast(Callable[..., object], method)))
    return cls


def atomic_repository_methods[C](
    method_names: tuple[str, ...],
) -> Callable[[type[C]], type[C]]:
    def decorate(cls: type[C]) -> type[C]:
        for name in method_names:
            method = cast(Callable[..., object], getattr(cls, name))
            setattr(cls, name, _atomic_method(method))
        return cls

    return decorate


@contextmanager
def atomic_connection(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    *,
    immediate: bool = False,
    nested_write: tuple[str, tuple[_SqliteValue, ...]] | None = None,
) -> Iterator[None]:
    with lock:
        nested = conn.in_transaction
        savepoint = f"repo_{uuid4().hex}"
        if nested:
            conn.execute(f"SAVEPOINT {savepoint}")
        else:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")

        try:
            if nested and immediate:
                if nested_write is None:
                    raise ValueError("nested immediate transaction requires a write-lock statement")
                conn.execute(*nested_write)
            yield
            if nested:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                conn.commit()
        except BaseException as error:
            try:
                if nested:
                    _rollback_savepoint(conn, savepoint)
                else:
                    conn.rollback()
            except BaseException as cleanup_error:
                error.add_note(f"transaction cleanup failed: {cleanup_error!r}")
                raise error from cleanup_error
            raise


def _rollback_savepoint(conn: sqlite3.Connection, savepoint: str) -> None:
    errors: list[BaseException] = []
    try:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
    except BaseException as error:
        errors.append(error)
    try:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException as error:
        errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("savepoint cleanup failed", errors)


def _locked_method[**P, R](
    method: Callable[Concatenate[_LockedRepository, P], R],
) -> Callable[Concatenate[_LockedRepository, P], R]:
    @wraps(method)
    def locked(self: _LockedRepository, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._connection_lock:
            return method(self, *args, **kwargs)

    return locked


def _atomic_method[**P, R](
    method: Callable[Concatenate[_AtomicRepository, P], R],
) -> Callable[Concatenate[_AtomicRepository, P], R]:
    @wraps(method)
    def atomic(self: _AtomicRepository, /, *args: P.args, **kwargs: P.kwargs) -> R:
        immediate = not self.conn.in_transaction
        with atomic_connection(self.conn, self._connection_lock, immediate=immediate):
            return method(self, *args, **kwargs)

    return atomic
