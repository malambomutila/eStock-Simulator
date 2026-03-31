"""
Dispenser agent — fills prescriptions and deducts stock.

Responsibilities:
  1. Validate the incoming prescription is PENDING and not already claimed.
  2. Locate available (non-quarantined, non-expired) stock using FEFO order.
  3. Deduct the dispensed quantity, handling partial dispenses when stock is low.
  4. Update the Prescription status (DISPENSED / PARTIALLY_DISPENSED / OUT_OF_STOCK).
  5. Write an immutable AuditLog entry for every outcome.
  6. Emit the result back to the orchestrator via AgentResponse.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import asc
from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from config.settings import settings
from core.database import get_db
from core.models import (
    ActionType,
    AgentRole,
    AuditLog,
    Prescription,
    PrescriptionStatus,
    StockLevel,
)
from core.schemas import (
    AgentMessage,
    AgentResponse,
    AuditLogCreate,
    DispenseRequest,
    DispenseResult,
)

logger = logging.getLogger(__name__)


class DispenserAgent(BaseAgent):
    """
    Simulates the pharmacy dispenser persona.

    The orchestrator must acquire a stock-level lock before dispatching a
    message here to prevent two concurrent dispense requests consuming from
    the same batch (set requires_lock=True on the AgentMessage).
    """

    role = AgentRole.DISPENSER

    async def handle(self, message: AgentMessage) -> AgentResponse:
        """Route incoming messages to the appropriate action handler."""
        if message.action_type == ActionType.DISPENSING_STARTED:
            return await self._dispense(message)

        audit = self._make_audit_entry(
            message,
            action_type=ActionType.SYSTEM_ERROR,
            entity_type=None,
            entity_id=None,
            payload={"unsupported_action": message.action_type},
        )
        return self._fail(
            message,
            ActionType.SYSTEM_ERROR,
            f"DispenserAgent does not handle action_type '{message.action_type}'",
            audit_entry=audit,
        )

    # Core dispensing logic
    async def _dispense(self, message: AgentMessage) -> AgentResponse:
        try:
            request = DispenseRequest(**message.payload)
        except Exception as exc:
            return self._fail(
                message,
                ActionType.DISPENSING_FAILED,
                f"Invalid dispense payload: {exc}",
            )

        with get_db() as session:
            return self._run_dispense(session, message, request)

    def _run_dispense(
        self, session: Session, message: AgentMessage, request: DispenseRequest
    ) -> AgentResponse:
        rx = self._fetch_prescription(session, request.prescription_id)
        if rx is None:
            return self._fail(
                message,
                ActionType.DISPENSING_FAILED,
                f"Prescription {request.prescription_id} not found.",
                audit_entry=self._make_audit_entry(
                    message,
                    ActionType.DISPENSING_FAILED,
                    entity_type="Prescription",
                    entity_id=request.prescription_id,
                    payload={"reason": "prescription_not_found"},
                ),
            )

        if rx.status != PrescriptionStatus.PENDING:
            return self._fail(
                message,
                ActionType.DISPENSING_FAILED,
                f"Prescription {rx.id} is not PENDING (current: {rx.status}).",
                audit_entry=self._make_audit_entry(
                    message,
                    ActionType.DISPENSING_FAILED,
                    entity_type="Prescription",
                    entity_id=rx.id,
                    payload={"current_status": rx.status, "reason": "not_pending"},
                ),
            )

        batches = self._get_available_batches(session, request)
        if not batches:
            rx.status = PrescriptionStatus.OUT_OF_STOCK
            session.flush()
            audit = self._make_audit_entry(
                message,
                ActionType.DISPENSING_FAILED,
                entity_type="Prescription",
                entity_id=rx.id,
                payload={
                    "drug_id": request.drug_id,
                    "quantity_requested": request.quantity_requested,
                    "reason": "out_of_stock",
                },
            )
            self._write_audit(session, audit)
            return self._fail(
                message,
                ActionType.DISPENSING_FAILED,
                f"No available stock for drug {request.drug_id} at {request.facility_id}.",
                audit_entry=audit,
            )

        qty_dispensed, last_batch = self._deduct_fefo(
            session, batches, request.quantity_requested
        )

        new_status = (
            PrescriptionStatus.DISPENSED
            if qty_dispensed >= request.quantity_requested
            else PrescriptionStatus.PARTIALLY_DISPENSED
        )

        rx.quantity_dispensed = qty_dispensed
        rx.status = new_status
        rx.dispensed_by_agent_id = self.agent_id
        rx.dispensed_at = datetime.now(timezone.utc)
        session.flush()

        remaining = self._total_available(session, request.drug_id, request.facility_id)

        dispense_result = DispenseResult(
            prescription_id=rx.id,
            drug_id=request.drug_id,
            quantity_dispensed=qty_dispensed,
            batch_number=last_batch,
            stock_remaining=remaining,
            status=new_status,
        )

        audit = self._make_audit_entry(
            message,
            ActionType.DISPENSING_COMPLETE,
            entity_type="Prescription",
            entity_id=rx.id,
            payload={
                **dispense_result.model_dump(),
                "quantity_requested": request.quantity_requested,
                "facility_id": request.facility_id,
                "doctor_agent_id": request.doctor_agent_id,
            },
        )
        self._write_audit(session, audit)

        self.logger.info(
            "Dispensed %d/%d of drug %s for Rx %s (status=%s, stock_left=%d)",
            qty_dispensed,
            request.quantity_requested,
            request.drug_id,
            rx.id,
            new_status,
            remaining,
        )

        return self._ok(
            message,
            ActionType.DISPENSING_COMPLETE,
            result=dispense_result.model_dump(),
            audit_entry=audit,
        )

    # Query helpers
    @staticmethod
    def _fetch_prescription(session: Session, rx_id: str) -> Prescription | None:
        return session.query(Prescription).filter(Prescription.id == rx_id).first()

    @staticmethod
    def _get_available_batches(
        session: Session, request: DispenseRequest
    ) -> list[StockLevel]:
        """
        Return non-quarantined, non-expired batches in FEFO order
        (earliest expiry first) for the requested drug at the given facility.
        """
        now = datetime.now(timezone.utc)
        return (
            session.query(StockLevel)
            .filter(
                StockLevel.drug_id == request.drug_id,
                StockLevel.facility_id == request.facility_id,
                StockLevel.quantity > 0,
                StockLevel.is_quarantined.is_(False),
                StockLevel.expiry_date > now,
            )
            .order_by(asc(StockLevel.expiry_date))
            .all()
        )

    @staticmethod
    def _deduct_fefo(
        session: Session, batches: list[StockLevel], qty_needed: int
    ) -> tuple[int, str | None]:
        """
        Consume from batches in FEFO order.

        Returns (total_dispensed, batch_number_of_last_touched_batch).
        """
        remaining_to_dispense = qty_needed
        last_batch: str | None = None

        for batch in batches:
            if remaining_to_dispense <= 0:
                break
            take = min(batch.quantity, remaining_to_dispense)
            batch.quantity -= take
            remaining_to_dispense -= take
            last_batch = batch.batch_number

        session.flush()
        return qty_needed - remaining_to_dispense, last_batch

    @staticmethod
    def _total_available(
        session: Session, drug_id: str, facility_id: str
    ) -> int:
        """Sum of all non-quarantined, non-expired stock for a drug at a facility."""
        now = datetime.now(timezone.utc)
        batches = (
            session.query(StockLevel)
            .filter(
                StockLevel.drug_id == drug_id,
                StockLevel.facility_id == facility_id,
                StockLevel.is_quarantined.is_(False),
                StockLevel.expiry_date > now,
            )
            .all()
        )
        return sum(b.quantity for b in batches)

    @staticmethod
    def _write_audit(session: Session, entry: AuditLogCreate) -> None:
        log = AuditLog(
            facility_id=entry.facility_id,
            agent_id=entry.agent_id,
            agent_role=entry.agent_role,
            action_type=entry.action_type,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            payload=entry.payload,
            is_anomaly=entry.is_anomaly,
            anomaly_reason=entry.anomaly_reason,
            correlation_id=entry.correlation_id,
        )
        session.add(log)
        session.flush()
