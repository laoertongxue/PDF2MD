from parsing_core.serving.models.api import WSEvent


class WsManager:
    def __init__(self, scheduler) -> None:
        self.scheduler = scheduler

    async def replay_and_subscribe(
        self,
        batch_id: str,
        ws,
        since: int = -1,
    ) -> list[WSEvent]:
        if self.scheduler.is_batch_gone(batch_id):
            await ws.close(code=410, reason="batch gone")
            return []
        events = self.scheduler.replay_events(batch_id, since)
        self.scheduler.add_subscriber(batch_id, ws)
        return events

    def unsubscribe(self, batch_id: str, ws) -> None:
        self.scheduler.remove_subscriber(batch_id, ws)
