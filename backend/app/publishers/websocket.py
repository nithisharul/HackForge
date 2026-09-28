"""WebSocket connection manager + publisher for the live dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from app.models.schemas import Alert, Severity
from app.publishers.base import Publisher

log = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks sockets per channel ("alerts", "metrics") and broadcasts to them."""

    def __init__(self) -> None:
        self.channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.channels[channel].add(ws)

    async def disconnect(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self.channels[channel].discard(ws)

    def count(self, channel: str | None = None) -> int:
        if channel:
            return len(self.channels[channel])
        return sum(len(v) for v in self.channels.values())

    async def broadcast(self, channel: str, message: dict) -> int:
        text = json.dumps(message, default=str)
        async with self._lock:
            targets = list(self.channels[channel])
        dead = []
        for ws in targets:
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2.0)
            except Exception:  # slow or closed client: drop it, never block others
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.channels[channel].discard(ws)
        return len(targets) - len(dead)


class WebSocketPublisher(Publisher):
    name = "websocket"
    min_severity = Severity.NONE  # the dashboard shows everything

    def __init__(self, manager: ConnectionManager):
        self.manager = manager

    async def publish(self, alert: Alert) -> None:
        await self.manager.broadcast("alerts", {"type": "alert", "data": alert.model_dump(mode="json")})
