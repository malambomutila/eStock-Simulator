from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from agents.base_agent import BaseAgent
from config.settings import settings
from core.database import get_db
from core.event_bus import TOPIC_AGENT_RESPONSE, EventBus
from core.models import ActionType, AgentRole, Prescription, PrescriptionStatus, StockLevel
from core.orchestrator import Orchestrator, _followup_dispense_message, build_default_orchestrator
from core.schemas import AgentMessage, AgentResponse


class EchoAgent(BaseAgent):
    def __init__(self, role: AgentRole, **kwargs):
        super().__init__(**kwargs)
        self.role = role

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return self._ok(
            message,
            message.action_type,
            {"echo": message.message_id},
        )


class SlowDispenser(BaseAgent):
    """Serialisation witness — records order while ``asyncio.sleep`` runs."""

    role = AgentRole.DISPENSER

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def handle(self, message: AgentMessage) -> AgentResponse:
        mid = message.message_id
        self.events.append(f"start:{mid}")
        await asyncio.sleep(0.06)
        self.events.append(f"end:{mid}")
        return self._ok(message, ActionType.DISPENSING_COMPLETE, {"mid": mid})


class HangAgent(BaseAgent):
    role = AgentRole.DISPENSER

    async def handle(self, message: AgentMessage) -> AgentResponse:
        await asyncio.sleep(3600)
        return self._ok(message, ActionType.DISPENSING_COMPLETE, {})


def test_followup_dispense_message_builds_dispenser_envelope():
    resp = AgentResponse(
        message_id="m1",
        correlation_id="corr-1",
        success=True,
        action_type=ActionType.PRESCRIPTION_CREATED,
        agent_id="doc",
        agent_role=AgentRole.DOCTOR,
        result={
            "prescription_id": "rx-1",
            "drug_id": "d1",
            "facility_id": "FAC-1",
            "doctor_agent_id": "doc-a",
            "quantity_prescribed": 3,
        },
    )
    follow = _followup_dispense_message("orch-test", resp)
    assert follow is not None
    assert follow.recipient_role == AgentRole.DISPENSER
    assert follow.action_type == ActionType.DISPENSING_STARTED
    assert follow.requires_lock is True
    assert follow.correlation_id == "corr-1"
    assert follow.payload["quantity_requested"] == 3
    assert follow.payload["prescription_id"] == "rx-1"


def test_followup_skips_on_missing_keys():
    resp = AgentResponse(
        message_id="m1",
        correlation_id="c",
        success=True,
        action_type=ActionType.PRESCRIPTION_CREATED,
        agent_id="doc",
        agent_role=AgentRole.DOCTOR,
        result={"prescription_id": "rx-1"},
    )
    assert _followup_dispense_message("o", resp) is None


def test_followup_merges_facility_and_doctor_from_eng3_style_result():
    """DoctorAgent omits facility_id / doctor_agent_id in result — orchestrator fills them."""
    resp = AgentResponse(
        message_id="m1",
        correlation_id="corr-eng3",
        success=True,
        action_type=ActionType.PRESCRIPTION_CREATED,
        agent_id="DoctorAgent-deadbeef",
        agent_role=AgentRole.DOCTOR,
        result={
            "prescription_id": "rx-1",
            "drug_id": "d1",
            "quantity_prescribed": 2,
        },
    )
    src = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sys",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id="FACILITY-ENG3",
        payload={},
    )
    follow = _followup_dispense_message("orch", resp, source_message=src)
    assert follow is not None
    assert follow.payload["facility_id"] == "FACILITY-ENG3"
    assert follow.payload["doctor_agent_id"] == "DoctorAgent-deadbeef"


@pytest.mark.asyncio
async def test_dispatch_unknown_role_returns_orchestrator_system_error():
    bus = EventBus()
    orch = Orchestrator(bus=bus)
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="t",
        recipient_role=AgentRole.DISPENSER,
        action_type=ActionType.DISPENSING_STARTED,
        facility_id="F1",
        payload={"drug_id": "d1"},
    )
    resp = await orch.dispatch_immediate(msg)
    assert resp.success is False
    assert resp.agent_role == AgentRole.ORCHESTRATOR
    assert "No agent registered" in (resp.error or "")


@pytest.mark.asyncio
async def test_priority_order_high_first():
    orch = Orchestrator()
    seen: list[str] = []

    class TagAgent(BaseAgent):
        role = AgentRole.DOCTOR

        async def handle(self, message: AgentMessage) -> AgentResponse:
            seen.append(message.payload["tag"])
            return self._ok(message, message.action_type, {})

    orch.register(AgentRole.DOCTOR, TagAgent())

    orch.submit(
        AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="s",
            recipient_role=AgentRole.DOCTOR,
            action_type=ActionType.AVAILABILITY_CHECKED,
            facility_id="F",
            payload={"tag": "low"},
            priority=2,
        )
    )
    orch.submit(
        AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="s",
            recipient_role=AgentRole.DOCTOR,
            action_type=ActionType.AVAILABILITY_CHECKED,
            facility_id="F",
            payload={"tag": "high"},
            priority=9,
        )
    )
    await orch.run_until_idle(max_messages=10)
    assert seen == ["high", "low"]


