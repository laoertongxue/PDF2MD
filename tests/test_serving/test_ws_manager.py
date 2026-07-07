import asyncio
import time

from parsing_core.serving.models.api import WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer
from parsing_core.serving.ws_manager import WsManager


class StubScheduler:
    def __init__(self):
        self._buffers = {"b1": EventRingBuffer(maxlen=10, ttl_sec=1800)}
        self._subscribers = {}
        for i in range(5):
            self._buffers["b1"].append(
                WSEvent(seq=i, batch_id="b1", event="TASK_STATE", payload={}, ts=0)
            )
        self._batches = {}

    def replay_events(self, batch_id, since):
        return self._buffers[batch_id].replay(since)

    def add_subscriber(self, batch_id, ws):
        self._subscribers.setdefault(batch_id, set()).add(ws)

    def remove_subscriber(self, batch_id, ws):
        self._subscribers.get(batch_id, set()).discard(ws)

    def is_batch_gone(self, batch_id):
        buf = self._buffers.get(batch_id)
        return buf is not None and buf.is_expired()


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive_text(self):
        await asyncio.sleep(10)
        return ""


def test_replay_since_zero_replays_all():
    sch = StubScheduler()
    mgr = WsManager(sch)
    ws = FakeWS()
    events = asyncio.run(mgr.replay_and_subscribe("b1", ws, since=-1))
    assert len(events) == 5


def test_replay_since_filters():
    sch = StubScheduler()
    mgr = WsManager(sch)
    ws = FakeWS()
    events = asyncio.run(mgr.replay_and_subscribe("b1", ws, since=2))
    assert [e.seq for e in events] == [3, 4]


def test_subscribe_registers():
    sch = StubScheduler()
    mgr = WsManager(sch)
    ws = FakeWS()
    asyncio.run(mgr.replay_and_subscribe("b1", ws, since=-1))
    assert ws in sch._subscribers["b1"]


def test_unsubscribe_removes():
    sch = StubScheduler()
    mgr = WsManager(sch)
    ws = FakeWS()
    asyncio.run(mgr.replay_and_subscribe("b1", ws, since=-1))
    mgr.unsubscribe("b1", ws)
    assert ws not in sch._subscribers.get("b1", set())


def test_batch_gone_returns_410():
    sch = StubScheduler()
    sch._buffers["b1"] = EventRingBuffer(maxlen=10, ttl_sec=0)
    sch._buffers["b1"].append(WSEvent(seq=0, batch_id="b1", event="X", payload={}, ts=0))
    time.sleep(0.01)
    mgr = WsManager(sch)
    ws = FakeWS()
    asyncio.run(mgr.replay_and_subscribe("b1", ws, since=-1))
    assert ws.closed is not None
    assert ws.closed[0] == 410
