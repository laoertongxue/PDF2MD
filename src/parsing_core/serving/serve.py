import argparse
import inspect
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from parsing_core.serving.api.deps import (
    SESSION_ENV,
    SESSION_HEADER,
    require_health_session,
    require_local_session,
    session_token_matches,
    set_scheduler,
    validate_session_token,
)
from parsing_core.serving.api.routes_batches import router as batches_router
from parsing_core.serving.api.routes_tasks import router as tasks_router
from parsing_core.serving.api.routes_topics import router as topics_router
from parsing_core.serving.api.routes_workbench import router as workbench_router
from parsing_core.serving.api.routes_ws import router as ws_router
from parsing_core.serving.config import (
    HOST,
    MAX_GLOBAL_CONCURRENCY,
    SERVE_DB_NAME,
    SERVE_FS_DIRNAME,
)
from parsing_core.serving.scheduler import OrchestratorFactory, Scheduler

DEFAULT_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
MAX_REQUEST_BODY_BYTES = 1_048_576
RECOVERY_BATCH_SIZE = 100
READY_SCHEMA = "pdf2md.sidecar.ready.v1"


class ShutdownController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request: Callable[[], None] | None = None
        self._requested = False

    def bind(self, request: Callable[[], None]) -> None:
        with self._lock:
            self._request = request
            requested = self._requested
        if requested:
            request()

    def request(self) -> None:
        with self._lock:
            self._requested = True
            request = self._request
        if request is not None:
            request()


class LocalApiSecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: set[str],
        session_token: str,
        max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self.app = app
        self.allowed_origins = allowed_origins
        self.session_token = session_token
        self.max_request_body_bytes = max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return

        headers: dict[str, list[str]] = {}
        for raw_key, raw_value in scope.get("headers", []):
            key = raw_key.decode("latin-1").lower()
            headers.setdefault(key, []).append(raw_value.decode("latin-1"))

        origin_values = headers.get("origin", [])
        session_values = headers.get(SESSION_HEADER.lower(), [])
        content_length_values = headers.get("content-length", [])
        request_method_values = headers.get("access-control-request-method", [])
        if (
            len(origin_values) > 1
            or len(session_values) > 1
            or len(content_length_values) > 1
            or len(request_method_values) > 1
        ):
            await self._reject(scope, receive, send, 400, "invalid_request")
            return

        origin = origin_values[0] if origin_values else None
        if origin not in self.allowed_origins:
            await self._reject(scope, receive, send, 403, "origin_forbidden")
            return

        is_preflight = scope.get("method") == "OPTIONS" and len(request_method_values) == 1
        if not is_preflight and not session_token_matches(
            session_values[0] if session_values else "", self.session_token
        ):
            await self._reject(scope, receive, send, 401, "session_required", allowed_origin=origin)
            return

        if content_length_values:
            try:
                if int(content_length_values[0]) > self.max_request_body_bytes:
                    await self._reject(
                        scope, receive, send, 413, "request_too_large", allowed_origin=origin
                    )
                    return
            except ValueError:
                await self._reject(
                    scope, receive, send, 400, "invalid_request", allowed_origin=origin
                )
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_body_bytes:
                await self._reject(
                    scope, receive, send, 413, "request_too_large", allowed_origin=origin
                )
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        receive = replay_body

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
        *,
        allowed_origin: str | None = None,
    ) -> None:
        headers = None
        if allowed_origin is not None:
            headers = {"Access-Control-Allow-Origin": allowed_origin, "Vary": "Origin"}
        response = JSONResponse(
            status_code=status_code,
            content={"detail": {"code": code}},
            headers=headers,
        )
        await response(scope, receive, send)


def require_loopback_host(host: str) -> str:
    if host == "localhost":
        return host
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("local API host must be loopback")


def _require_loopback_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("CORS origin must use HTTP on a loopback host")
    require_loopback_host(parsed.hostname)
    return origin


def allowed_cors_origins() -> list[str]:
    extra = os.environ.get("PARSING_CORE_CORS_ORIGINS", "")
    configured = [origin.strip() for origin in extra.split(",") if origin.strip()]
    return DEFAULT_CORS_ORIGINS + [_require_loopback_origin(origin) for origin in configured]


