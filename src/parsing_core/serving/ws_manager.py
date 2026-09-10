import asyncio

from fastapi import WebSocket

from parsing_core.serving.models.api import WSEvent
from parsing_core.serving.scheduler import Scheduler, SchedulerCapacityError


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
        try:
            self.scheduler.add_subscriber(batch_id, ws)
        except SchedulerCapacityError:
            await ws.close(code=4429, reason="subscriber_limit")
            return None
        return events

    def start_sender(
        self,
        batch_id: str,
        ws: WebSocket,
        replay_events: list[WSEvent],
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        return self.scheduler.start_subscriber(batch_id, ws, replay_events)

    def unsubscribe(self, batch_id: str, ws: WebSocket) -> asyncio.Task[None] | None:
        return self.scheduler.remove_subscriber(batch_id, ws)
