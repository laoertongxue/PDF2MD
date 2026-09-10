import asyncio
import json
import time
from concurrent.futures import CancelledError as FutureCancelledError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, QueryParams
from starlette.websockets import WebSocketDisconnect

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api import routes_ws
from parsing_core.serving.models.api import WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer
from parsing_core.serving.scheduler import Scheduler
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"
ALLOWED_ORIGIN = "http://localhost:1420"
AUTH_HEADERS = {"Origin": ALLOWED_ORIGIN, "X-PDF2MD-Session": TEST_SESSION_TOKEN}
WS_PROTOCOLS = ["pdf2md-session-v1", f"pdf2md-session-token.{TEST_SESSION_TOKEN}"]


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
        )
    )


def test_ws_receives_event(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]}, headers=AUTH_HEADERS)
    batch_id = r.json()["batch_id"]
    received = False
    try:
        with client.websocket_connect(
            f"/ws/batch/{batch_id}",
            headers={"Origin": ALLOWED_ORIGIN},
            subprotocols=WS_PROTOCOLS,
        ) as ws:
            assert ws.accepted_subprotocol == "pdf2md-session-v1"
            msg = json.loads(ws.receive_text())
            assert msg["batch_id"] == batch_id
            received = True
    except FutureCancelledError:
        assert received


def test_ws_receives_batch_done(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]}, headers=AUTH_HEADERS)
    batch_id = r.json()["batch_id"]
    events = []
    received_batch_done = False
    try:
        with client.websocket_connect(
            f"/ws/batch/{batch_id}",
            headers={"Origin": ALLOWED_ORIGIN},
            subprotocols=WS_PROTOCOLS,
        ) as ws:
            for _ in range(30):
                try:
                    msg = json.loads(ws.receive_text())
                    events.append(msg)
                    if msg["event"] == "BATCH_DONE":
                        received_batch_done = True
                        break
                except Exception:
                    break
    except FutureCancelledError:
        assert received_batch_done

    kinds = [e["event"] for e in events]
    assert any(k in kinds for k in ("BATCH_STATE", "TASK_STATE"))
    assert "BATCH_DONE" in kinds


def test_ws_since_replays_filtered(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    sample = str(Path("tests/fixtures/sample.md").resolve())
    r = client.post("/api/batches", json={"files": [sample]}, headers=AUTH_HEADERS)
    batch_id = r.json()["batch_id"]
    time.sleep(2)
    events = []
    replay_finished = False
    try:
        with client.websocket_connect(
            f"/ws/batch/{batch_id}?since=0",
            headers={"Origin": ALLOWED_ORIGIN},
            subprotocols=WS_PROTOCOLS,
        ) as ws:
            for _ in range(30):
                try:
                    msg = json.loads(ws.receive_text())
                    events.append(msg)
                    if msg["event"] == "BATCH_DONE":
                        replay_finished = True
                        break
                except Exception:
                    break
    except FutureCancelledError:
        assert replay_finished

    # since=0 should only replay seq > 0
    assert all(e["seq"] > 0 for e in events)


def test_ws_replay_and_live_emit_use_single_ordered_writer(monkeypatch):
    batch_id = "b1"
    scheduler = Scheduler(lambda: object())
    scheduler._buffers[batch_id] = EventRingBuffer(maxlen=10)
    for seq in range(2):
        scheduler._buffers[batch_id].append(
            WSEvent(seq=seq, batch_id=batch_id, event="TASK_STATE", payload={}, ts=0)
        )
    scheduler._seq_counters[batch_id] = 2
    scheduler._subscribers[batch_id] = set()

    class BlockingReplayWebSocket:
        def __init__(self):
            self.headers = Headers(
                {
                    "origin": ALLOWED_ORIGIN,
                    "sec-websocket-protocol": ", ".join(WS_PROTOCOLS),
                }
            )
            self.query_params = QueryParams("")
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    allowed_origins={ALLOWED_ORIGIN},
                    session_token=TEST_SESSION_TOKEN,
                )
            )
            self.first_send_started = asyncio.Event()
            self.release_first_send = asyncio.Event()
            self.all_events_sent = asyncio.Event()
            self.sent_seqs = []
            self.active_sends = 0
            self.max_active_sends = 0
            self._blocked_first_send = False

        async def accept(self, subprotocol=None):
            self.accepted_subprotocol = subprotocol

        async def close(self, code=1000, reason=""):
            self.closed = (code, reason)

        async def send_text(self, text):
            seq = json.loads(text)["seq"]
            self.active_sends += 1
            self.max_active_sends = max(self.max_active_sends, self.active_sends)
            try:
                if seq == 0 and not self._blocked_first_send:
                    self._blocked_first_send = True
                    self.first_send_started.set()
                    await self.release_first_send.wait()
                self.sent_seqs.append(seq)
                if len(self.sent_seqs) == 3:
                    self.all_events_sent.set()
            finally:
                self.active_sends -= 1

        async def receive_text(self):
            await self.all_events_sent.wait()
            raise WebSocketDisconnect(code=1000)

    websocket = BlockingReplayWebSocket()
    monkeypatch.setattr(routes_ws, "get_scheduler", lambda: scheduler)

    async def run_scenario():
        route_task = asyncio.create_task(routes_ws.ws_batch(websocket, batch_id))
        try:
            await asyncio.wait_for(websocket.first_send_started.wait(), timeout=1)
            await asyncio.wait_for(
                scheduler._emit(
                    batch_id,
                    WSEvent(
                        seq=0,
                        batch_id=batch_id,
                        event="TASK_STATE",
                        payload={"status": "LIVE"},
                        ts=0,
                    ),
                ),
                timeout=1,
            )
        finally:
            websocket.release_first_send.set()
        await asyncio.wait_for(route_task, timeout=1)

    asyncio.run(run_scenario())

    assert websocket.max_active_sends == 1
    assert websocket.sent_seqs == [0, 1, 2]
    assert websocket not in scheduler._subscribers[batch_id]