def request_process_shutdown() -> None:
    import signal

    os.kill(os.getpid(), signal.SIGTERM)


def build_app(
    orch_factory: Callable[[], object],
    session_token: str,
    max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    shutdown_hook: Callable[[], None | Awaitable[None]] | None = None,
    shutdown_request: Callable[[], None | Awaitable[None]] | None = None,
) -> FastAPI:
    session_token = validate_session_token(session_token)
    sch = Scheduler(
        cast(OrchestratorFactory, orch_factory),
        max_global_concurrency=max_global_concurrency,
    )

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if lifespan is None:
                yield
            else:
                async with lifespan(app):
                    yield
        finally:
            await sch.shutdown()
            if shutdown_hook is not None:
                result = shutdown_hook()
                if inspect.isawaitable(result):
                    await result

    app = FastAPI(
        title="parsing-core-serving",
        lifespan=combined_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    origins = allowed_cors_origins()
    app.state.session_token = session_token
    app.state.allowed_origins = frozenset(origins)
    app.state.scheduler = sch
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["Accept", "Content-Type", SESSION_HEADER],
    )
    app.add_middleware(
        LocalApiSecurityMiddleware,
        allowed_origins=set(origins),
        session_token=session_token,
    )

    set_scheduler(sch)

    @app.get("/health", dependencies=[Depends(require_health_session)])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/shutdown", dependencies=[Depends(require_health_session)])
    async def shutdown(background_tasks: BackgroundTasks) -> dict[str, str]:
        background_tasks.add_task(shutdown_request or request_process_shutdown)
        return {"status": "shutting_down"}

    authenticated = [Depends(require_local_session)]
    app.include_router(batches_router, dependencies=authenticated)
    app.include_router(tasks_router, dependencies=authenticated)
    app.include_router(topics_router, dependencies=authenticated)
    app.include_router(workbench_router, dependencies=authenticated)
    app.include_router(ws_router)
    return app


