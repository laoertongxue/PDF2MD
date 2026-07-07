import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


def make_app(tmp_path, monkeypatch):
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

    return build_app(orch_factory=orch_factory, max_global_concurrency=4)


@pytest.mark.asyncio
async def test_e2e_batch_submit_and_complete(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.post("/api/batches", json={"files": [sample] * 3, "concurrency": 3})
        assert r.status_code == 200
        batch_id = r.json()["batch_id"]
        for _ in range(30):
            await asyncio.sleep(0.5)
            s = await cli.get(f"/api/batches/{batch_id}")
            body = s.json()
            if body["status"] == "COMPLETED":
                break
        else:
            pytest.fail("batch did not complete in time")
        assert body["completed_tasks"] == 3


@pytest.mark.asyncio
async def test_e2e_merged_download(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as cli:
        r = await cli.get("/health")
        assert r.json() == {"status": "ok"}

        r = await cli.post("/api/tasks", json={"file_path": sample})
        task_id = r.json()["task_ids"][0]
        for _ in range(20):
            await asyncio.sleep(0.5)
            s = await cli.get(f"/api/tasks/{task_id}")
            if s.json()["status"] == "COMPLETED":
                break
        merged = await cli.get(f"/api/tasks/{task_id}/merged")
        assert merged.status_code == 200
        assert "▸ AI 解读" in merged.text
