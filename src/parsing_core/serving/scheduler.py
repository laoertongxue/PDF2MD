from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, Protocol, cast

from parsing_core.log import get_logger
from parsing_core.models.dataclasses import Task
from parsing_core.serving.config import (
    DEFAULT_BATCH_CONCURRENCY,
    MAX_ACTIVE_BATCHES,
    MAX_BATCH_FILES,
    MAX_BATCH_PATH_LENGTH,
    MAX_BATCH_SUBSCRIBERS,
    MAX_GLOBAL_CONCURRENCY,
    RING_BUFFER_MAX,
    SERVE_BATCH_HISTORY_MAX,
    SERVE_BUFFER_TTL_SEC,
    THREAD_PROGRESS_PENDING_MAX,
)
from parsing_core.serving.models.api import BatchResponse, WSEvent
from parsing_core.serving.ring_buffer import EventRingBuffer
from parsing_core.storage.fs_layout import TaskDirectoryEntry
from parsing_core.storage.repository import BatchRecord

log = get_logger(__name__)

WS_SUBSCRIBER_QUEUE_MAX = 256
STARTUP_CLEANUP_BATCH_SIZE = 128
_PURGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="pdf2md-purge",
)
TaskPhase = Literal["WAITING", "QUEUED", "RUNNING", "DONE"]
TaskTerminalStatus = Literal["COMPLETED", "FAILED", "CANCELLED"]


class SchedulerCapacityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PendingCompletion:
    task_id: str
    emitted_task_id: str
    status: TaskTerminalStatus
    error_msg: str | None = None


@dataclass
class DispatchRequest:
    batch_id: str
    task_id: str
    ready: asyncio.Future[None]
    sequence: int
    dispatched: bool = False
    released: bool = False


@dataclass(frozen=True)
class WorkerOutcome:
    started: bool
    result: object | None = None


@dataclass
class BatchContext:
    batch_id: str
    total: int
    sem: asyncio.Semaphore
    concurrency: int
    priority: int
    completed: int = 0
    completed_task_ids: set[str] = field(default_factory=set)
    pending_completions: dict[str, PendingCompletion] = field(default_factory=dict)
    task_phases: dict[str, TaskPhase] = field(default_factory=dict)
    externally_cancelled_task_ids: set[str] = field(default_factory=set)
    cancel_requested: bool = False
    has_failed: bool = False
    has_cancelled: bool = False
    status: str = "RUNNING"
    finalized: bool = False
    started_at: float = 0
    finished_at: float | None = None
    persistence_error_reported: bool = False
    running_tasks: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    persistence_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    phase_lock: threading.Lock = field(default_factory=threading.Lock)


ProgressCallback = Callable[[str, str, dict[str, object]], None]


class BatchRepository(Protocol):
    def create_batch_with_tasks(self, batch: BatchRecord, tasks: list[Task]) -> None: ...

    def update_task_status(
        self,
        task_id: str,
        status: str,
        error_msg: str | None = None,
    ) -> None: ...

    def set_batch_progress(
        self,
        batch_id: str,
        completed: int,
        status: str | None = None,
    ) -> None: ...

    def get_batch(self, batch_id: str) -> BatchRecord | None: ...

    def list_batches_by_status(self, status: str) -> list[BatchRecord]: ...

    def list_all_batches(self) -> list[BatchRecord]: ...

    def list_all_tasks(self) -> list[Task]: ...

    def list_waiting_tasks_page(
        self,
        *,
        after_id: str | None,
        limit: int,
    ) -> list[Task]: ...

    def get_task(self, task_id: str) -> Task | None: ...

    def list_task_ids_with_materialized_children(self) -> set[str]: ...


class SchedulerOrchestrator(Protocol):
    repo: BatchRepository
    fs: SchedulerFileLayout
    on_progress: ProgressCallback | None

    def parse_file(
        self,
        file_path: str,
        force: bool,
        task_id: str,
        batch_id: str,
    ) -> object: ...

    def purge(self, task_id: str) -> dict[str, object]: ...

    def try_purge(self, task_id: str) -> dict[str, object] | None: ...

    def _try_open_resume_lock(
        self,
        task_id: str,
        *,
        blocking: bool = False,
    ) -> int | None: ...

    def _close_resume_lock(self, lock_fd: int) -> None: ...


