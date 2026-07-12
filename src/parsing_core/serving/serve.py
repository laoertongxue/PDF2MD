import argparse
import ipaddress
import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from parsing_core.serving.api.deps import set_scheduler
from parsing_core.serving.api.routes_batches import router as batches_router
from parsing_core.serving.api.routes_tasks import router as tasks_router
from parsing_core.serving.api.routes_topics import router as topics_router
from parsing_core.serving.api.routes_workbench import router as workbench_router
from parsing_core.serving.api.routes_ws import router as ws_router
from parsing_core.serving.config import (
    HOST,
    MAX_GLOBAL_CONCURRENCY,
    PORT,
    SERVE_DB_NAME,
    SERVE_FS_DIRNAME,
)
from parsing_core.serving.scheduler import Scheduler

DEFAULT_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
]


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
    orch_factory: Callable,
    max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
    health_token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="parsing-core-serving")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    sch = Scheduler(orch_factory, max_global_concurrency=max_global_concurrency)
    set_scheduler(sch)

    @app.get("/health")
    async def health(x_pdf2md_health_token: str | None = Header(default=None)):
        if health_token is not None and x_pdf2md_health_token != health_token:
            raise HTTPException(status_code=403, detail="wrong instance token")
        payload = {"status": "ok"}
        if health_token is not None:
            payload["instance"] = health_token
        return payload

    app.include_router(batches_router)
    app.include_router(tasks_router)
    app.include_router(topics_router)
    app.include_router(workbench_router)
    app.include_router(ws_router)
    return app


def run_uvicorn(app: FastAPI, *, host: str, port: int, socket_fd: int | None = None) -> None:
    import uvicorn

    if socket_fd is not None:
        uvicorn.run(app, fd=socket_fd)
    else:
        uvicorn.run(app, host=host, port=port)


def main() -> int:
    parser = argparse.ArgumentParser(prog="parsing-core serve")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--global-concurrency", type=int, default=MAX_GLOBAL_CONCURRENCY)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--health-token", default=None)
    parser.add_argument("--socket-fd", type=int, default=None)
    args = parser.parse_args()
    require_loopback_host(args.host)

    if args.parent_pid is not None:
        import threading

        def _watchdog():
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

    def orch_factory():
        fs = FsLayout(base_dir=serve_base)
        conn = init_db(db_path)
        apply_serve_schema(conn)
        apply_workbench_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=db_path)

    app = build_app(
        orch_factory=orch_factory,
        max_global_concurrency=args.global_concurrency,
        health_token=args.health_token,
    )
    run_uvicorn(app, host=args.host, port=args.port, socket_fd=args.socket_fd)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
