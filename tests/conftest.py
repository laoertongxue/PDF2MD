# tests/conftest.py
import functools
import inspect
import sqlite3
from pathlib import Path

import pytest

_TRANSIENT_SQLITE_MESSAGE = "locking protocol"
_RETRY_SCOPE = "tests/test_serving/test_scheduler.py::"


def _retry_transient_sqlite(original):
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapper(*args, **kwargs):
            for attempt in range(2):
                try:
                    return await original(*args, **kwargs)
                except sqlite3.OperationalError as error:
                    if attempt or str(error) != _TRANSIENT_SQLITE_MESSAGE:
                        raise

        return async_wrapper

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        for attempt in range(2):
            try:
                return original(*args, **kwargs)
            except sqlite3.OperationalError as error:
                if attempt or str(error) != _TRANSIENT_SQLITE_MESSAGE:
                    raise

    return wrapper


def pytest_collection_modifyitems(items):
    for item in items:
        if not item.nodeid.startswith(_RETRY_SCOPE):
            continue
        original = getattr(item, "obj", None)
        if callable(original):
            item.obj = _retry_transient_sqlite(original)


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
