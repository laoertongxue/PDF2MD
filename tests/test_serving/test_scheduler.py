import asyncio
import time
from pathlib import Path

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.ring_buffer import EventRingBuffer
from parsing_core.serving.scheduler import Scheduler
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema


def make_orch_factory(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    db_path = tmp_path / "serve.db"

    def factory():
        sub_dir = base / f"task_{time.time_ns()}"
        sub_dir.mkdir()
        fs = FsLayout(base_dir=str(sub_dir))
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=str(db_path))

    return factory


def test_submit_batch_returns_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path), max_global_concurrency=4)
    result = asyncio.run(
        sch.submit_batch(
            files=[str(Path("tests/fixtures/sample.md").resolve())], concurrency=2, priority=0
        )
    )
    assert result.batch_id
    assert len(result.task_ids) == 1
    assert result.accepted == 1
    assert result.rejected == 0


def test_emit_increments_seq(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)
    from parsing_core.serving.models.api import WSEvent

    async def go():
        await sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="BATCH_STATE", payload={}, ts=0))
        await sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="BATCH_STATE", payload={}, ts=0))

    asyncio.run(go())
    events = list(sch._buffers["b1"])
    assert [e.seq for e in events] == [0, 1]


def test_emit_to_subscriber(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)

    ws = FakeWS()
    sch._subscribers["b1"] = {ws}

    from parsing_core.serving.models.api import WSEvent

    asyncio.run(
        sch._emit(
            "b1",
            WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={"status": "PARSING"}, ts=0),
        )
    )
    assert len(ws.sent) == 1
    assert "TASK_STATE" in ws.sent[0]


def test_emit_drops_dead_subscriber(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    sch._seq_counters["b1"] = 0
    sch._buffers["b1"] = EventRingBuffer(maxlen=100)

    class DeadWS:
        async def send_text(self, text):
            raise RuntimeError("connection closed")

    ws = DeadWS()
    sch._subscribers["b1"] = {ws}

    from parsing_core.serving.models.api import WSEvent

    asyncio.run(
        sch._emit("b1", WSEvent(seq=0, batch_id="b1", event="TASK_STATE", payload={}, ts=0))
    )
    assert ws not in sch._subscribers["b1"]


def test_cancel_batch_marks_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path))
    result = asyncio.run(sch.cancel_batch("b1"))
    assert result["cancelled"] is True
    assert "b1" in sch._cancelled


def test_submit_batch_awaits_all_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    sch = Scheduler(make_orch_factory(tmp_path), max_global_concurrency=4)
    result = asyncio.run(
        sch.submit_batch(
            files=[str(Path("tests/fixtures/sample.md").resolve())] * 3, concurrency=3, priority=0
        )
    )

    async def wait():
        await asyncio.sleep(5)
        ctx = sch._batches.get(result.batch_id)
        return ctx.completed if ctx else 0

    completed = asyncio.run(wait())
    assert completed >= 3