def test_ws_replay_disconnect_unsubscribes_after_successful_subscription(monkeypatch):
    class ReplayScheduler:
        def __init__(self):
            self.subscribers = {}

        def is_batch_gone(self, _batch_id):
            return False

        def replay_events(self, batch_id, _since):
            return [WSEvent(seq=1, batch_id=batch_id, event="TASK_STATE", payload={}, ts=0)]

        def add_subscriber(self, batch_id, websocket):
            self.subscribers.setdefault(batch_id, set()).add(websocket)

        def remove_subscriber(self, batch_id, websocket):
            self.subscribers.get(batch_id, set()).discard(websocket)

        def start_subscriber(self, _batch_id, websocket, events):
            replay_done = asyncio.Event()

            async def send_replay():
                try:
                    for event in events:
                        await websocket.send_text(event.model_dump_json())
                finally:
                    replay_done.set()

            return asyncio.create_task(send_replay()), replay_done

    class ReplayDisconnectWebSocket:
        def __init__(self):
            self.headers = Headers(
                {
                    "origin": ALLOWED_ORIGIN,
                    "sec-websocket-protocol": ", ".join(WS_PROTOCOLS),
                }
            )
            self.query_params = QueryParams("")
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    allowed_origins={ALLOWED_ORIGIN},
                    session_token=TEST_SESSION_TOKEN,
                )
            )
            self.receive_calls = 0

        async def accept(self, subprotocol=None):
            self.accepted_subprotocol = subprotocol

        async def close(self, code=1000, reason=""):
            self.closed = (code, reason)

        async def send_text(self, _text):
            raise WebSocketDisconnect(code=1001)

        async def receive_text(self):
            self.receive_calls += 1
            raise AssertionError("receive loop must not start after replay disconnect")

    scheduler = ReplayScheduler()
    websocket = ReplayDisconnectWebSocket()
    monkeypatch.setattr(routes_ws, "get_scheduler", lambda: scheduler)

    try:
        asyncio.run(routes_ws.ws_batch(websocket, "b1"))
    except WebSocketDisconnect:
        pass

    assert websocket not in scheduler.subscribers.get("b1", set())
    assert websocket.receive_calls == 0


