import time

import pytest
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import allowed_cors_origins, build_app, require_loopback_host
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


def make_test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    base = tmp_path / "data"
    base.mkdir()
    db_path = tmp_path / "serve.db"

    def orch_factory():
        sub_dir = base / f"task_{time.time_ns()}"
        sub_dir.mkdir()
        fs = FsLayout(base_dir=str(sub_dir))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    app = build_app(orch_factory=orch_factory, max_global_concurrency=4)
    return TestClient(app)


def test_health_returns_ok(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_serve_rejects_non_loopback_hosts(host):
    with pytest.raises(ValueError, match="loopback"):
        require_loopback_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_accepts_loopback_hosts(host):
    assert require_loopback_host(host) == host


@pytest.mark.parametrize(
    "origin",
    ["*", "https://example.com", "http://192.168.1.10:1420", "null"],
)
def test_cors_rejects_wildcard_and_non_loopback_origins(monkeypatch, origin):
    monkeypatch.setenv("PARSING_CORE_CORS_ORIGINS", origin)
    with pytest.raises(ValueError, match="loopback"):
        allowed_cors_origins()