@pytest.mark.asyncio
async def test_lock_serializes_same_drug():
    orch = Orchestrator()
    slow = SlowDispenser()
    orch.register(AgentRole.DISPENSER, slow)

    def make_msg(mid_suffix: str) -> AgentMessage:
        return AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="s",
            recipient_role=AgentRole.DISPENSER,
            action_type=ActionType.DISPENSING_STARTED,
            facility_id="FAC-X",
            payload={"drug_id": "drug-1"},
            requires_lock=True,
            message_id=f"m-{mid_suffix}",
        )

    await asyncio.gather(
        orch.dispatch_immediate(make_msg("a")),
        orch.dispatch_immediate(make_msg("b")),
    )
    ev = slow.events
    ia_s, ia_e = ev.index("start:m-a"), ev.index("end:m-a")
    ib_s, ib_e = ev.index("start:m-b"), ev.index("end:m-b")
    assert ia_e > ia_s and ib_e > ib_s
    # Same-drug lock: one full dispense finishes before the other starts
    assert ia_e < ib_s or ib_e < ia_s


@pytest.mark.asyncio
async def test_event_bus_receives_publish():
    bus = EventBus()
    hits: list[str] = []

    def on_topic(topic: str, payload: dict) -> None:
        hits.append(topic)

    bus.subscribe(TOPIC_AGENT_RESPONSE, on_topic)
    orch = Orchestrator(bus=bus)
    orch.register(AgentRole.DOCTOR, EchoAgent(AgentRole.DOCTOR))
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="s",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.AVAILABILITY_CHECKED,
        facility_id="F",
        payload={},
    )
    await orch.dispatch_immediate(msg)
    assert TOPIC_AGENT_RESPONSE in hits


@pytest.mark.asyncio
async def test_timeout_returns_system_error(monkeypatch):
    monkeypatch.setattr(settings, "orchestrator_agent_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "orchestrator_max_retries", 0)
    orch = Orchestrator()
    orch.register(AgentRole.DISPENSER, HangAgent())
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="s",
        recipient_role=AgentRole.DISPENSER,
        action_type=ActionType.DISPENSING_STARTED,
        facility_id="F",
        payload={"drug_id": "d1"},
    )
    resp = await orch.dispatch_immediate(msg)
    assert resp.success is False
    assert "timed out" in (resp.error or "").lower()


@pytest.mark.parametrize("use_simulation_doctor", [True, False])
@pytest.mark.asyncio
async def test_integration_prescription_to_dispense(
    seeded_sqlite_db, monkeypatch, use_simulation_doctor: bool
):
    monkeypatch.setattr(settings, "orchestrator_agent_timeout_seconds", 60.0)

    with get_db() as session:
        sl = session.scalars(
            select(StockLevel).where(
                StockLevel.facility_id == settings.simulation_facility_id,
                StockLevel.quantity > 2,
            ).limit(1)
        ).first()
        assert sl is not None
        drug_id = sl.drug_id
        fac = settings.simulation_facility_id

    orch = build_default_orchestrator(use_simulation_doctor=use_simulation_doctor)
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="pytest",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id=fac,
        payload={
            "drug_id": drug_id,
            "facility_id": fac,
            "patient_id": "PAT-TEST-1",
            "doctor_agent_id": "doc-test",
            "quantity_prescribed": 2,
        },
    )
    orch.submit(msg)
    responses = await orch.run_until_idle(max_messages=10)
    assert len(responses) >= 2
    assert responses[0].success and responses[0].action_type == ActionType.PRESCRIPTION_CREATED
    assert responses[1].success and responses[1].action_type == ActionType.DISPENSING_COMPLETE

    with get_db() as session:
        rx_id = responses[0].result["prescription_id"]
        rx = session.get(Prescription, rx_id)
        assert rx is not None
        assert rx.status == PrescriptionStatus.DISPENSED


@pytest.mark.asyncio
async def test_real_doctor_rejects_incomplete_payload():
    """Default factory registers Eng 3 DoctorAgent — empty prescription payload fails."""
    orch = build_default_orchestrator(use_simulation_doctor=False)
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="t",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id=settings.simulation_facility_id,
        payload={},
    )
    resp = await orch.dispatch_immediate(msg)
    assert resp.success is False


@pytest.mark.asyncio
async def test_explicit_placeholder_doctor_still_returns_stub_error():
    from agents.stub_agents import PlaceholderDoctorAgent

    orch = Orchestrator()
    orch.register(AgentRole.DOCTOR, PlaceholderDoctorAgent())
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="t",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id=settings.simulation_facility_id,
        payload={"drug_id": "x", "patient_id": "p", "quantity_prescribed": 1},
    )
    resp = await orch.dispatch_immediate(msg)
    assert resp.success is False
    assert "PlaceholderDoctorAgent" in (resp.error or "")
