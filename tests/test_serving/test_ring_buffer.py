import time

from parsing_core.serving.models.api import WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer


def make_event(seq):
    return WSEvent(
        seq=seq,
        batch_id="b1",
        event="TASK_STATE",
        payload={"status": "PARSING"},
        ts=int(time.time()),
    )


def test_append_and_replay_all():
    buf = EventRingBuffer(maxlen=100)
    for i in range(5):
        buf.append(make_event(i))
    assert len(buf) == 5
    assert [e.seq for e in buf.replay(since=-1)] == [0, 1, 2, 3, 4]


def test_replay_since_filters():
    buf = EventRingBuffer(maxlen=100)
    for i in range(10):
        buf.append(make_event(i))
    assert [e.seq for e in buf.replay(since=4)] == [5, 6, 7, 8, 9]


def test_replay_since_minus_one_returns_all():
    buf = EventRingBuffer(maxlen=100)
    for i in range(3):
        buf.append(make_event(i))
    assert len(buf.replay(since=-1)) == 3


def test_maxlen_evicts_oldest():
    buf = EventRingBuffer(maxlen=3)
    for i in range(10):
        buf.append(make_event(i))
    assert len(buf) == 3
    assert [e.seq for e in buf.replay(since=-1)] == [7, 8, 9]


def test_replay_empty_buffer():
    buf = EventRingBuffer(maxlen=10)
    assert buf.replay(since=-1) == []


def test_is_expired_default_false():
    buf = EventRingBuffer(maxlen=10, ttl_sec=1800)
    assert not buf.is_expired()


def test_is_expired_after_ttl():
    buf = EventRingBuffer(maxlen=10, ttl_sec=0)
    buf.append(make_event(0))
    time.sleep(0.1)
    assert buf.is_expired()