def recover_interrupted_work(
    db_path: Path,
    temp_dir: Path,
    *,
    resume_chapter_sync: bool = False,
) -> None:
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            task_columns = (
                {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
                if "tasks" in tables
                else set()
            )
            interruption_message = "recoverable: interrupted by service shutdown"
            if "batches" in tables and "batch_id" in task_columns:
                conn.execute(
                    "UPDATE tasks SET status = 'INTERRUPTED', error_msg = ?, "
                    "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                    "WHERE batch_id IN (SELECT id FROM batches WHERE status = 'RUNNING') "
                    "AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED')",
                    (interruption_message,),
                )
                conn.execute(
                    "UPDATE batches SET status = 'INTERRUPTED', "
                    "finished_at = COALESCE("
                    "finished_at, CAST(strftime('%s', 'now') AS INTEGER)) "
                    "WHERE status = 'RUNNING'"
                )
                conn.execute(
                    "UPDATE tasks SET status = 'INTERRUPTED', error_msg = ?, "
                    "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                    "WHERE status = 'RUNNING'",
                    (interruption_message,),
                )
            elif "tasks" in tables:
                conn.execute(
                    "UPDATE tasks SET status = 'INTERRUPTED', error_msg = ?, "
                    "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                    "WHERE status = 'RUNNING'",
                    (interruption_message,),
                )
            if {
                "wb_chapters",
                "wb_chapter_generation_runs",
                "wb_chapter_generation_leases",
            } <= tables:
                conn.execute(
                    "UPDATE wb_chapter_generation_runs SET status = 'FAILED', "
                    "error = 'chapter generation interrupted', "
                    "error_code = 'CHAPTER_GENERATION_INTERRUPTED', "
                    "error_message = 'chapter generation interrupted', "
                    "finished_at = CAST(strftime('%s', 'now') AS INTEGER) "
                    "WHERE status = 'RUNNING'"
                )
                conn.execute(
                    "UPDATE wb_chapters SET status = 'FAILED', "
                    "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                    "WHERE status = 'RUNNING'"
                )
                conn.execute("DELETE FROM wb_chapter_generation_leases")
            conn.commit()
        finally:
            conn.close()
        if resume_chapter_sync:
            _recover_pending_chapter_publications(db_path)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _recover_pending_chapter_publications(db_path: Path) -> None:
    from parsing_core.workbench.markdown_sync import recover_chapter_publication_journals
    from parsing_core.workbench.pipeline import recover_pending_chapter_markdown_sync
    from parsing_core.workbench.repository import WorkbenchRepository
    from parsing_core.workbench.topic_markdown_sync import recover_topic_publication_journals

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    repo = WorkbenchRepository(conn)
    try:
        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT chapter_id FROM wb_chapter_generation_publications "
                "WHERE status = 'SYNC_PENDING' AND chapter_id > ? "
                "ORDER BY chapter_id LIMIT ?",
                (cursor, RECOVERY_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1][0]
            for row in rows:
                chapter_id = row[0]
                try:
                    recover_pending_chapter_markdown_sync(repo, chapter_id)
                except Exception:
                    conn.execute(
                        "UPDATE wb_chapter_generation_publications "
                        "SET error = 'chapter Markdown publication failed', "
                        "error_code = 'CHAPTER_MARKDOWN_PUBLICATION_FAILED', "
                        "error_message = 'chapter Markdown publication failed', "
                        "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                        "WHERE chapter_id = ? AND status = 'SYNC_PENDING'",
                        (chapter_id,),
                    )
                    conn.commit()

        cursor_type = ""
        cursor_id = ""
        while True:
            rows = conn.execute(
                "SELECT entity_type, entity_id, publication_id "
                "FROM wb_markdown_publications "
                "WHERE entity_type > ? OR (entity_type = ? AND entity_id > ?) "
                "ORDER BY entity_type, entity_id LIMIT ?",
                (cursor_type, cursor_type, cursor_id, RECOVERY_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            cursor_type, cursor_id = rows[-1][0], rows[-1][1]
            for entity_type, entity_id, publication_id in rows:
                try:
                    if entity_type == "chapter":
                        recover_chapter_publication_journals(repo, entity_id)
                    else:
                        recover_topic_publication_journals(repo, entity_id)
                except Exception:
                    conn.execute(
                        "UPDATE wb_markdown_publications "
                        "SET error_code = 'MARKDOWN_PUBLICATION_RECOVERY_FAILED', "
                        "error_message = 'Markdown publication recovery failed', "
                        "updated_at = CAST(strftime('%s', 'now') AS INTEGER) "
                        "WHERE entity_type = ? AND entity_id = ? AND publication_id = ?",
                        (entity_type, entity_id, publication_id),
                    )
                    conn.commit()
                    continue
                conn.execute(
                    "DELETE FROM wb_markdown_publications "
                    "WHERE entity_type = ? AND entity_id = ? AND publication_id = ?",
                    (entity_type, entity_id, publication_id),
                )
                conn.commit()

        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT chapter_id FROM wb_chapter_publication_receipts "
                "WHERE chapter_id > ? ORDER BY chapter_id LIMIT ?",
                (cursor, RECOVERY_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1][0]
            for row in rows:
                try:
                    recover_chapter_publication_journals(repo, row[0])
                except Exception:
                    continue

        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT entity_id FROM wb_markdown_publication_receipts "
                "WHERE entity_type = 'chapter' AND entity_id > ? "
                "ORDER BY entity_id LIMIT ?",
                (cursor, RECOVERY_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1][0]
            for row in rows:
                try:
                    recover_chapter_publication_journals(repo, row[0])
                except Exception:
                    continue

        cursor = ""
        while True:
            rows = conn.execute(
                "SELECT entity_id FROM wb_markdown_publication_receipts "
                "WHERE entity_type = 'topic' AND entity_id > ? "
                "ORDER BY entity_id LIMIT ?",
                (cursor, RECOVERY_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1][0]
            for row in rows:
                try:
                    recover_topic_publication_journals(repo, row[0])
                except Exception:
                    continue
    finally:
        conn.close()


def run_uvicorn(
    app: FastAPI,
    *,
    host: str,
    port: int,
    socket_fd: int | None = None,
    shutdown_controller: ShutdownController | None = None,
) -> None:
    import uvicorn

    if shutdown_controller is not None:
        config = uvicorn.Config(app, fd=socket_fd, host=host, port=port)
        server = uvicorn.Server(config)
        shutdown_controller.bind(lambda: setattr(server, "should_exit", True))
        server.run()
        return
    if socket_fd is not None:
        uvicorn.run(app, fd=socket_fd)
    else:
        uvicorn.run(app, host=host, port=port)


def session_token_from_environment() -> str:
    token = os.environ.pop(SESSION_ENV, "")
    if not token:
        raise RuntimeError("session token is required")
    try:
        return validate_session_token(token)
    except ValueError as exc:
        raise RuntimeError("session token does not meet security requirements") from exc


def session_token_from_fd(fd: int) -> str:
    if fd <= 2:
        raise RuntimeError("session token file descriptor is invalid")
    token_bytes = bytearray()
    try:
        while len(token_bytes) <= 1024:
            chunk = os.read(fd, min(256, 1025 - len(token_bytes)))
            if not chunk:
                break
            token_bytes.extend(chunk)
    finally:
        os.close(fd)
    if not token_bytes or len(token_bytes) > 1024:
        raise RuntimeError("session token channel is invalid")
    try:
        token = token_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("session token channel is invalid") from exc
    token_bytes[:] = b"\0" * len(token_bytes)
    try:
        return validate_session_token(token)
    except ValueError as exc:
        raise RuntimeError("session token does not meet security requirements") from exc


def build_ready_payload(host: str, port: int) -> dict[str, str | int]:
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise ValueError("sidecar ready endpoint must be IPv4 loopback")
    return {"schema": READY_SCHEMA, "host": host, "port": port}


def _endpoint_from_socket_fd(socket_fd: int) -> tuple[str, int]:
    duplicate = socket.fromfd(socket_fd, socket.AF_INET, socket.SOCK_STREAM)
    try:
        host, port = duplicate.getsockname()[:2]
    finally:
        duplicate.close()
    return str(host), int(port)


def _bind_loopback_socket(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen()
        return listener
    except Exception:
        listener.close()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(prog="parsing-core serve")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--global-concurrency", type=int, default=MAX_GLOBAL_CONCURRENCY)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--socket-fd", type=int, default=None)
    parser.add_argument("--session-token-fd", type=int, required=True)
    args = parser.parse_args()
    require_loopback_host(args.host)
    session_token = session_token_from_fd(args.session_token_fd)

    from parsing_core.llm.stub_client import StubLLMClient
    from parsing_core.orchestrator import Orchestrator
    from parsing_core.storage.fs_layout import FsLayout
    from parsing_core.storage.repository import Repository
    from parsing_core.storage.schema import init_db
    from parsing_core.storage.schema_ext import apply_serve_schema
    from parsing_core.workbench.schema import apply_workbench_schema

    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    serve_base = os.path.join(base, SERVE_FS_DIRNAME)
    Path(serve_base).mkdir(parents=True, exist_ok=True)
    db_path = os.path.join(serve_base, SERVE_DB_NAME)
    temp_dir = Path(serve_base) / "tmp"
    temp_dir.mkdir(exist_ok=True)

    def orch_factory() -> Orchestrator:
        fs = FsLayout(base_dir=serve_base)
        conn = init_db(db_path)
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=db_path)

    bootstrap_conn = init_db(db_path)
    apply_serve_schema(bootstrap_conn)
    apply_workbench_schema(bootstrap_conn)
    bootstrap_conn.close()
    recover_interrupted_work(
        Path(db_path),
        temp_dir,
        resume_chapter_sync=True,
    )

    shutdown_controller = ShutdownController()
    app = build_app(
        orch_factory=orch_factory,
        session_token=session_token,
        max_global_concurrency=args.global_concurrency,
        shutdown_hook=lambda: recover_interrupted_work(Path(db_path), temp_dir),
        shutdown_request=shutdown_controller.request,
    )
    owned_listener: socket.socket | None = None
    socket_fd = args.socket_fd
    if socket_fd is None:
        owned_listener = _bind_loopback_socket(args.host, args.port)
        socket_fd = owned_listener.fileno()
    host, port = _endpoint_from_socket_fd(socket_fd)
    payload = build_ready_payload(host, port)
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    try:
        run_uvicorn(
            app,
            host=host,
            port=port,
            socket_fd=socket_fd,
            shutdown_controller=shutdown_controller,
        )
    finally:
        if owned_listener is not None:
            owned_listener.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
