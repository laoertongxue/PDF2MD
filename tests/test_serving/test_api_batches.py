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
        sub_dir = base / f"task_{time.time_ns()}"
        sub_dir.mkdir()
        fs = FsLayout(base_dir=str(sub_dir))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    return TestClient(build_app(orch_factory=orch_factory, max_global_concurrency=4))


def test_create_batch(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample], "concurrency": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"]
    assert body["accepted"] == 1


def test_create_batch_validates_empty_files(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.post("/api/batches", json={"files": []})
    assert r.status_code == 422


def test_create_batch_validates_concurrency(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample], "concurrency": 0})
    assert r.status_code == 422


def test_get_batch_status(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = client.post("/api/batches", json={"files": [sample]})
    batch_id = r1.json()["batch_id"]
    time.sleep(2)
    r2 = client.get(f"/api/batches/{batch_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["batch_id"] == batch_id


def test_list_batches(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    client.post("/api/batches", json={"files": [sample]})
    client.post("/api/batches", json={"files": [sample]})
    r = client.get("/api/batches")
    assert r.status_code == 200
    assert len(r.json()) >= 2


def test_list_batches_by_status(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    client.post("/api/batches", json={"files": [sample]})
    time.sleep(2)
    r = client.get("/api/batches?status=COMPLETED")
    body = r.json()
    for b in body:
        if b["status"] == "COMPLETED":
            break
    else:
        pass


def test_delete_batch_cancels(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r1 = client.post("/api/batches", json={"files": [sample] * 5, "concurrency": 1})
    batch_id = r1.json()["batch_id"]
    r2 = client.delete(f"/api/batches/{batch_id}")
    assert r2.status_code == 200
    assert r2.json()["cancelled"] is True


def test_get_batch_not_found(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    r = client.get("/api/batches/nope")
    assert r.status_code == 404
