import argparse
import os
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from parsing_core.serving.api.deps import set_scheduler
from parsing_core.serving.api.routes_batches import router as batches_router
from parsing_core.serving.api.routes_tasks import router as tasks_router
from parsing_core.serving.api.routes_ws import router as ws_router
from parsing_core.serving.config import (
    HOST,
    MAX_GLOBAL_CONCURRENCY,
    PORT,
    SERVE_DB_NAME,
    SERVE_FS_DIRNAME,
)
from parsing_core.serving.scheduler import Scheduler


def build_app(
    orch_factory: Callable,
    max_global_concurrency: int = MAX_GLOBAL_CONCURRENCY,
) -> FastAPI:
    app = FastAPI(title="parsing-core-serving")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    sch = Scheduler(orch_factory, max_global_concurrency=max_global_concurrency)
    set_scheduler(sch)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(batches_router)
    app.include_router(tasks_router)
    app.include_router(ws_router)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(prog="parsing-core serve")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--global-concurrency", type=int, default=MAX_GLOBAL_CONCURRENCY)
    parser.add_argument("--parent-pid", type=int, default=None)
    args = parser.parse_args()

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

    import uvicorn

    from parsing_core.llm.stub_client import StubLLMClient
    from parsing_core.orchestrator import Orchestrator
    from parsing_core.storage.fs_layout import FsLayout
    from parsing_core.storage.repository import Repository
    from parsing_core.storage.schema import init_db
    from parsing_core.storage.schema_ext import apply_serve_schema

    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    serve_base = os.path.join(base, SERVE_FS_DIRNAME)
    Path(serve_base).mkdir(parents=True, exist_ok=True)
    db_path = os.path.join(serve_base, SERVE_DB_NAME)

    def orch_factory():
        fs = FsLayout(base_dir=serve_base)
        conn = init_db(db_path)
        apply_serve_schema(conn)
        repo = Repository(conn)
        return Orchestrator(repo=repo, fs=fs, llm=StubLLMClient(), db_path=db_path)

    app = build_app(orch_factory=orch_factory, max_global_concurrency=args.global_concurrency)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
