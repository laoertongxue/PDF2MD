import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from parsing_core.serving.config import (
    DEFAULT_BATCH_CONCURRENCY,
    MAX_GLOBAL_CONCURRENCY,
    RING_BUFFER_MAX,
    SERVE_BUFFER_TTL_SEC,
)
from parsing_core.serving.models.api import BatchResponse, WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer


@dataclass
class BatchContext:
    batch_id: str
    total: int
    sem: asyncio.Semaphore
    completed: int = 0
    started_at: float = field(default_factory=time.time)


OrchestratorFactory = Callable[[], object]


class Scheduler:
    def __init__(
        self,
        orch_factory: OrchestratorFactory,
        max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
    ) -> None:
        self._orch_factory = orch_factory
        self._global_sem = asyncio.Semaphore(max_global_concurrency)
        self._batches: dict[str, BatchContext] = {}
        self._buffers: dict[str, EventRingBuffer] = {}
        self._subscribers: dict[str, set] = {}
        self._seq_counters: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        orch = orch_factory()
        self._query_orch = orch

    async def submit_batch(
        self,
        files: list[str],
        concurrency: int = DEFAULT_BATCH_CONCURRENCY,
        priority: int = 0,
    ) -> BatchResponse:
        batch_id = str(uuid.uuid4())
        task_ids = [str(uuid.uuid4()) for _ in files]
        batch_sem = asyncio.Semaphore(concurrency)
        self._batches[batch_id] = BatchContext(
            batch_id=batch_id,
            total=len(files),
            sem=batch_sem,
        )
        self._buffers[batch_id] = EventRingBuffer(
            maxlen=RING_BUFFER_MAX,
            ttl_sec=SERVE_BUFFER_TTL_SEC,
        )
        self._subscribers[batch_id] = set()
        self._seq_counters[batch_id] = 0

        now = int(time.time())
        self._query_orch.repo.create_batch(
            {
                "id": batch_id,
                "status": "RUNNING",
                "concurrency": concurrency,
                "policy": "parallel",
                "priority": priority,
                "total_tasks": len(files),
                "completed_tasks": 0,
                "created_at": now,
                "finished_at": None,
            }
        )

        await self._emit(
            batch_id,
            WSEvent(
                seq=0,
                batch_id=batch_id,
                event="BATCH_STATE",
                payload={"status": "RUNNING", "total_tasks": len(files)},
                ts=now,
            ),
        )

        for path, task_id in zip(files, task_ids, strict=True):
            asyncio.create_task(self._run_task(batch_id, task_id, path))

        return BatchResponse(
            batch_id=batch_id,
            task_ids=task_ids,
            accepted=len(task_ids),
            rejected=0,
        )

    async def _run_task(self, batch_id: str, task_id: str, file_path: str) -> None:
        if batch_id in self._cancelled:
            await self._emit(
                batch_id,
                WSEvent(
                    seq=0,
                    batch_id=batch_id,
                    task_id=task_id,
                    event="TASK_STATE",
                    payload={"status": "CANCELLED"},
                    ts=int(time.time()),
                ),
            )
            return

        loop = asyncio.get_running_loop()

        async with self._global_sem:
            ctx = self._batches[batch_id]
            async with ctx.sem:
                orch = self._orch_factory()
                emitted_task_id = {"id": task_id}

                def sync_progress(real_task_id, event_kind, payload):
                    emitted_task_id["id"] = real_task_id
                    asyncio.run_coroutine_threadsafe(
                        self._emit(
                            batch_id,
                            WSEvent(
                                seq=0,
                                batch_id=batch_id,
                                task_id=real_task_id,
                                event=event_kind,
                                payload=payload,
                                ts=int(time.time()),
                            ),
                        ),
                        loop,
                    )

                orch.on_progress = sync_progress
                try:
                    await asyncio.to_thread(orch.parse_file, file_path, False, task_id, batch_id)
                except Exception as e:
                    await self._emit(
                        batch_id,
                        WSEvent(
                            seq=0,
                            batch_id=batch_id,
                            task_id=emitted_task_id["id"],
                            event="ERROR",
                            payload={"error": str(e)},
                            ts=int(time.time()),
                        ),
                    )
                finally:
                    ctx.completed += 1
                    self._query_orch.repo.increment_batch_completed(batch_id)
                    final_status = "CANCELLED" if batch_id in self._cancelled else "COMPLETED"
                    await self._emit(
                        batch_id,
                        WSEvent(
                            seq=0,
                            batch_id=batch_id,
                            task_id=emitted_task_id["id"],
                            event="TASK_STATE",
                            payload={"status": final_status},
                            ts=int(time.time()),
                        ),
                    )
                    if ctx.completed >= ctx.total:
                        await self._finalize_batch(batch_id)

    async def _finalize_batch(self, batch_id: str) -> None:
        self._query_orch.repo.finish_batch(batch_id, status="COMPLETED")
        await self._emit(
            batch_id,
            WSEvent(
                seq=0,
                batch_id=batch_id,
                event="BATCH_DONE",
                payload={"status": "COMPLETED"},
                ts=int(time.time()),
            ),
        )

    async def _emit(self, batch_id: str, event_template: WSEvent) -> None:
        if batch_id not in self._seq_counters:
            return
        event_template.seq = self._seq_counters[batch_id]
        self._seq_counters[batch_id] += 1
        self._buffers[batch_id].append(event_template)
        subs = list(self._subscribers.get(batch_id, ()))
        for ws in subs:
            try:
                await ws.send_text(event_template.model_dump_json())
            except Exception:
                self._subscribers[batch_id].discard(ws)

    async def cancel_batch(self, batch_id: str) -> dict:
        self._cancelled.add(batch_id)
        return {"batch_id": batch_id, "cancelled": True}

    def is_batch_gone(self, batch_id: str) -> bool:
        buf = self._buffers.get(batch_id)
        ctx = self._batches.get(batch_id)
        if buf is None and ctx is None:
            return True
        if buf is not None and buf.is_expired():
            return True
        return False

    def replay_events(self, batch_id: str, since: int) -> list[WSEvent]:
        buf = self._buffers.get(batch_id)
        return buf.replay(since) if buf else []

    def add_subscriber(self, batch_id: str, ws) -> None:
        self._subscribers.setdefault(batch_id, set()).add(ws)

    def remove_subscriber(self, batch_id: str, ws) -> None:
        self._subscribers.get(batch_id, set()).discard(ws)
