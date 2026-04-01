"""
In-process pub/sub for orchestrator and dashboard integration.

Topics are stable strings so Eng 5 can bridge the same payloads to WebSocket
or REST without changing agent contracts.

Topics
------
``estock.agent.response``
    Emitted after every routed message; payload includes ``message`` and
    ``response`` (JSON-serialisable dicts).

``estock.orchestrator.task.completed``
    Emitted when a task finishes; payload includes ``message``, ``response``,
    and ``attempt`` (int).

``estock.orchestrator.conflict``
    Reserved for rejected or skipped work due to contention; currently unused
    because stock locks **serialize** waiters instead of failing.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

TopicHandler = Callable[[str, dict[str, Any]], None]

# Public topic constants (stable API for Eng 5)
TOPIC_AGENT_RESPONSE = "estock.agent.response"
TOPIC_ORCHESTRATOR_COMPLETED = "estock.orchestrator.task.completed"
TOPIC_ORCHESTRATOR_CONFLICT = "estock.orchestrator.conflict"


class EventBus:
    """Thread-safe synchronous dispatch; handlers must be non-blocking or fast."""

    __slots__ = ("_subs",)

    def __init__(self) -> None:
        self._subs: dict[str, list[TopicHandler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: TopicHandler) -> None:
        if handler not in self._subs[topic]:
            self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: TopicHandler) -> None:
        handlers = self._subs.get(topic)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for handler in list(self._subs.get(topic, [])):
            handler(topic, payload)
