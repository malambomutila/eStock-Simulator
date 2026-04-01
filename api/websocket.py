"""WebSocket bridge from synchronous EventBus to async clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.event_bus import (
    TOPIC_AGENT_RESPONSE,
    TOPIC_ORCHESTRATOR_COMPLETED,
    TOPIC_ORCHESTRATOR_CONFLICT,
    EventBus,
)

logger = logging.getLogger(__name__)

# All topic names we allow via ``?topics=`` (comma-separated).
_SUBSCRIBABLE = frozenset(
    {
        TOPIC_AGENT_RESPONSE,
        TOPIC_ORCHESTRATOR_COMPLETED,
        TOPIC_ORCHESTRATOR_CONFLICT,
    }
)
# Default subscription per Eng 5: agent responses + orchestrator task completion.
_DEFAULT_WANTED = frozenset(
    {
        TOPIC_AGENT_RESPONSE,
        TOPIC_ORCHESTRATOR_COMPLETED,
    }
)


def create_websocket_router(bus: EventBus) -> APIRouter:
    """Router with ``GET``-upgrade WebSocket at ``/ws/events``."""

    router = APIRouter(tags=["websocket"])

    @router.websocket("/ws/events")
    async def stream_events(websocket: WebSocket) -> None:
        await websocket.accept()
        raw = (websocket.query_params.get("topics") or "").strip()
        if raw:
            requested = frozenset(t.strip() for t in raw.split(",") if t.strip())
            wanted = requested & _SUBSCRIBABLE
            if not wanted:
                wanted = _DEFAULT_WANTED
        else:
            wanted = _DEFAULT_WANTED

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(topic: str, payload: dict[str, Any]) -> None:
            if topic not in wanted:
                return

            def _enqueue() -> None:
                try:
                    queue.put_nowait({"topic": topic, "payload": payload})
                except Exception:  # noqa: BLE001
                    logger.debug("WebSocket queue put failed", exc_info=True)

            loop.call_soon_threadsafe(_enqueue)

        for t in wanted:
            bus.subscribe(t, on_event)

        try:
            while True:
                msg = await queue.get()
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            pass
        finally:
            for t in wanted:
                bus.unsubscribe(t, on_event)

    return router
