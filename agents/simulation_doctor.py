"""
Non-LLM doctor used for integration tests and orchestrator dry runs.

Eng 3 replaces this with the real clinical persona + LLM. This module only
handles ``PRESCRIPTION_CREATED`` by inserting a pending ``Prescription`` row.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.database import get_db
from core.models import ActionType, AgentRole, Prescription, PrescriptionStatus
from core.schemas import AgentMessage, AgentResponse, PrescriptionCreate


class SimulationDoctorAgent(BaseAgent):
    role = AgentRole.DOCTOR

    async def handle(self, message: AgentMessage) -> AgentResponse:
        if message.action_type != ActionType.PRESCRIPTION_CREATED:
            return self._fail(
                message,
                ActionType.SYSTEM_ERROR,
                f"SimulationDoctorAgent only handles PRESCRIPTION_CREATED (got {message.action_type}).",
            )

        payload = dict(message.payload or {})
        payload.setdefault("doctor_agent_id", self.agent_id)
        try:
            data = PrescriptionCreate(**payload)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                message,
                ActionType.SYSTEM_ERROR,
                f"Invalid prescription payload: {exc}",
            )

        with get_db() as session:
            rx = Prescription(
                drug_id=data.drug_id,
                facility_id=data.facility_id,
                patient_id=data.patient_id,
                doctor_agent_id=data.doctor_agent_id,
                quantity_prescribed=data.quantity_prescribed,
                diagnosis_code=data.diagnosis_code,
                notes=data.notes,
                status=PrescriptionStatus.PENDING,
            )
            session.add(rx)
            session.flush()
            rx_id = rx.id

        result = {
            "prescription_id": rx_id,
            "drug_id": data.drug_id,
            "facility_id": data.facility_id,
            "doctor_agent_id": data.doctor_agent_id,
            "quantity_prescribed": data.quantity_prescribed,
            "status": PrescriptionStatus.PENDING.value,
        }
        return self._ok(
            message,
            ActionType.PRESCRIPTION_CREATED,
            result,
        )
