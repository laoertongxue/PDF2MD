import time
from collections import deque

from parsing_core.serving.models.api import WSEvent


class EventRingBuffer:
    def __init__(self, maxlen: int = 10000, ttl_sec: int = 1800) -> None:
        self._buf: deque[WSEvent] = deque(maxlen=maxlen)
        self._ttl_sec = ttl_sec
        self._last_append_ts: float | None = None

    def append(self, event: WSEvent) -> None:
        self._buf.append(event)
        self._last_append_ts = time.time()

    def replay(self, since: int) -> list[WSEvent]:
        return [e for e in self._buf if e.seq > since]

    def is_expired(self) -> bool:
        if self._last_append_ts is None:
            return False
        return (time.time() - self._last_append_ts) > self._ttl_sec

    def __iter__(self):
        return iter(self._buf)

    def __len__(self) -> int:
        return len(self._buf)
