import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from parsing_core.serving.api.deps import (
    WS_SESSION_PROTOCOL,
    get_scheduler,
    origin_is_allowed,
    session_token_matches,
    websocket_session_from_protocols,
)
from parsing_core.serving.ws_manager import WsManager

router = APIRouter(tags=["ws"])


@router.websocket("/ws/batch/{batch_id}")
async def ws_batch(websocket: WebSocket, batch_id: str) -> None:
    origins = websocket.headers.getlist("origin")
    if len(origins) != 1 or not origin_is_allowed(origins[0], websocket.app.state.allowed_origins):
        await websocket.accept()
        await websocket.close(code=4403, reason="origin_forbidden")
        return
    protocol_headers = websocket.headers.getlist("sec-websocket-protocol")
    supplied = websocket_session_from_protocols(
        protocol_headers[0] if len(protocol_headers) == 1 else None
    )
    if not session_token_matches(supplied, websocket.app.state.session_token):
        await websocket.accept()
        await websocket.close(code=4401, reason="session_required")
        return

    since = -1
    query_since = websocket.query_params.get("since")
    if query_since is not None:
        try:
            since = int(query_since)
        except ValueError:
            since = -1

    sch = get_scheduler()
    mgr = WsManager(sch)

    await websocket.accept(subprotocol=WS_SESSION_PROTOCOL)
    events = await mgr.replay_and_subscribe(batch_id, websocket, since=since)
    if events is None:
        return

    receive_task: asyncio.Task[str] | None = None
    try:
        try:
            sender_task, replay_done = mgr.start_sender(batch_id, websocket, events)
            await replay_done.wait()
            if sender_task.done():
                await asyncio.gather(sender_task, return_exceptions=True)
                return

            while True:
                receive_task = asyncio.create_task(
                    websocket.receive_text(),
                    name=f"pdf2md-ws-receive-{batch_id}",
                )
                done, _ = await asyncio.wait(
                    {receive_task, sender_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if sender_task in done:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, sender_task, return_exceptions=True)
                    return
                receive_task.result()
        finally:
            if receive_task is not None:
                if not receive_task.done():
                    receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            removed_sender = mgr.unsubscribe(batch_id, websocket)
            if removed_sender is not None:
                await asyncio.gather(removed_sender, return_exceptions=True)
    except WebSocketDisconnect:
        pass
