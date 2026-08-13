import argparse
import inspect
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI
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
from parsing_core.serving.scheduler import Scheduler

DEFAULT_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
MAX_REQUEST_BODY_BYTES = 1_048_576
READY_SCHEMA = "pdf2md.sidecar.ready.v1"


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

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        origin = headers.get("origin")
        if origin not in self.allowed_origins:
            await self._reject(scope, receive, send, 403, "origin_forbidden")
            return

        is_preflight = scope.get("method") == "OPTIONS"
        if not is_preflight and not session_token_matches(
            headers.get(SESSION_HEADER.lower(), ""), self.session_token
        ):
            await self._reject(scope, receive, send, 401, "session_required", allowed_origin=origin)
            return

        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_request_body_bytes:
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


def build_app(
    orch_factory: Callable[[], object],
    session_token: str,
    max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    shutdown_hook: Callable[[], None | Awaitable[None]] | None = None,
) -> FastAPI:
    session_token = validate_session_token(session_token)

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if lifespan is None:
                yield
            else:
                async with lifespan(app):
                    yield
        finally:
            if shutdown_hook is not None:
                result = shutdown_hook()
                if inspect.isawaitable(result):
                    await result

    app = FastAPI(title="parsing-core-serving", lifespan=combined_lifespan)
    origins = allowed_cors_origins()
    app.state.session_token = session_token
    app.state.allowed_origins = frozenset(origins)
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

    sch = Scheduler(orch_factory, max_global_concurrency=max_global_concurrency)
    set_scheduler(sch)

    @app.get("/health", dependencies=[Depends(require_health_session)])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    authenticated = [Depends(require_local_session)]
    app.include_router(batches_router, dependencies=authenticated)
    app.include_router(tasks_router, dependencies=authenticated)
    app.include_router(topics_router, dependencies=authenticated)
    app.include_router(workbench_router, dependencies=authenticated)
    app.include_router(ws_router)
    return app


def recover_interrupted_work(db_path: Path, temp_dir: Path) -> None:
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE tasks SET status = 'INTERRUPTED', error_msg = ? WHERE status = 'RUNNING'",
                ("recoverable: interrupted by service shutdown",),
            )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if {
                "wb_chapters",
                "wb_chapter_generation_runs",
                "wb_chapter_generation_leases",
            } <= tables:
                conn.execute(
                    "UPDATE wb_chapter_generation_runs SET status = 'FAILED', "
                    "error = 'interrupted', finished_at = CAST(strftime('%s', 'now') AS INTEGER) "
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
    shutil.rmtree(temp_dir, ignore_errors=True)


def run_uvicorn(app: FastAPI, *, host: str, port: int, socket_fd: int | None = None) -> None:
    import uvicorn

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
    args = parser.parse_args()
    require_loopback_host(args.host)
    session_token = session_token_from_environment()

    if args.parent_pid is not None:
        import threading

        def _watchdog() -> None:
            import os as _os
            import signal
            import time as _t

            pid = args.parent_pid
            while True:
                try:
                    _os.kill(pid, 0)
                except OSError:
                    _os.kill(_os.getpid(), signal.SIGTERM)
                    return
                _t.sleep(3)

        threading.Thread(target=_watchdog, daemon=True, name="parent-watchdog").start()

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
    recover_interrupted_work(Path(db_path), temp_dir)

    app = build_app(
        orch_factory=orch_factory,
        session_token=session_token,
        max_global_concurrency=args.global_concurrency,
        shutdown_hook=lambda: recover_interrupted_work(Path(db_path), temp_dir),
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
        run_uvicorn(app, host=host, port=port, socket_fd=socket_fd)
    finally:
        if owned_listener is not None:
            owned_listener.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
