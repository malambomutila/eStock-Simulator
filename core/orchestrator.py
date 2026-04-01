"""
Central orchestrator: priority queue, role routing, per-drug locks, follow-ups.

Lock behaviour
--------------
When ``AgentMessage.requires_lock`` is true, the orchestrator acquires an
asyncio lock keyed by ``(facility_id, drug_id)``. If ``drug_id`` is absent
from ``payload``, the key falls back to ``(facility_id, "__facility__")`` so
callers still serialize. Waiters **queue** — we do not reject with conflict
events (topic ``TOPIC_ORCHESTRATOR_CONFLICT`` is reserved for future use).

Prescription → dispense chain
------------------------------
On a **successful** ``AgentResponse`` from the doctor with
``action_type == PRESCRIPTION_CREATED`` and a ``result`` dict containing
``prescription_id``, ``drug_id``, ``facility_id``, ``doctor_agent_id``, and
``quantity_prescribed``, the orchestrator enqueues a **follow-up**
``AgentMessage`` to the dispenser with ``DISPENSING_STARTED``,
``requires_lock=True``, and the same ``correlation_id``.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import uuid
from collections import defaultdict
from agents.base_agent import BaseAgent
from config.settings import settings
from core.event_bus import (
    TOPIC_AGENT_RESPONSE,
    TOPIC_ORCHESTRATOR_COMPLETED,
    EventBus,
)
from core.models import ActionType, AgentRole
from core.schemas import AgentMessage, AgentResponse, DispenseRequest

logger = logging.getLogger(__name__)


def _lock_key(message: AgentMessage) -> tuple[str, str] | None:
    if not message.requires_lock:
        return None
    drug_id = (message.payload or {}).get("drug_id")
    fac = message.facility_id
    if drug_id:
        return (fac, str(drug_id))
    return (fac, "__facility__")


def _followup_dispense_message(
    orchestrator_id: str, response: AgentResponse
) -> AgentMessage | None:
    if not response.success or response.action_type != ActionType.PRESCRIPTION_CREATED:
        return None
    r = response.result or {}
    required = (
        "prescription_id",
        "drug_id",
        "facility_id",
        "doctor_agent_id",
        "quantity_prescribed",
    )
    if not all(k in r for k in required):
        logger.debug(
            "Skipping dispense follow-up: missing keys in PRESCRIPTION_CREATED result "
            "(correlation_id=%s)",
            response.correlation_id,
        )
        return None

    dispense_payload = DispenseRequest(
        prescription_id=str(r["prescription_id"]),
        drug_id=str(r["drug_id"]),
        quantity_requested=int(r["quantity_prescribed"]),
        facility_id=str(r["facility_id"]),
        doctor_agent_id=str(r["doctor_agent_id"]),
        correlation_id=response.correlation_id,
    ).model_dump(mode="json")

    return AgentMessage(
        sender_role=AgentRole.ORCHESTRATOR,
        sender_id=orchestrator_id,
        recipient_role=AgentRole.DISPENSER,
        action_type=ActionType.DISPENSING_STARTED,
        facility_id=str(r["facility_id"]),
        payload=dispense_payload,
        priority=9,
        requires_lock=True,
        correlation_id=response.correlation_id,
    )


class Orchestrator:
    """
    Routes ``AgentMessage`` instances to registered agents.

    Higher ``priority`` values are processed first (schema: 1 = lowest, 10 = highest).
    FIFO order is preserved among messages with the same priority.
    """

    __slots__ = (
        "_agents",
        "_bus",
        "_heap",
        "_orchestrator_id",
        "_seq",
        "_stock_locks",
    )

    def __init__(
        self,
        bus: EventBus | None = None,
        orchestrator_id: str | None = None,
    ) -> None:
        self._bus = bus or EventBus()
        self._agents: dict[AgentRole, BaseAgent] = {}
        self._heap: list[tuple[int, int, AgentMessage]] = []
        self._seq = 0
        self._orchestrator_id = orchestrator_id or f"orchestrator-{uuid.uuid4().hex[:12]}"
        self._stock_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def orchestrator_id(self) -> str:
        return self._orchestrator_id

    def register(self, role: AgentRole, agent: BaseAgent) -> None:
        if role == AgentRole.ORCHESTRATOR:
            raise ValueError("Cannot register orchestrator role as agent handler.")
        self._agents[role] = agent

    def unregister(self, role: AgentRole) -> None:
        self._agents.pop(role, None)

    def submit(self, message: AgentMessage) -> None:
        """Enqueue by priority (higher ``priority`` first)."""
        self._seq += 1
        heapq.heappush(self._heap, (-message.priority, self._seq, message))

    def _synthetic_unroutable(self, message: AgentMessage, detail: str) -> AgentResponse:
        return AgentResponse(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            success=False,
            action_type=ActionType.SYSTEM_ERROR,
            agent_id=self._orchestrator_id,
            agent_role=AgentRole.ORCHESTRATOR,
            error=detail,
            result={"code": "UNROUTABLE"},
        )

    async def _invoke_agent_with_resilience(
        self, message: AgentMessage
    ) -> AgentResponse:
        role = message.recipient_role
        if role is None:
            return self._synthetic_unroutable(message, "recipient_role is required.")
        agent = self._agents.get(role)
        if agent is None:
            return self._synthetic_unroutable(
                message, f"No agent registered for role {role!s}."
            )

        timeout = settings.orchestrator_agent_timeout_seconds
        max_retries = settings.orchestrator_max_retries
        last_exc: BaseException | None = None

        for attempt in range(max_retries + 1):
            try:
                resp = await asyncio.wait_for(agent.handle(message), timeout=timeout)
                self._bus.publish(
                    TOPIC_AGENT_RESPONSE,
                    {
                        "message": message.model_dump(mode="json"),
                        "response": resp.model_dump(mode="json"),
                        "attempt": attempt,
                    },
                )
                self._bus.publish(
                    TOPIC_ORCHESTRATOR_COMPLETED,
                    {
                        "message": message.model_dump(mode="json"),
                        "response": resp.model_dump(mode="json"),
                        "attempt": attempt,
                    },
                )
                return resp
            except asyncio.TimeoutError:
                last_exc = None
                logger.warning(
                    "Agent timeout role=%s correlation_id=%s attempt=%s/%s",
                    role,
                    message.correlation_id,
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt >= max_retries:
                    return AgentResponse(
                        message_id=message.message_id,
                        correlation_id=message.correlation_id,
                        success=False,
                        action_type=ActionType.SYSTEM_ERROR,
                        agent_id=self._orchestrator_id,
                        agent_role=AgentRole.ORCHESTRATOR,
                        error=(
                            f"Agent timed out after {timeout}s "
                            f"({max_retries + 1} attempt(s))."
                        ),
                        result={"code": "TIMEOUT", "role": str(role)},
                    )
            except Exception as exc:
                last_exc = exc
                logger.exception(
                    "Agent error role=%s correlation_id=%s",
                    role,
                    message.correlation_id,
                )
                break

        err_msg = (
            f"{type(last_exc).__name__}: {last_exc}"
            if last_exc
            else "Unknown agent failure"
        )
        return AgentResponse(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            success=False,
            action_type=ActionType.SYSTEM_ERROR,
            agent_id=self._orchestrator_id,
            agent_role=AgentRole.ORCHESTRATOR,
            error=err_msg,
            result={"code": "AGENT_EXCEPTION"},
        )

    async def dispatch_immediate(self, message: AgentMessage) -> AgentResponse:
        """
        Run one message immediately (not via the internal heap), then enqueue any
        prescription→dispense follow-up.
        """
        key = _lock_key(message)
        if key:
            async with self._stock_locks[key]:
                resp = await self._invoke_agent_with_resilience(message)
        else:
            resp = await self._invoke_agent_with_resilience(message)

        follow = _followup_dispense_message(self._orchestrator_id, resp)
        if follow:
            self.submit(follow)
        return resp

    async def process_next(self) -> AgentResponse | None:
        if not self._heap:
            return None
        _, _, message = heapq.heappop(self._heap)
        return await self.dispatch_immediate(message)

    async def run_until_idle(self, max_messages: int = 100) -> list[AgentResponse]:
        """Drain the priority queue (including follow-ups submitted during dispatch)."""
        out: list[AgentResponse] = []
        for _ in range(max_messages):
            if not self._heap:
                break
            r = await self.process_next()
            if r is not None:
                out.append(r)
        return out


def build_default_orchestrator(
    *,
    use_simulation_doctor: bool = False,
    bus: EventBus | None = None,
) -> Orchestrator:
    """
    Factory: registers dispenser + stubs + optional simulation doctor (demo only).

    Eng 3/4 replace stubs with real personas; keep imports lazy to avoid cycles.
    """
    from agents.dispenser_agent import DispenserAgent
    from agents.stub_agents import (
        PlaceholderGovAgent,
        PlaceholderStockManagerAgent,
        PlaceholderDoctorAgent,
    )

    orch = Orchestrator(bus=bus)
    orch.register(AgentRole.DISPENSER, DispenserAgent())

    if use_simulation_doctor:
        from agents.simulation_doctor import SimulationDoctorAgent

        orch.register(AgentRole.DOCTOR, SimulationDoctorAgent())
    else:
        orch.register(AgentRole.DOCTOR, PlaceholderDoctorAgent())

    orch.register(AgentRole.STOCK_MANAGER, PlaceholderStockManagerAgent())
    orch.register(AgentRole.GOV_OFFICIAL, PlaceholderGovAgent())
    return orch
