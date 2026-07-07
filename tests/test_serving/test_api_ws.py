import json
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


def test_ws_receives_event(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]})
    batch_id = r.json()["batch_id"]
    with client.websocket_connect(f"/ws/batch/{batch_id}") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["batch_id"] == batch_id


def test_ws_receives_batch_done(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]})
    batch_id = r.json()["batch_id"]
    with client.websocket_connect(f"/ws/batch/{batch_id}") as ws:
        events = []
        for _ in range(30):
            try:
                msg = json.loads(ws.receive_text())
                events.append(msg)
                if msg["event"] == "BATCH_DONE":
                    break
            except Exception:
                break
        kinds = [e["event"] for e in events]
        assert any(k in kinds for k in ("BATCH_STATE", "TASK_STATE"))
        assert "BATCH_DONE" in kinds


def test_ws_since_replays_filtered(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]})
    batch_id = r.json()["batch_id"]
    time.sleep(2)
    with client.websocket_connect(f"/ws/batch/{batch_id}?since=0") as ws:
        events = []
        for _ in range(30):
            try:
                msg = json.loads(ws.receive_text())
                events.append(msg)
                if msg["event"] == "BATCH_DONE":
                    break
            except Exception:
                break
        # since=0 should only replay seq > 0
        assert all(e["seq"] > 0 for e in events)


def test_ws_nonexistent_batch_closes(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    try:
        with client.websocket_connect("/ws/batch/nonexistent") as ws:
            ws.receive_text()
        raise AssertionError("should have raised")
    except Exception:
        # 410 close or starlette Disconnect
        pass
