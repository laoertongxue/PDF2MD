from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from parsing_core.serving.api.deps import get_scheduler
from parsing_core.serving.ws_manager import WsManager

router = APIRouter(tags=["ws"])


@router.websocket("/ws/batch/{batch_id}")
async def ws_batch(websocket: WebSocket, batch_id: str):
    since = -1
    query_since = websocket.query_params.get("since")
    if query_since is not None:
        try:
            since = int(query_since)
        except ValueError:
            since = -1

    sch = get_scheduler()
    mgr = WsManager(sch)

    await websocket.accept()
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