def test_ws_sender_start_failure_unsubscribes_and_waits_for_cleanup(monkeypatch):
    class FailingSenderScheduler:
        def __init__(self):
            self.subscribers = {}
            self.sender_task = None
            self.cleanup_finished = asyncio.Event()
            self.cleanup_task = None

        def is_batch_gone(self, _batch_id):
            return False

        def replay_events(self, _batch_id, _since):
            return []

        def add_subscriber(self, batch_id, websocket):
            self.subscribers.setdefault(batch_id, set()).add(websocket)

        def start_subscriber(self, _batch_id, _websocket, _events):
            async def send_forever():
                await asyncio.Event().wait()

            self.sender_task = asyncio.create_task(send_forever())
            raise RuntimeError("sender startup failed")

        def remove_subscriber(self, batch_id, websocket):
            self.subscribers.get(batch_id, set()).discard(websocket)
            self.sender_task.cancel()

            async def finish_cleanup():
                await asyncio.gather(self.sender_task, return_exceptions=True)
                self.cleanup_finished.set()

            self.cleanup_task = asyncio.create_task(finish_cleanup())
            return self.cleanup_task

    class UnusedWebSocket:
        def __init__(self):
            self.headers = Headers(
                {
                    "origin": ALLOWED_ORIGIN,
                    "sec-websocket-protocol": ", ".join(WS_PROTOCOLS),
                }
            )
            self.query_params = QueryParams("")
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    allowed_origins={ALLOWED_ORIGIN},
                    session_token=TEST_SESSION_TOKEN,
                )
            )

        async def accept(self, subprotocol=None):
            self.accepted_subprotocol = subprotocol

        async def receive_text(self):
            raise AssertionError("receive loop must not start after sender startup failure")

    scheduler = FailingSenderScheduler()
    websocket = UnusedWebSocket()
    monkeypatch.setattr(routes_ws, "get_scheduler", lambda: scheduler)

    async def run_scenario():
        with pytest.raises(RuntimeError, match="sender startup failed"):
            await routes_ws.ws_batch(websocket, "b1")

        assert websocket not in scheduler.subscribers.get("b1", set())
        assert scheduler.cleanup_finished.is_set()
        assert scheduler.cleanup_task.done()
        assert scheduler.sender_task.done()
        assert scheduler.sender_task.cancelled()

    asyncio.run(run_scenario())


def test_ws_disconnect_waits_for_blocked_sender_to_finish(monkeypatch):
    class BlockingSenderScheduler:
        def __init__(self):
            self.subscribers = {}
            self.sender_started = asyncio.Event()
            self.sender_task = None

        def is_batch_gone(self, _batch_id):
            return False

        def replay_events(self, _batch_id, _since):
            return []

        def add_subscriber(self, batch_id, websocket):
            self.subscribers.setdefault(batch_id, set()).add(websocket)

        def start_subscriber(self, _batch_id, _websocket, _events):
            replay_done = asyncio.Event()
            replay_done.set()

            async def send_forever():
                self.sender_started.set()
                await asyncio.Event().wait()

            self.sender_task = asyncio.create_task(send_forever())
            return self.sender_task, replay_done

        def remove_subscriber(self, batch_id, websocket):
            self.subscribers.get(batch_id, set()).discard(websocket)
            self.sender_task.cancel()
            return self.sender_task

    class DisconnectWebSocket:
        def __init__(self, scheduler):
            self.scheduler = scheduler
            self.headers = Headers(
                {
                    "origin": ALLOWED_ORIGIN,
                    "sec-websocket-protocol": ", ".join(WS_PROTOCOLS),
                }
            )
            self.query_params = QueryParams("")
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    allowed_origins={ALLOWED_ORIGIN},
                    session_token=TEST_SESSION_TOKEN,
                )
            )

        async def accept(self, subprotocol=None):
            self.accepted_subprotocol = subprotocol

        async def receive_text(self):
            await self.scheduler.sender_started.wait()
            raise WebSocketDisconnect(code=1000)

    scheduler = BlockingSenderScheduler()
    websocket = DisconnectWebSocket(scheduler)
    monkeypatch.setattr(routes_ws, "get_scheduler", lambda: scheduler)

    async def run_scenario():
        await routes_ws.ws_batch(websocket, "b1")
        assert scheduler.sender_task.done()
        assert scheduler.sender_task.cancelled()

    asyncio.run(run_scenario())


