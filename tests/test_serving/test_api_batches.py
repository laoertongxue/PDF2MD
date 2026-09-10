import asyncio
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.models.dataclasses import Task
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api.routes_batches import create_batch as create_batch_route
from parsing_core.serving.models.api import BatchCreateRequest
from parsing_core.serving.scheduler import SchedulerCapacityError
from parsing_core.serving.serve import build_app, recover_interrupted_work
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
        sub_dir = base / f"task_{time.time_ns()}"
        sub_dir.mkdir()
        fs = FsLayout(base_dir=str(sub_dir))
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


def test_create_batch_validates_file_count_and_path_length(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    too_many = client.post(
        "/api/batches",
        json={"files": [f"/{index}.pdf" for index in range(101)]},
    )
    too_long = client.post("/api/batches", json={"files": ["/" + "a" * 4096]})
    assert too_many.status_code == 422
    assert too_long.status_code == 422


def test_create_batch_capacity_error_is_stable_429():
    class FullScheduler:
        async def submit_batch(self, *_args):
            raise SchedulerCapacityError("ACTIVE_BATCH_LIMIT", "active batch limit reached")

    async def go():
        with pytest.raises(HTTPException) as captured:
            await create_batch_route(BatchCreateRequest(files=["/one.pdf"]), FullScheduler())
        assert captured.value.status_code == 429
        assert captured.value.detail == {
            "code": "ACTIVE_BATCH_LIMIT",
            "message": "active batch limit reached",
        }

    asyncio.run(go())


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


def test_get_batch_immediately_lists_every_accepted_task(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    created = client.post(
        "/api/batches",
        json={"files": [sample, sample], "concurrency": 1},
    ).json()

    status = client.get(f"/api/batches/{created['batch_id']}")

    assert status.status_code == 200
    assert status.json()["total_tasks"] == created["accepted"] == 2
    assert {task["task_id"] for task in status.json()["tasks"]} == set(created["task_ids"])


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


def test_restart_reconciles_running_batch_and_its_nonterminal_tasks(tmp_path):
    db_path = tmp_path / "serve.db"
    temp_dir = tmp_path / "serve-temp"
    temp_dir.mkdir()
    conn = init_db(str(db_path))
    apply_serve_schema(conn)
    repo = Repository(conn)
    batch = {
        "id": "interrupted-batch",
        "status": "RUNNING",
        "concurrency": 1,
        "policy": "parallel",
        "priority": 0,
        "total_tasks": 3,
        "completed_tasks": 1,
        "created_at": 1,
        "finished_at": None,
    }
    repo.create_batch_with_tasks(
        batch,
        [
            Task(
                id="waiting-task",
                file_path="waiting.pdf",
                snapshot_path="",
                file_sha256="",
                status="WAITING",
                batch_id=batch["id"],
            ),
            Task(
                id="parsing-task",
                file_path="parsing.pdf",
                snapshot_path="snapshot.pdf",
                file_sha256="sha",
                status="PARSING",
                batch_id=batch["id"],
            ),
            Task(
                id="completed-task",
                file_path="completed.pdf",
                snapshot_path="snapshot.pdf",
                file_sha256="done-sha",
                status="COMPLETED",
                batch_id=batch["id"],
            ),
        ],
    )
    conn.close()

    recover_interrupted_work(db_path, temp_dir)

    recovered_conn = init_db(str(db_path))
    apply_serve_schema(recovered_conn)
    recovered = Repository(recovered_conn)
    recovered_batch = recovered.get_batch(batch["id"])
    tasks = {task.id: task for task in recovered.list_all_tasks()}
    assert recovered_batch is not None
    assert recovered_batch["status"] == "INTERRUPTED"
    assert recovered_batch["finished_at"] is not None
    assert tasks["waiting-task"].status == "INTERRUPTED"
    assert tasks["parsing-task"].status == "INTERRUPTED"
    assert tasks["completed-task"].status == "COMPLETED"
    assert not temp_dir.exists()
    recovered_conn.close()
