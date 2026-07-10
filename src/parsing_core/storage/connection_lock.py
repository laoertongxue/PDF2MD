import sqlite3
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from uuid import uuid4


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
) -> tuple[threading.RLock, weakref.finalize]:
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
    return entry.lock, finalizer


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


def lock_repository_methods(cls):
    for name, method in vars(cls).items():
        if name.startswith("_") or not callable(method):
            continue
        setattr(cls, name, _locked_method(method))
    return cls


def atomic_repository_methods(method_names: tuple[str, ...]):
    def decorate(cls):
        for name in method_names:
            setattr(cls, name, _atomic_method(getattr(cls, name)))
        return cls

    return decorate


@contextmanager
def atomic_connection(
    conn: sqlite3.Connection,
    lock: threading.RLock,
    *,
    immediate: bool = False,
    nested_write: tuple[str, tuple] | None = None,
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
    errors = []
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


def _locked_method(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._connection_lock:
            return method(self, *args, **kwargs)

    return locked


def _atomic_method(method):
    @wraps(method)
    def atomic(self, *args, **kwargs):
        with atomic_connection(self.conn, self._connection_lock):
            return method(self, *args, **kwargs)

    return atomic
