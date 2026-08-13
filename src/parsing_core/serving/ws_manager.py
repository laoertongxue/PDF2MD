from fastapi import WebSocket

from parsing_core.serving.models.api import WSEvent
from parsing_core.serving.scheduler import Scheduler


class WsManager:
    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler

    async def replay_and_subscribe(
        self,
        batch_id: str,
        ws: WebSocket,
        since: int = -1,
    ) -> list[WSEvent] | None:
        if self.scheduler.is_batch_gone(batch_id):
            await ws.close(code=4410, reason="batch_gone")
            return None
        events: list[WSEvent] = self.scheduler.replay_events(batch_id, since)
        self.scheduler.add_subscriber(batch_id, ws)
        return events

    def unsubscribe(self, batch_id: str, ws: WebSocket) -> None:
        self.scheduler.remove_subscriber(batch_id, ws)
