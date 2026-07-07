import time
from pathlib import Path

from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import build_app
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
        fs = FsLayout(base_dir=str(base))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    return TestClient(build_app(orch_factory=orch_factory, max_global_concurrency=4))


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


def test_get_merged_md(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = client.post("/api/tasks", json={"file_path": sample})
    task_id = r1.json()["task_ids"][0]
    time.sleep(2)
    r2 = client.get(f"/api/tasks/{task_id}/merged")
    assert r2.status_code == 200
    assert "▸ AI 解读" in r2.text
    assert "mermaid" in r2.text


def test_get_merged_not_found(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/api/tasks/nope/merged")
    assert r.status_code == 404
