import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Task
from parsing_core.orchestrator import Orchestrator
from parsing_core.parser.markitdown_adapter import MarkItDownAdapter
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"
AUTH_HEADERS = {"Origin": "http://localhost:1420", "X-PDF2MD-Session": TEST_SESSION_TOKEN}


def make_test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    base = tmp_path / "data"
    base.mkdir()
    db_path = tmp_path / "serve.db"

    def orch_factory():
        fs = FsLayout(base_dir=str(base))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    return TestClient(
        build_app(
            orch_factory=orch_factory,
            max_global_concurrency=4,
            session_token=TEST_SESSION_TOKEN,
        ),
        headers=AUTH_HEADERS,
    )


def test_create_single_task_auto_batch(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/tasks", json={"file_path": sample})
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"]
    assert len(body["task_ids"]) == 1


def test_get_task_status(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = client.post("/api/tasks", json={"file_path": sample})
    task_id = r1.json()["task_ids"][0]
    time.sleep(2)
    r2 = client.get(f"/api/tasks/{task_id}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["task_id"] == task_id


def test_get_task_not_found(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/api/tasks/nope")
    assert r.status_code == 404


def test_delete_task_purges(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = client.post("/api/tasks", json={"file_path": sample})
    task_id = r1.json()["task_ids"][0]
    time.sleep(2)
    r2 = client.delete(f"/api/tasks/{task_id}")
    assert r2.status_code == 200
    assert r2.json()["purged"] is True


def test_delete_task_routes_through_scheduler_coordination(tmp_path, monkeypatch):
    with make_test_app(tmp_path, monkeypatch) as client:
        scheduler = client.app.state.scheduler
        calls = []

        async def coordinated_delete(task_id):
            calls.append(task_id)
            return {"task_id": task_id, "purged": True}

        monkeypatch.setattr(scheduler, "delete_task", coordinated_delete, raising=False)

        response = client.delete("/api/tasks/coordinated-task")

        assert response.status_code == 200
        assert response.json() == {"task_id": "coordinated-task", "purged": True}
        assert calls == ["coordinated-task"]


def test_delete_unknown_task_returns_404(tmp_path, monkeypatch):
    with make_test_app(tmp_path, monkeypatch) as client:
        response = client.delete("/api/tasks/unknown-task")

        assert response.status_code == 404
        assert response.json() == {"detail": "task not found"}


def test_get_merged_md(tmp_path, monkeypatch):
    sample = str(Path("tests/fixtures/sample.md").resolve())
    with make_test_app(tmp_path, monkeypatch) as client:
        r1 = client.post("/api/tasks", json={"file_path": sample})
        task_id = r1.json()["task_ids"][0]
        for _ in range(100):
            if client.get(f"/api/tasks/{task_id}").json()["status"] == "COMPLETED":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("task did not complete")

        r2 = client.get(f"/api/tasks/{task_id}/merged")
        assert r2.status_code == 200
        assert "▸ AI 解读" in r2.text
        assert "mermaid" in r2.text


def test_get_merged_not_found(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/api/tasks/nope/merged")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "status",
    ["WAITING", "PENDING", "RUNNING", "FAILED", "CANCELLED"],
)
def test_get_merged_rejects_non_completed_task_even_when_file_exists(tmp_path, monkeypatch, status):
    with make_test_app(tmp_path, monkeypatch) as client:
        orch = client.app.state.scheduler._query_orch
        task_id = f"not-completed-{status.lower()}"
        now = int(time.time())
        orch.repo.create_task(
            Task(
                id=task_id,
                file_path="/course.md",
                snapshot_path="/tmp/snapshot.pdf",
                file_sha256="sha",
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
        Path(orch.fs.merged_path(task_id)).write_text("premature", encoding="utf-8")

        response = client.get(f"/api/tasks/{task_id}/merged")

        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "task_not_completed", "status": status}}


def test_cache_materialization_files_are_hidden_until_database_cas_commits(tmp_path, monkeypatch):
    files_published = threading.Event()
    allow_database_cas = threading.Event()
    pause_enabled = threading.Event()
    real_materialize = Repository.materialize_cached_task

    def paused_materialize(self, task, sections, artifacts):
        if pause_enabled.is_set():
            files_published.set()
            assert allow_database_cas.wait(timeout=2)
        return real_materialize(self, task, sections, artifacts)

    monkeypatch.setattr(Repository, "materialize_cached_task", paused_materialize)
    sample = str(Path("tests/fixtures/sample.md").resolve())

    with make_test_app(tmp_path, monkeypatch) as client:
        first = client.post("/api/tasks", json={"file_path": sample})
        first_id = first.json()["task_ids"][0]
        for _ in range(100):
            if client.get(f"/api/tasks/{first_id}").json()["status"] == "COMPLETED":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("first task did not complete")

        pause_enabled.set()
        second = client.post("/api/tasks", json={"file_path": sample})
        second_id = second.json()["task_ids"][0]
        assert files_published.wait(timeout=2)
        try:
            orch = client.app.state.scheduler._query_orch
            assert Path(orch.fs.merged_path(second_id)).is_file()
            response = client.get(f"/api/tasks/{second_id}/merged")
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "task_not_completed"
        finally:
            allow_database_cas.set()

        for _ in range(100):
            status = client.get(f"/api/tasks/{second_id}")
            if status.json()["status"] == "COMPLETED":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("second task did not complete")
        assert client.get(f"/api/tasks/{second_id}/merged").status_code == 200


def test_file_cache_materializes_second_accepted_task_through_api(tmp_path, monkeypatch):
    parser_calls = []
    llm_calls = []
    real_parse = MarkItDownAdapter.parse
    real_interpret = StubLLMClient.interpret

    def counting_parse(self, file_path):
        parser_calls.append(file_path)
        return real_parse(self, file_path)

    def counting_interpret(self, section, raw_md):
        llm_calls.append(section.id)
        return real_interpret(self, section, raw_md)

    monkeypatch.setattr(MarkItDownAdapter, "parse", counting_parse)
    monkeypatch.setattr(StubLLMClient, "interpret", counting_interpret)
    sample = str(Path("tests/fixtures/sample.md").resolve())

    with make_test_app(tmp_path, monkeypatch) as client:
        first = client.post("/api/tasks", json={"file_path": sample})
        first_id = first.json()["task_ids"][0]
        for _ in range(100):
            first_status = client.get(f"/api/tasks/{first_id}")
            if first_status.json()["status"] == "COMPLETED":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("first task did not complete")
        first_parser_calls = len(parser_calls)
        first_llm_calls = len(llm_calls)

        second = client.post("/api/tasks", json={"file_path": sample})
        second_id = second.json()["task_ids"][0]
        assert second_id != first_id
        for _ in range(100):
            second_status = client.get(f"/api/tasks/{second_id}")
            if second_status.json()["status"] == "COMPLETED":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("second task did not complete")

        status_body = second_status.json()
        assert status_body["sections"] > 0
        assert status_body["completed"] == status_body["sections"]
        merged = client.get(f"/api/tasks/{second_id}/merged")
        assert merged.status_code == 200
        assert merged.text.startswith(f"> 任务 ID: {second_id}\n")
        assert len(parser_calls) == first_parser_calls
        assert len(llm_calls) == first_llm_calls
