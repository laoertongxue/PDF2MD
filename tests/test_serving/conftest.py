# tests/test_serving/conftest.py
from pathlib import Path

import pytest


@pytest.fixture
def serve_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def sample_md_abs_path():
    return str(Path("tests/fixtures/sample.md").resolve())
