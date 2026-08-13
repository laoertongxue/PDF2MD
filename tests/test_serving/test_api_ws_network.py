import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosedError

from parsing_core.llm.stub_client import StubLLMClient
from parsing_core.orchestrator import Orchestrator
from parsing_core.serving.api import routes_ws
from parsing_core.serving.serve import build_app
from parsing_core.storage.fs_layout import FsLayout
from parsing_core.storage.repository import Repository
from parsing_core.storage.schema import init_db
from parsing_core.storage.schema_ext import apply_serve_schema

TEST_SESSION_TOKEN = "test-session-token-0123456789abcdef0123456789abcdef"
ALLOWED_ORIGIN = "http://localhost:1420"
WS_PROTOCOLS = ["pdf2md-session-v1", f"pdf2md-session-token.{TEST_SESSION_TOKEN}"]


@contextmanager
def _real_uvicorn(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "serve.db"

    def orch_factory():
        conn = init_db(str(db_path))
        apply_serve_schema(conn)
        return Orchestrator(
            repo=Repository(conn),
            fs=FsLayout(base_dir=str(tmp_path / "data")),
            llm=StubLLMClient(),
            db_path=str(db_path),
        )

    app = build_app(orch_factory=orch_factory, session_token=TEST_SESSION_TOKEN)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", access_log=False))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="test-real-ws-server",
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("real Uvicorn server did not start")
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "real Uvicorn server thread must exit"


async def _closed_code(
    uri: str, *, origin: str, subprotocols: list[str] | None
) -> tuple[int, str | None]:
    async with websockets.connect(
        uri,
        origin=origin,
        subprotocols=subprotocols,
        open_timeout=3,
        close_timeout=1,
    ) as websocket:
        selected = websocket.subprotocol
        with pytest.raises(ConnectionClosedError):
            await asyncio.wait_for(websocket.recv(), timeout=3)
        assert websocket.close_code is not None
        return websocket.close_code, selected


@pytest.mark.parametrize(
    ("origin", "protocols", "expected_code"),
    [
        (ALLOWED_ORIGIN, None, 4401),
        (ALLOWED_ORIGIN, ["pdf2md-session-v1", "pdf2md-session-token.wrong"], 4401),
        ("https://attacker.example", WS_PROTOCOLS, 4403),
    ],
)
def test_real_websocket_auth_closes_with_application_codes_before_scheduler(
    tmp_path, monkeypatch, origin, protocols, expected_code
):
    scheduler_calls: list[bool] = []

    def scheduler_must_not_run():
        scheduler_calls.append(True)
        raise AssertionError("scheduler reached before websocket authentication")

    monkeypatch.setattr(routes_ws, "get_scheduler", scheduler_must_not_run)
    with _real_uvicorn(tmp_path) as base:
        uri = f"{base}/ws/batch/any?since=0"
        assert TEST_SESSION_TOKEN not in uri
        code, selected = asyncio.run(_closed_code(uri, origin=origin, subprotocols=protocols))

    assert code == expected_code
    assert selected is None
    assert not scheduler_calls


def test_real_websocket_missing_batch_uses_valid_custom_close_code(tmp_path):
    with _real_uvicorn(tmp_path) as base:
        uri = f"{base}/ws/batch/missing"
        assert TEST_SESSION_TOKEN not in uri
        code, selected = asyncio.run(
            _closed_code(uri, origin=ALLOWED_ORIGIN, subprotocols=WS_PROTOCOLS)
        )

    assert code == 4410
    assert selected == "pdf2md-session-v1"
