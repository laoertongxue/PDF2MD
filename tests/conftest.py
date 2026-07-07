# tests/conftest.py
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
