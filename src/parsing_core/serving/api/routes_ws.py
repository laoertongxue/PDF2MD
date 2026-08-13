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
    if not origin_is_allowed(websocket.headers.get("origin"), websocket.app.state.allowed_origins):
        await websocket.close(code=4403, reason="origin_forbidden")
        return
    supplied = websocket_session_from_protocols(websocket.headers.get("sec-websocket-protocol"))
    if not session_token_matches(supplied, websocket.app.state.session_token):
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
    for ev in events:
        await websocket.send_text(ev.model_dump_json())

    if mgr.scheduler.is_batch_gone(batch_id):
        await websocket.close(code=410, reason="batch gone")
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        mgr.unsubscribe(batch_id, websocket)
