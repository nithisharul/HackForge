"""WebSocket endpoints.

/ws/alerts  -> {"type": "alert", "data": Alert} and {"type": "incident", "data": Incident}
/ws/metrics -> {"type": "metrics", "data": Metrics} every tick

On connect each socket gets a small "hello" so the client can hydrate
immediately instead of waiting for the next tick.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


async def _serve(channel: str, websocket: WebSocket) -> None:
    manager = websocket.app.state.ws_manager
    pipeline = websocket.app.state.pipeline
    await manager.connect(channel, websocket)
    try:
        if channel == "metrics":
            await websocket.send_json({"type": "hello", "data": list(pipeline.metrics_history)[-120:]})
        else:
            recent = await websocket.app.state.store.list_alerts(limit=20)
            await websocket.send_json({"type": "hello", "data": recent})
        while True:
            # we don't expect client messages; this just detects disconnects (and
            # lets clients send "ping" keep-alives through proxies)
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(channel, websocket)


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    await _serve("alerts", websocket)


@router.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket):
    await _serve("metrics", websocket)