def test_ws_external_cancellation_propagates_after_sender_cleanup(monkeypatch):
    class BlockingSenderScheduler:
        def __init__(self):
            self.subscribers = {}
            self.sender_started = asyncio.Event()
            self.sender_finished = asyncio.Event()
            self.sender_task = None

        def is_batch_gone(self, _batch_id):
            return False

        def replay_events(self, _batch_id, _since):
            return []

        def add_subscriber(self, batch_id, websocket):
            self.subscribers.setdefault(batch_id, set()).add(websocket)

        def start_subscriber(self, _batch_id, _websocket, _events):
            replay_done = asyncio.Event()
            replay_done.set()

            async def send_forever():
                self.sender_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.sender_finished.set()

            self.sender_task = asyncio.create_task(send_forever())
            return self.sender_task, replay_done

        def remove_subscriber(self, batch_id, websocket):
            self.subscribers.get(batch_id, set()).discard(websocket)
            self.sender_task.cancel()
            return self.sender_task

    class WaitingWebSocket:
        def __init__(self):
            self.headers = Headers(
                {
                    "origin": ALLOWED_ORIGIN,
                    "sec-websocket-protocol": ", ".join(WS_PROTOCOLS),
                }
            )
            self.query_params = QueryParams("")
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    allowed_origins={ALLOWED_ORIGIN},
                    session_token=TEST_SESSION_TOKEN,
                )
            )
            self.receive_started = asyncio.Event()
            self.receive_finished = asyncio.Event()

        async def accept(self, subprotocol=None):
            self.accepted_subprotocol = subprotocol

        async def receive_text(self):
            receive_task = asyncio.current_task()
            assert receive_task is not None
            receive_task.set_name("pdf2md-ws-receive-test")
            self.receive_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.receive_finished.set()

    scheduler = BlockingSenderScheduler()
    websocket = WaitingWebSocket()
    monkeypatch.setattr(routes_ws, "get_scheduler", lambda: scheduler)

    async def run_scenario():
        route_task = asyncio.create_task(routes_ws.ws_batch(websocket, "b1"))
        await asyncio.wait_for(scheduler.sender_started.wait(), timeout=1)
        await asyncio.wait_for(websocket.receive_started.wait(), timeout=1)

        route_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route_task

        assert websocket.receive_finished.is_set()
        pending_receive_tasks = {
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("pdf2md-ws-receive")
            and not task.done()
        }
        assert pending_receive_tasks == set()
        assert scheduler.sender_finished.is_set()
        assert scheduler.sender_task.done()
        assert scheduler.sender_task.cancelled()
        assert websocket not in scheduler.subscribers.get("b1", set())

    asyncio.run(run_scenario())


def test_ws_nonexistent_batch_closes(tmp_path, monkeypatch):
    client = make_test_app(tmp_path, monkeypatch)
    with client.websocket_connect(
        "/ws/batch/nonexistent", headers={"Origin": ALLOWED_ORIGIN}, subprotocols=WS_PROTOCOLS
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 4410


@pytest.mark.parametrize(
    ("origin", "protocols", "expected_code"),
    [
        (ALLOWED_ORIGIN, None, 4401),
        (ALLOWED_ORIGIN, ["pdf2md-session-v1", "pdf2md-session-token.wrong"], 4401),
        ("https://attacker.example", WS_PROTOCOLS, 4403),
    ],
)
def test_ws_auth_closes_immediately_after_accept(
    tmp_path, monkeypatch, origin, protocols, expected_code
):
    client = make_test_app(tmp_path, monkeypatch)
    with client.websocket_connect(
        "/ws/batch/any",
        headers={"Origin": origin},
        subprotocols=protocols,
    ) as ws:
        assert ws.accepted_subprotocol is None
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()

    assert exc_info.value.code == expected_code
