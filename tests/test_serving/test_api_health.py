import time

import pytest
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import (
    allowed_cors_origins,
    build_app,
    require_loopback_host,
    run_uvicorn,
)
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"
ALLOWED_ORIGIN = "http://localhost:1420"


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

    app = build_app(
        orch_factory=orch_factory,
        max_global_concurrency=4,
        session_token=TEST_SESSION_TOKEN,
    )
    return TestClient(app)


def test_health_returns_ok(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/health", headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_requires_matching_session_without_origin(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    missing = client.get("/health")
    wrong = client.get("/health", headers={"X-PDF2MD-Session": "wrong"})
    response = client.get("/health", headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN})

    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "session_required"
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "session_required"
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_wrong_origin_even_with_session(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get(
        "/health",
        headers={
            "Origin": "https://attacker.example",
            "X-PDF2MD-Session": TEST_SESSION_TOKEN,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_forbidden"


def test_all_business_routes_require_session_token(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get("/api/workbench/courses", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "session_required"


def test_wrong_origin_is_rejected(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get(
        "/api/workbench/courses",
        headers={"Origin": "https://attacker.example", "X-PDF2MD-Session": TEST_SESSION_TOKEN},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_forbidden"


def test_business_api_rejects_missing_origin_even_with_session(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get(
        "/api/workbench/courses",
        headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_forbidden"


def test_cors_preflight_allows_session_header(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.options(
        "/api/workbench/courses",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-PDF2MD-Session",
        },
    )

    assert response.status_code == 200
    assert "x-pdf2md-session" in response.headers["access-control-allow-headers"].lower()


def test_cors_preflight_rejects_wrong_origin_with_stable_code(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.options(
        "/api/workbench/courses",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-PDF2MD-Session",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "origin_forbidden"}}


def test_session_failure_remains_readable_to_allowed_browser_origin(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get("/api/workbench/courses", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.json() == {"detail": {"code": "session_required"}}


def test_uvicorn_uses_inherited_socket_instead_of_rebinding(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    app = object()

    run_uvicorn(app, host="127.0.0.1", port=8000, socket_fd=17)

    assert calls == [(app, {"fd": 17})]


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