class SchedulerFileLayout(Protocol):
    def iter_task_entries(
        self,
        *,
        batch_size: int,
    ) -> Iterator[tuple[TaskDirectoryEntry, ...]]: ...

    def remove_staging_tree(
        self,
        name: str,
        *,
        expected_identity: tuple[int, int],
    ) -> bool: ...

    def remove_task_tree(
        self,
        task_id: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> bool: ...

    def task_identity(self, task_id: str) -> tuple[int, int] | None: ...


class WebSocketSender(Protocol):
    async def send_text(self, text: str) -> None: ...


OrchestratorFactory = Callable[[], SchedulerOrchestrator]


class Scheduler:
    def __init__(
        self,
        orch_factory: OrchestratorFactory,
        max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
        subscriber_queue_max: int = WS_SUBSCRIBER_QUEUE_MAX,
        buffer_ttl_sec: float = SERVE_BUFFER_TTL_SEC,
        max_history_batches: int = SERVE_BATCH_HISTORY_MAX,
        max_active_batches: int = MAX_ACTIVE_BATCHES,
        max_subscribers_per_batch: int = MAX_BATCH_SUBSCRIBERS,
        progress_pending_max: int = THREAD_PROGRESS_PENDING_MAX,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_global_concurrency < 1:
            raise ValueError("max_global_concurrency must be positive")
        if subscriber_queue_max < 1:
            raise ValueError("subscriber_queue_max must be positive")
        if buffer_ttl_sec < 0:
            raise ValueError("buffer_ttl_sec must be non-negative")
        if max_history_batches < 0:
            raise ValueError("max_history_batches must be non-negative")
        if max_active_batches < 1:
            raise ValueError("max_active_batches must be positive")
        if max_subscribers_per_batch < 1:
            raise ValueError("max_subscribers_per_batch must be positive")
        if progress_pending_max < 1:
            raise ValueError("progress_pending_max must be positive")
        if clock is not None:
            wall_clock = clock
            monotonic_clock = clock

        self._orch_factory = orch_factory
        self._max_global_concurrency = max_global_concurrency
        self._global_running = 0
        self._dispatch_lock = asyncio.Lock()
        self._dispatch_waiters: dict[tuple[str, str], DispatchRequest] = {}
        self._dispatch_sequence = 0
        self._last_batch_by_priority: dict[int, str] = {}
        self._last_deferred_priority: int | None = None
        self._priority_streak = 0
        self._priority_burst = 3
        self._batches: dict[str, BatchContext] = {}
        self._buffers: dict[str, EventRingBuffer] = {}
        self._subscribers: dict[str, set[WebSocketSender]] = {}
        self._subscriber_queues: dict[str, dict[WebSocketSender, asyncio.Queue[WSEvent]]] = {}
        self._subscriber_tasks: dict[str, dict[WebSocketSender, asyncio.Task[None]]] = {}
        self._subscriber_queue_max = subscriber_queue_max
        self._max_subscribers_per_batch = max_subscribers_per_batch
        self._seq_counters: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._batch_tasks: dict[str, dict[str, asyncio.Task[None]]] = {}
        self._thread_task_ids: dict[str, set[str]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._worker_futures: dict[tuple[str, str], concurrent.futures.Future[WorkerOutcome]] = {}
        self._persistence_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._progress_futures: dict[concurrent.futures.Future[None], str] = {}
        self._progress_lock = threading.Lock()
        self._progress_pending_max = progress_pending_max
        self._buffer_ttl_sec = buffer_ttl_sec
        self._max_history_batches = max_history_batches
        self._max_active_batches = max_active_batches
        self._max_tracked_batches = max_active_batches + max_history_batches
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting_batches = True
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False
        self._shutdown_task: asyncio.Task[None] | None = None

        self._query_orch = orch_factory()
        self._cleanup_interrupted_materializations()

    def _cleanup_interrupted_materializations(self) -> None:
        fs = getattr(self._query_orch, "fs", None)
        if fs is None:
            return
        try:
            for entries in fs.iter_task_entries(batch_size=STARTUP_CLEANUP_BATCH_SIZE):
                for entry in entries:
                    task_id = self._staging_task_id(entry.name)
                    if task_id is not None:
                        self._cleanup_interrupted_staging(
                            fs,
                            entry.name,
                            task_id,
                            entry.identity,
                        )
        except (OSError, RuntimeError):
            log.warning("startup_task_scan_failed")
        self._cleanup_interrupted_waiting_outputs(fs)

    @staticmethod
    def _staging_task_id(name: str) -> str | None:
        if not name.startswith(".") or not name.endswith(".tmp"):
            return None
        task_id, separator, nonce = name[1:-4].rpartition(".")
        if task_id and separator and nonce:
            return task_id
        return None

    def _try_cleanup_task_lock(self, task_id: str) -> int | None:
        try:
            return self._query_orch._try_open_resume_lock(task_id)
        except (OSError, RuntimeError, ValueError):
            log.warning("cleanup_task_lock_unavailable task_id=%s", task_id)
            return None

    def _cleanup_interrupted_staging(
        self,
        fs: SchedulerFileLayout,
        name: str,
        task_id: str,
        expected_identity: tuple[int, int],
    ) -> None:
        lock_fd = self._try_cleanup_task_lock(task_id)
        if lock_fd is None:
            return
        try:
            try:
                fs.remove_staging_tree(name, expected_identity=expected_identity)
            except (OSError, RuntimeError):
                log.warning("startup_staging_cleanup_failed task_id=%s", task_id)
        finally:
            self._query_orch._close_resume_lock(lock_fd)

    def _cleanup_interrupted_waiting_outputs(self, fs: SchedulerFileLayout) -> None:
        tasks_with_children = self._query_orch.repo.list_task_ids_with_materialized_children()
        after_id: str | None = None
        while True:
            tasks = self._query_orch.repo.list_waiting_tasks_page(
                after_id=after_id,
                limit=STARTUP_CLEANUP_BATCH_SIZE,
            )
            if not tasks:
                return
            for task in tasks:
                after_id = task.id
                if task.id in tasks_with_children:
                    continue
                try:
                    expected_identity = fs.task_identity(task.id)
                except (OSError, RuntimeError):
                    log.warning("startup_task_identity_failed task_id=%s", task.id)
                    continue
                if expected_identity is None:
                    continue
                lock_fd = self._try_cleanup_task_lock(task.id)
                if lock_fd is None:
                    continue
                try:
                    try:
                        fs.remove_task_tree(
                            task.id,
                            expected_identity=expected_identity,
                        )
                    except (OSError, RuntimeError):
                        log.warning("startup_task_cleanup_failed task_id=%s", task.id)
                finally:
                    self._query_orch._close_resume_lock(lock_fd)

    async def submit_batch(
        self,
        files: list[str],
        concurrency: int = DEFAULT_BATCH_CONCURRENCY,
        priority: int = 0,
    ) -> BatchResponse:
        if not self._accepting_batches:
            raise RuntimeError("scheduler is shutting down")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if len(files) > MAX_BATCH_FILES:
            raise ValueError(f"batch accepts at most {MAX_BATCH_FILES} files")
        if any(not path or len(path) > MAX_BATCH_PATH_LENGTH for path in files):
            raise ValueError(f"file paths must be 1..{MAX_BATCH_PATH_LENGTH} characters")

        self._purge_terminal_batches()
        active_batches = sum(not ctx.finalized for ctx in self._batches.values())
        if active_batches >= self._max_active_batches:
            raise SchedulerCapacityError(
                "ACTIVE_BATCH_LIMIT",
                "active batch limit reached",
            )
        if len(self._batches) >= self._max_tracked_batches:
            raise SchedulerCapacityError(
                "TRACKED_BATCH_LIMIT",
                "tracked batch limit reached",
            )

        batch_id = str(uuid.uuid4())
        task_ids = [str(uuid.uuid4()) for _ in files]
        wall_now = int(self._wall_clock())
        batch_record = BatchRecord(
            id=batch_id,
            status="RUNNING",
            concurrency=concurrency,
            policy="parallel",
            priority=priority,
            total_tasks=len(files),
            completed_tasks=0,
            created_at=wall_now,
            finished_at=None,
        )
        preregistered_tasks = [
            Task(
                id=task_ids[index],
                file_path=file_path,
                snapshot_path="",
                file_sha256="",
                status="WAITING",
                created_at=wall_now,
                updated_at=wall_now,
                batch_id=batch_id,
            )
            for index, file_path in enumerate(files)
        ]
        self._query_orch.repo.create_batch_with_tasks(batch_record, preregistered_tasks)

        ctx = BatchContext(
            batch_id=batch_id,
            total=len(files),
            sem=asyncio.Semaphore(concurrency),
            concurrency=concurrency,
            priority=priority,
            started_at=self._monotonic_clock(),
            task_phases=dict.fromkeys(task_ids, "WAITING"),
        )
        self._batches[batch_id] = ctx
        self._buffers[batch_id] = EventRingBuffer(
            maxlen=RING_BUFFER_MAX,
            ttl_sec=self._buffer_ttl_sec,
            clock=self._monotonic_clock,
        )
        self._subscribers[batch_id] = set()
        self._subscriber_queues[batch_id] = {}
        self._subscriber_tasks[batch_id] = {}
        self._seq_counters[batch_id] = 0
        self._batch_tasks[batch_id] = {}
        self._thread_task_ids[batch_id] = set()

        await self._emit(
            batch_id,
            WSEvent(
                seq=0,
                batch_id=batch_id,
                event="BATCH_STATE",
                payload={"status": "RUNNING", "total_tasks": len(files)},
                ts=wall_now,
            ),
        )

        for index, path in enumerate(files):
            task_id = task_ids[index]
            task = asyncio.create_task(
                self._run_task(batch_id, task_id, path),
                name=f"batch-task-{batch_id}-{task_id}",
            )
            self._batch_tasks[batch_id][task_id] = task
            task.add_done_callback(partial(self._on_batch_task_done, batch_id, task_id))

        if not task_ids:
            await self._finalize_batch(batch_id)

        return BatchResponse(
            batch_id=batch_id,
            task_ids=task_ids,
            accepted=len(task_ids),
            rejected=0,
        )

    def _batch_cancel_requested(self, ctx: BatchContext) -> bool:
        return ctx.cancel_requested or ctx.batch_id in self._cancelled

    def _task_cancelled_before_worker_start(self, ctx: BatchContext, task_id: str) -> bool:
        return self._batch_cancel_requested(ctx) or task_id in ctx.externally_cancelled_task_ids

    def _default_executor(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> concurrent.futures.Executor:
        executor = getattr(loop, "_default_executor", None)
        if executor is None:
            executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="pdf2md-worker")
            loop.set_default_executor(executor)
        return cast(concurrent.futures.Executor, executor)

    def _eligible_dispatch_requests_locked(self) -> list[DispatchRequest]:
        eligible: list[DispatchRequest] = []
        for request in self._dispatch_waiters.values():
            ctx = self._batches.get(request.batch_id)
            if (
                request.dispatched
                or request.ready.cancelled()
                or ctx is None
                or self._batch_cancel_requested(ctx)
                or ctx.running_tasks >= ctx.concurrency
            ):
                continue
            eligible.append(request)
        return eligible

    def _select_dispatch_request_locked(
        self,
        eligible: list[DispatchRequest],
    ) -> DispatchRequest:
        priorities = sorted(
            {self._batches[request.batch_id].priority for request in eligible},
            reverse=True,
        )
        highest_priority = priorities[0]
        if len(priorities) > 1 and self._priority_streak >= self._priority_burst:
            deferred_priorities = priorities[1:]
            if self._last_deferred_priority in deferred_priorities:
                deferred_index = (
                    deferred_priorities.index(self._last_deferred_priority) + 1
                ) % len(deferred_priorities)
            else:
                deferred_index = 0
            selected_priority = deferred_priorities[deferred_index]
            self._last_deferred_priority = selected_priority
            self._priority_streak = 0
        else:
            selected_priority = highest_priority
            if len(priorities) > 1:
                self._priority_streak += 1
            else:
                self._priority_streak = 0

        candidates = [
            request
            for request in eligible
            if self._batches[request.batch_id].priority == selected_priority
        ]
        first_sequence_by_batch: dict[str, int] = {}
        for request in candidates:
            first_sequence_by_batch[request.batch_id] = min(
                request.sequence,
                first_sequence_by_batch.get(request.batch_id, request.sequence),
            )
        batch_ids = sorted(first_sequence_by_batch, key=first_sequence_by_batch.__getitem__)
        last_batch = self._last_batch_by_priority.get(selected_priority)
        if last_batch in batch_ids:
            selected_index = (batch_ids.index(last_batch) + 1) % len(batch_ids)
        else:
            selected_index = 0
        selected_batch = batch_ids[selected_index]
        self._last_batch_by_priority[selected_priority] = selected_batch
        return min(
            (request for request in candidates if request.batch_id == selected_batch),
            key=lambda request: request.sequence,
        )

    def _dispatch_available_locked(self) -> None:
        stale_keys = [
            key
            for key, request in self._dispatch_waiters.items()
            if not request.dispatched and request.ready.cancelled()
        ]
        for key in stale_keys:
            self._dispatch_waiters.pop(key, None)

        while self._global_running < self._max_global_concurrency:
            eligible = self._eligible_dispatch_requests_locked()
            if not eligible:
                return
            request = self._select_dispatch_request_locked(eligible)
            ctx = self._batches[request.batch_id]
            request.dispatched = True
            ctx.running_tasks += 1
            self._global_running += 1
            with ctx.phase_lock:
                ctx.task_phases[request.task_id] = "QUEUED"
            if not request.ready.done():
                request.ready.set_result(None)

    async def _wait_for_dispatch(
        self,
        batch_id: str,
        task_id: str,
    ) -> DispatchRequest:
        loop = asyncio.get_running_loop()
        request = DispatchRequest(
            batch_id=batch_id,
            task_id=task_id,
            ready=loop.create_future(),
            sequence=self._dispatch_sequence,
        )
        self._dispatch_sequence += 1
        key = (batch_id, task_id)
        async with self._dispatch_lock:
            self._dispatch_waiters[key] = request
            self._dispatch_available_locked()
        try:
            await request.ready
        except asyncio.CancelledError:
            if request.dispatched:
                await self._release_dispatch_request(request)
            else:
                async with self._dispatch_lock:
                    self._dispatch_waiters.pop(key, None)
                    self._dispatch_available_locked()
            raise
        return request

    async def _release_dispatch_request(self, request: DispatchRequest) -> None:
        async with self._dispatch_lock:
            self._dispatch_waiters.pop((request.batch_id, request.task_id), None)
            if request.dispatched and not request.released:
                request.released = True
                ctx = self._batches.get(request.batch_id)
                if ctx is not None:
                    ctx.running_tasks = max(0, ctx.running_tasks - 1)
                self._global_running = max(0, self._global_running - 1)
            self._dispatch_available_locked()

    def _run_worker(
        self,
        ctx: BatchContext,
        task_id: str,
        orch: SchedulerOrchestrator,
        file_path: str,
        batch_id: str,
    ) -> WorkerOutcome:
        with ctx.phase_lock:
            if self._task_cancelled_before_worker_start(ctx, task_id):
                return WorkerOutcome(started=False)
            ctx.task_phases[task_id] = "RUNNING"
            self._thread_task_ids.setdefault(batch_id, set()).add(task_id)
        try:
            result = orch.parse_file(file_path, False, task_id, batch_id)
            return WorkerOutcome(started=True, result=result)
        finally:
            with ctx.phase_lock:
                self._thread_task_ids.get(batch_id, set()).discard(task_id)

    async def _await_worker_after_outer_cancel(
        self,
        worker: concurrent.futures.Future[WorkerOutcome],
        ctx: BatchContext,
        task_id: str,
    ) -> WorkerOutcome:
        with ctx.phase_lock:
            ctx.externally_cancelled_task_ids.add(task_id)
        while True:
            if worker.cancelled():
                return WorkerOutcome(started=False)
            try:
                return await asyncio.shield(asyncio.wrap_future(worker))
            except asyncio.CancelledError:
                with ctx.phase_lock:
                    ctx.externally_cancelled_task_ids.add(task_id)

    async def _run_task(self, batch_id: str, task_id: str, file_path: str) -> None:
        ctx = self._batches.get(batch_id)
        if ctx is None:
            return
        loop = asyncio.get_running_loop()
        final_status: TaskTerminalStatus = "COMPLETED"
        error_msg: str | None = None
        outer_cancelled = False
        dispatch_request: DispatchRequest | None = None
        worker: concurrent.futures.Future[WorkerOutcome] | None = None
        worker_error: Exception | None = None
        worker_outcome = WorkerOutcome(started=False)

        try:
            if self._batch_cancel_requested(ctx):
                final_status = "CANCELLED"
                return

            dispatch_request = await self._wait_for_dispatch(batch_id, task_id)
            async with ctx.sem:
                if self._batch_cancel_requested(ctx):
                    final_status = "CANCELLED"
                    return

                orch = self._orch_factory()

                def sync_progress(
                    real_task_id: str,
                    event_kind: str,
                    payload: dict[str, object],
                ) -> None:
                    self._relay_progress(
                        loop,
                        batch_id,
                        real_task_id,
                        event_kind,
                        payload,
                    )

                orch.on_progress = sync_progress
                with ctx.phase_lock:
                    if self._batch_cancel_requested(ctx):
                        final_status = "CANCELLED"
                        return
                    ctx.task_phases[task_id] = "QUEUED"

                worker = self._default_executor(loop).submit(
                    self._run_worker,
                    ctx,
                    task_id,
                    orch,
                    file_path,
                    batch_id,
                )
                self._worker_futures[(batch_id, task_id)] = worker
                try:
                    worker_outcome = await asyncio.shield(asyncio.wrap_future(worker))
                except asyncio.CancelledError:
                    outer_cancelled = True
                    final_status = "CANCELLED"
                    try:
                        worker_outcome = await self._await_worker_after_outer_cancel(
                            worker,
                            ctx,
                            task_id,
                        )
                    except Exception as error:
                        worker_error = error
                        error_msg = str(error)
                except Exception as error:
                    worker_error = error

                if worker_error is not None and not outer_cancelled:
                    final_status = "FAILED"
                    error_msg = str(worker_error)
                    await self._emit(
                        batch_id,
                        WSEvent(
                            seq=0,
                            batch_id=batch_id,
                            task_id=task_id,
                            event="ERROR",
                            payload={"error": error_msg},
                            ts=int(self._wall_clock()),
                        ),
                    )
                elif not worker_outcome.started:
                    final_status = "CANCELLED"
        except asyncio.CancelledError:
            outer_cancelled = True
            final_status = "CANCELLED"
            with ctx.phase_lock:
                ctx.externally_cancelled_task_ids.add(task_id)
            if worker is not None:
                try:
                    await self._await_worker_after_outer_cancel(worker, ctx, task_id)
                except Exception as error:
                    worker_error = error
                    error_msg = str(error)
        finally:
            if self._batch_cancel_requested(ctx) or outer_cancelled:
                final_status = "CANCELLED"
            if worker is not None and worker.done():
                self._worker_futures.pop((batch_id, task_id), None)
            if dispatch_request is not None:
                await self._release_dispatch_request(dispatch_request)
            await self._complete_task(
                batch_id,
                task_id,
                task_id,
                final_status,
                error_msg,
            )
            tasks = self._batch_tasks.get(batch_id)
            current_task = asyncio.current_task()
            if tasks is not None and tasks.get(task_id) is current_task:
                tasks.pop(task_id, None)

        if outer_cancelled:
            raise asyncio.CancelledError()

    def _relay_progress(
        self,
        loop: asyncio.AbstractEventLoop,
        batch_id: str,
        task_id: str,
        event_kind: str,
        payload: dict[str, object],
    ) -> None:
        with self._progress_lock:
            if len(self._progress_futures) >= self._progress_pending_max:
                return
            coroutine = self._emit(
                batch_id,
                WSEvent(
                    seq=0,
                    batch_id=batch_id,
                    task_id=task_id,
                    event=event_kind,
                    payload=dict(payload),
                    ts=int(self._wall_clock()),
                ),
            )
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except RuntimeError:
                coroutine.close()
                return
            self._progress_futures[future] = batch_id
        future.add_done_callback(self._progress_future_done)

    def _progress_future_done(self, future: concurrent.futures.Future[None]) -> None:
        with self._progress_lock:
            self._progress_futures.pop(future, None)
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            log.exception("scheduler_progress_relay_failed")

    def _cancel_progress_for_batch(self, batch_id: str) -> None:
        with self._progress_lock:
            pending = [
                future
                for future, future_batch_id in self._progress_futures.items()
                if future_batch_id == batch_id
            ]
        for future in pending:
            future.cancel()

    async def _set_batch_progress_with_retry(
        self,
        ctx: BatchContext,
        completed: int,
        status: str | None,
    ) -> bool:
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                self._query_orch.repo.set_batch_progress(ctx.batch_id, completed, status)
                return True
            except Exception as error:
                last_error = error

        await self._report_persistence_failure(ctx, completed, status, last_error)
        self._ensure_persistence_retry(ctx)
        return False

    async def _report_persistence_failure(
        self,
        ctx: BatchContext,
        completed: int,
        status: str | None,
        error: Exception | None,
    ) -> None:
        if not ctx.persistence_error_reported:
            ctx.persistence_error_reported = True
            await self._emit(
                ctx.batch_id,
                WSEvent(
                    seq=0,
                    batch_id=ctx.batch_id,
                    event="ERROR",
                    payload={
                        "code": "BATCH_PERSISTENCE_FAILED",
                        "error": "batch progress persistence failed; retry required",
                    },
                    ts=int(self._wall_clock()),
                ),
            )
        log.error(
            "batch_progress_persistence_failed batch_id=%s completed=%s status=%s error=%s",
            ctx.batch_id,
            completed,
            status,
            error,
        )

    def _ensure_persistence_retry(self, ctx: BatchContext) -> None:
        existing = self._persistence_retry_tasks.get(ctx.batch_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._persistence_retry_loop(ctx.batch_id),
            name=f"batch-persistence-retry-{ctx.batch_id}",
        )
        self._persistence_retry_tasks[ctx.batch_id] = task
        task.add_done_callback(self._consume_task_result)

    async def _persistence_retry_loop(self, batch_id: str) -> None:
        delay = 0.05
        current = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(delay)
                ctx = self._batches.get(batch_id)
                if ctx is None or ctx.finalized:
                    return
                if await self.retry_batch_persistence(batch_id):
                    return
                delay = min(delay * 2, 1.0)
        finally:
            if self._persistence_retry_tasks.get(batch_id) is current:
                self._persistence_retry_tasks.pop(batch_id, None)

    def _effective_task_status(
        self,
        ctx: BatchContext,
        status: TaskTerminalStatus,
    ) -> TaskTerminalStatus:
        if self._batch_cancel_requested(ctx):
            return "CANCELLED"
        return status

    async def _flush_pending_completions_locked(self, ctx: BatchContext) -> None:
        while ctx.pending_completions:
            task_id = next(iter(ctx.pending_completions))
            pending = ctx.pending_completions[task_id]
            status = self._effective_task_status(ctx, pending.status)
            completed = ctx.completed + 1
            if not await self._set_batch_progress_with_retry(ctx, completed, None):
                return
            try:
                self._query_orch.repo.update_task_status(
                    pending.task_id,
                    status,
                    pending.error_msg,
                )
            except Exception as error:
                await self._report_persistence_failure(ctx, completed, None, error)
                self._ensure_persistence_retry(ctx)
                return

            ctx.pending_completions.pop(task_id, None)
            ctx.completed_task_ids.add(task_id)
            ctx.completed = completed
            if status == "FAILED":
                ctx.has_failed = True
            elif status == "CANCELLED":
                ctx.has_cancelled = True
            with ctx.phase_lock:
                ctx.task_phases[task_id] = "DONE"
            await self._emit(
                ctx.batch_id,
                WSEvent(
                    seq=0,
                    batch_id=ctx.batch_id,
                    task_id=pending.emitted_task_id,
                    event="TASK_STATE",
                    payload={"status": status},
                    ts=int(self._wall_clock()),
                ),
            )

        if ctx.completed >= ctx.total:
            await self._finalize_batch_locked(ctx)

    async def _complete_task(
        self,
        batch_id: str,
        task_id: str,
        emitted_task_id: str,
        status: TaskTerminalStatus,
        error_msg: str | None = None,
    ) -> bool:
        ctx = self._batches.get(batch_id)
        if ctx is None:
            return False
        async with ctx.persistence_lock:
            if task_id in ctx.completed_task_ids or task_id in ctx.pending_completions:
                return False
            ctx.pending_completions[task_id] = PendingCompletion(
                task_id=task_id,
                emitted_task_id=emitted_task_id,
                status=status,
                error_msg=error_msg,
            )
            await self._flush_pending_completions_locked(ctx)
            return task_id in ctx.completed_task_ids

    def _terminal_status(self, ctx: BatchContext) -> TaskTerminalStatus:
        if self._batch_cancel_requested(ctx) or ctx.has_cancelled:
            return "CANCELLED"
        if ctx.has_failed:
            return "FAILED"
        return "COMPLETED"

    async def _finalize_batch_locked(self, ctx: BatchContext) -> None:
        if ctx.finalized or ctx.completed < ctx.total or ctx.pending_completions:
            return
        status = self._terminal_status(ctx)
        if not await self._set_batch_progress_with_retry(ctx, ctx.completed, status):
            return

        ctx.finalized = True
        ctx.status = status
        ctx.finished_at = self._monotonic_clock()
        self._cancel_progress_for_batch(ctx.batch_id)
        await self._emit(
            ctx.batch_id,
            WSEvent(
                seq=0,
                batch_id=ctx.batch_id,
                event="BATCH_DONE",
                payload={"status": status},
                ts=int(self._wall_clock()),
            ),
        )
        ctx.done.set()
        self._purge_terminal_batches()

    async def _finalize_batch(self, batch_id: str) -> None:
        ctx = self._batches.get(batch_id)
        if ctx is None:
            return
        async with ctx.persistence_lock:
            await self._finalize_batch_locked(ctx)

    async def retry_batch_persistence(self, batch_id: str) -> bool:
        ctx = self._batches.get(batch_id)
        if ctx is None:
            return False
        async with ctx.persistence_lock:
            await self._flush_pending_completions_locked(ctx)
            if not ctx.pending_completions and ctx.completed >= ctx.total:
                await self._finalize_batch_locked(ctx)
            return ctx.finalized

    def _on_batch_task_done(
        self,
        batch_id: str,
        task_id: str,
        task: asyncio.Task[None],
    ) -> None:
        tasks = self._batch_tasks.get(batch_id)
        if tasks is not None and tasks.get(task_id) is task:
            tasks.pop(task_id, None)
        if task.cancelled():
            self._schedule_prestart_cancel_completion(batch_id, task_id)
        self._consume_task_result(task)
        self._purge_terminal_batches()

    def _schedule_prestart_cancel_completion(self, batch_id: str, task_id: str) -> None:
        ctx = self._batches.get(batch_id)
        if ctx is None or task_id in ctx.completed_task_ids or task_id in ctx.pending_completions:
            return

        async def complete() -> None:
            await self._complete_task(batch_id, task_id, task_id, "CANCELLED")

        cleanup = asyncio.create_task(
            complete(),
            name=f"batch-prestart-cancel-{batch_id}-{task_id}",
        )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        self._cleanup_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("scheduler_background_task_failed")

    def _track_cancelled_cleanup_task(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            task.cancel()
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._consume_task_result)
            return
        self._consume_task_result(task)

    def _batch_has_subscribers(self, batch_id: str) -> bool:
        return bool(self._subscribers.get(batch_id))

    def _batch_has_active_tasks(self, batch_id: str) -> bool:
        return any(not task.done() for task in self._batch_tasks.get(batch_id, {}).values())

    def _purge_terminal_batches(self) -> None:
        terminal_ids = [batch_id for batch_id, ctx in self._batches.items() if ctx.finalized]
        if not terminal_ids:
            return

        def purgeable(batch_id: str) -> bool:
            return not self._batch_has_subscribers(batch_id) and not self._batch_has_active_tasks(
                batch_id
            )

        to_purge = {
            batch_id
            for batch_id in terminal_ids
            if purgeable(batch_id)
            and ((buffer := self._buffers.get(batch_id)) is None or buffer.is_expired())
        }
        retained_count = len(terminal_ids) - len(to_purge)
        excess = max(0, retained_count - self._max_history_batches)
        if excess:
            oldest_purgeable = sorted(
                (
                    batch_id
                    for batch_id in terminal_ids
                    if batch_id not in to_purge and purgeable(batch_id)
                ),
                key=lambda batch_id: self._batches[batch_id].finished_at or float("inf"),
            )
            to_purge.update(oldest_purgeable[:excess])

        for batch_id in to_purge:
            self._purge_batch_state(batch_id)

    def _purge_batch_state(self, batch_id: str) -> None:
        sender_tasks = tuple(self._subscriber_tasks.pop(batch_id, {}).values())
        for sender in sender_tasks:
            self._track_cancelled_cleanup_task(sender)

        completed_tasks = tuple(self._batch_tasks.pop(batch_id, {}).values())
        for task in completed_tasks:
            if task.done():
                self._consume_task_result(task)

        self._cancel_progress_for_batch(batch_id)
        self._batches.pop(batch_id, None)
        self._buffers.pop(batch_id, None)
        self._subscribers.pop(batch_id, None)
        self._subscriber_queues.pop(batch_id, None)
        self._seq_counters.pop(batch_id, None)
        self._thread_task_ids.pop(batch_id, None)
        self._cancelled.discard(batch_id)
        stale_priorities = [
            priority
            for priority, last_batch_id in self._last_batch_by_priority.items()
            if last_batch_id == batch_id
        ]
        for priority in stale_priorities:
            self._last_batch_by_priority.pop(priority, None)

    async def _emit(self, batch_id: str, event_template: WSEvent) -> None:
        ctx = self._batches.get(batch_id)
        if ctx is not None and ctx.finalized and event_template.event != "BATCH_DONE":
            return
        if batch_id not in self._seq_counters:
            return
        event_template.seq = self._seq_counters[batch_id]
        self._seq_counters[batch_id] += 1
        self._buffers[batch_id].append(event_template)
        for ws in list(self._subscribers.get(batch_id, ())):
            queue = self._subscriber_queues.get(batch_id, {}).get(ws)
            if queue is None:
                self._discard_subscriber(batch_id, ws, cancel_sender=True)
                continue
            try:
                queue.put_nowait(event_template)
            except asyncio.QueueFull:
                self._discard_subscriber(batch_id, ws, cancel_sender=True)
                log.debug("ws_subscriber_queue_full batch_id=%s", batch_id)

    async def cancel_batch(self, batch_id: str) -> dict[str, object]:
        self._purge_terminal_batches()
        ctx = self._batches.get(batch_id)
        if ctx is None:
            return {"batch_id": batch_id, "cancelled": False}
        if ctx.finalized:
            return {"batch_id": batch_id, "cancelled": True}

        self._cancelled.add(batch_id)
        with ctx.phase_lock:
            ctx.cancel_requested = True
            waiting_ids = [
                task_id for task_id, phase in ctx.task_phases.items() if phase == "WAITING"
            ]
        waiting_tasks: list[asyncio.Task[None]] = []
        for task_id in waiting_ids:
            task = self._batch_tasks.get(batch_id, {}).get(task_id)
            if task is not None and not task.done():
                task.cancel()
                waiting_tasks.append(task)
        if waiting_tasks:
            await asyncio.gather(*waiting_tasks, return_exceptions=True)
        for task_id in waiting_ids:
            await self._complete_task(batch_id, task_id, task_id, "CANCELLED")
        await self.retry_batch_persistence(batch_id)
        return {"batch_id": batch_id, "cancelled": True}

    async def delete_task(self, task_id: str) -> dict[str, object]:
        task_record = self._query_orch.repo.get_task(task_id)
        batch_id = task_record.batch_id if task_record is not None else None
        ctx = self._batches.get(batch_id) if batch_id is not None else None
        task_handle: asyncio.Task[None] | None = None
        worker: concurrent.futures.Future[WorkerOutcome] | None = None
        if ctx is not None and batch_id is not None:
            with ctx.phase_lock:
                ctx.externally_cancelled_task_ids.add(task_id)
            task_handle = self._batch_tasks.get(batch_id, {}).get(task_id)
            worker = self._worker_futures.get((batch_id, task_id))
            if worker is not None and not worker.done():
                worker.cancel()
            if task_handle is not None and not task_handle.done():
                task_handle.cancel()

            if worker is not None:
                await self._wait_for_tracked_worker((batch_id, task_id), worker)
            if task_handle is not None:
                await asyncio.gather(task_handle, return_exceptions=True)
            await self._complete_task(batch_id, task_id, task_id, "CANCELLED")

        loop = asyncio.get_running_loop()
        delay = 0.01
        while True:
            result = await loop.run_in_executor(
                _PURGE_EXECUTOR,
                self._query_orch.try_purge,
                task_id,
            )
            if result is not None:
                return result
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.1)

    async def _wait_for_tracked_worker(
        self,
        key: tuple[str, str],
        worker: concurrent.futures.Future[WorkerOutcome],
    ) -> None:
        batch_id, task_id = key
        ctx = self._batches.get(batch_id)
        if ctx is None:
            while not worker.done():
                try:
                    await asyncio.shield(asyncio.wrap_future(worker))
                except asyncio.CancelledError:
                    continue
                except Exception:
                    return
            return
        try:
            await self._await_worker_after_outer_cancel(worker, ctx, task_id)
        except Exception:
            pass

    async def _shutdown_impl(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._accepting_batches = False
            active_batch_ids = [
                batch_id for batch_id, ctx in self._batches.items() if not ctx.finalized
            ]
            task_handles = tuple(
                task for tasks in self._batch_tasks.values() for task in tasks.values()
            )

            if active_batch_ids:
                await asyncio.gather(
                    *(self.cancel_batch(batch_id) for batch_id in active_batch_ids),
                    return_exceptions=True,
                )

            worker_items = tuple(self._worker_futures.items())
            if worker_items:
                await asyncio.gather(
                    *(self._wait_for_tracked_worker(key, worker) for key, worker in worker_items),
                    return_exceptions=True,
                )
            if task_handles:
                await asyncio.gather(*task_handles, return_exceptions=True)

            while self._cleanup_tasks:
                await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)

            for batch_id in active_batch_ids:
                await self.retry_batch_persistence(batch_id)

            retry_tasks = tuple(self._persistence_retry_tasks.values())
            for task in retry_tasks:
                if not task.done():
                    task.cancel()
            if retry_tasks:
                await asyncio.gather(*retry_tasks, return_exceptions=True)
            self._shutdown_complete = True

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_impl(),
                name="scheduler-shutdown",
            )
        outer_cancelled = False
        while not self._shutdown_task.done():
            try:
                await asyncio.shield(self._shutdown_task)
            except asyncio.CancelledError:
                outer_cancelled = True
        await self._shutdown_task
        if outer_cancelled:
            raise asyncio.CancelledError()

    def is_batch_gone(self, batch_id: str) -> bool:
        self._purge_terminal_batches()
        return batch_id not in self._buffers and batch_id not in self._batches

    def replay_events(self, batch_id: str, since: int) -> list[WSEvent]:
        self._purge_terminal_batches()
        buffer = self._buffers.get(batch_id)
        return buffer.replay(since) if buffer else []

    async def _send_subscriber(
        self,
        batch_id: str,
        ws: WebSocketSender,
        replay_events: tuple[WSEvent, ...],
        queue: asyncio.Queue[WSEvent],
        replay_done: asyncio.Event,
    ) -> None:
        try:
            for event in replay_events:
                await ws.send_text(event.model_dump_json())
            replay_done.set()
            while True:
                event = await queue.get()
                await ws.send_text(event.model_dump_json())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("ws_subscriber_send_failed batch_id=%s", batch_id)
        finally:
            replay_done.set()
            self._discard_subscriber(batch_id, ws, cancel_sender=False)

    def add_subscriber(self, batch_id: str, ws: WebSocketSender) -> None:
        self.remove_subscriber(batch_id, ws)
        subscribers = self._subscribers.setdefault(batch_id, set())
        if len(subscribers) >= self._max_subscribers_per_batch:
            raise SchedulerCapacityError(
                "SUBSCRIBER_LIMIT",
                "subscriber limit reached",
            )
        subscribers.add(ws)
        self._subscriber_queues.setdefault(batch_id, {})[ws] = asyncio.Queue(
            maxsize=self._subscriber_queue_max
        )

    def start_subscriber(
        self,
        batch_id: str,
        ws: WebSocketSender,
        replay_events: list[WSEvent],
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        queue = self._subscriber_queues.get(batch_id, {}).get(ws)
        if queue is None:
            raise RuntimeError("subscriber is not registered")
        replay_done = asyncio.Event()
        sender = asyncio.create_task(
            self._send_subscriber(batch_id, ws, tuple(replay_events), queue, replay_done),
            name=f"ws-sender-{batch_id}",
        )
        self._subscriber_tasks.setdefault(batch_id, {})[ws] = sender
        return sender, replay_done

    def _discard_subscriber(
        self,
        batch_id: str,
        ws: WebSocketSender,
        *,
        cancel_sender: bool,
    ) -> asyncio.Task[None] | None:
        self._subscribers.get(batch_id, set()).discard(ws)
        self._subscriber_queues.get(batch_id, {}).pop(ws, None)
        sender = self._subscriber_tasks.get(batch_id, {}).pop(ws, None)
        if cancel_sender and sender is not None and not sender.done():
            sender.cancel()
        return sender

    def remove_subscriber(self, batch_id: str, ws: WebSocketSender) -> asyncio.Task[None] | None:
        return self._discard_subscriber(batch_id, ws, cancel_sender=True)
