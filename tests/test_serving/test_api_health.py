import asyncio
import threading
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


def make_test_app(tmp_path, monkeypatch, *, shutdown_request=None):
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
        shutdown_request=shutdown_request,
    )
    return TestClient(app)


def test_health_returns_ok(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/health", headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_shutdown_requires_current_session_and_acknowledges_once(tmp_path, monkeypatch):
    requests = []
    client = make_test_app(
        tmp_path,
        monkeypatch,
        shutdown_request=lambda: requests.append("shutdown"),
    )

    missing = client.post("/shutdown")
    wrong = client.post("/shutdown", headers={"X-PDF2MD-Session": "wrong-session-token"})
    valid = client.post("/shutdown", headers={"X-PDF2MD-Session": TEST_SESSION_TOKEN})

    assert missing.status_code == 401
    assert missing.json() == {"detail": {"code": "session_required"}}
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": {"code": "session_required"}}
    assert valid.status_code == 200
    assert valid.json() == {"status": "shutting_down"}
    assert requests == ["shutdown"]


@pytest.mark.asyncio
async def test_lifespan_shutdown_drains_scheduler_thread_before_external_hook(tmp_path):
    loop = asyncio.get_running_loop()
    parse_started = asyncio.Event()
    release_parse = threading.Event()
    thread_finished = threading.Event()
    events: list[str] = []

    class Repo:
        def __init__(self):
            self.batches = {}
            self.tasks = {}

        def create_batch_with_tasks(self, batch, tasks):
            self.batches[batch["id"]] = dict(batch)
            self.tasks.update({task.id: task for task in tasks})

        def set_batch_progress(self, batch_id, completed, status=None):
            self.batches[batch_id]["completed_tasks"] = completed
            if status is not None:
                self.batches[batch_id]["status"] = status

        def update_task_status(self, task_id, status, error_msg=None):
            self.tasks[task_id].status = status
            self.tasks[task_id].error_msg = error_msg

        def get_batch(self, batch_id):
            return self.batches.get(batch_id)

        def list_batches_by_status(self, status):
            return [batch for batch in self.batches.values() if batch["status"] == status]

        def list_all_batches(self):
            return list(self.batches.values())

        def list_all_tasks(self):
            return list(self.tasks.values())

    repo = Repo()

    class Orch:
        def __init__(self):
            self.repo = repo
            self.on_progress = None

        def parse_file(self, *_args):
            loop.call_soon_threadsafe(parse_started.set)
            release_parse.wait(timeout=5)
            thread_finished.set()

    app = build_app(
        orch_factory=Orch,
        session_token=TEST_SESSION_TOKEN,
        shutdown_hook=lambda: events.append("external-hook"),
    )

    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    scheduler = app.state.scheduler
    response = await scheduler.submit_batch(files=["running.pdf"], concurrency=1)
    await asyncio.wait_for(parse_started.wait(), timeout=1)

    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await asyncio.sleep(0.05)
    try:
        assert not shutdown.done()
        assert not thread_finished.is_set()
        assert not scheduler._batches[response.batch_id].done.is_set()
        assert repo.batches[response.batch_id]["status"] == "RUNNING"
        assert events == []
    finally:
        release_parse.set()

    await asyncio.wait_for(shutdown, timeout=1)
    assert repo.batches[response.batch_id]["status"] == "CANCELLED"
    assert events == ["external-hook"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_api_docs_are_disabled(tmp_path, monkeypatch, path):
    client = make_test_app(tmp_path, monkeypatch)

    response = client.get(path)

    assert response.status_code == 404


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
