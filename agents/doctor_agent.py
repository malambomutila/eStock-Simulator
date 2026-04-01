"""
Doctor agent — creates prescriptions and checks drug availability.

Responsibilities:
  1. Receive a prescription request from the orchestrator (patient context + drug details).
  2. Optionally call the LLM to generate clinical notes / urgency classification.
  3. Verify the requested drug exists and is active in the catalogue.
  4. Insert a new Prescription row (status=PENDING) for the Dispenser to fulfil.
  5. Check drug availability on demand without modifying stock.
  6. Emit restock requests when availability is critically low.
  7. Write an immutable AuditLog entry for every outcome.

Expected AgentMessage payloads
──────────────────────────────
PRESCRIPTION_CREATED:
  {
    "patient_id":           str,          # anonymised patient identifier
    "drug_id":              str,          # drug primary key from catalogue
    "quantity_prescribed":  int,          # units requested
    "diagnosis_code":       str | null,   # ICD-10 code
    "notes":                str | null    # optional clinical notes (LLM will enrich)
  }

AVAILABILITY_CHECKED:
  { "drug_id": str }

RESTOCK_REQUESTED:
  { "drug_id": str, "quantity_needed": int, "reason": str | null }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from config.prompts import DOCTOR_SYSTEM_PROMPT
from config.settings import settings
from core.database import get_db
from core.models import (
    ActionType,
    AgentRole,
    AuditLog,
    Drug,
    Prescription,
    PrescriptionStatus,
    StockLevel,
)
from core.schemas import AgentMessage, AgentResponse, AuditLogCreate

logger = logging.getLogger(__name__)


class DoctorAgent(BaseAgent):
    """Simulates the clinical physician persona."""

    role = AgentRole.DOCTOR

    async def handle(self, message: AgentMessage) -> AgentResponse:
        if message.action_type == ActionType.PRESCRIPTION_CREATED:
            return await self._create_prescription(message)
        if message.action_type == ActionType.AVAILABILITY_CHECKED:
            return await self._check_availability(message)
        if message.action_type == ActionType.RESTOCK_REQUESTED:
            return await self._request_restock(message)

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
            f"DoctorAgent does not handle action_type '{message.action_type}'",
            audit_entry=audit,
        )

    # ── Action handlers ────────────────────────────────────────────────────────

    async def _create_prescription(self, message: AgentMessage) -> AgentResponse:
        payload = message.payload
        drug_id: str | None = payload.get("drug_id")
        patient_id: str | None = payload.get("patient_id")
        quantity_prescribed: int | None = payload.get("quantity_prescribed")

        if not all([drug_id, patient_id, quantity_prescribed]):
            return self._fail(
                message,
                ActionType.PRESCRIPTION_CREATED,
                "Payload must include drug_id, patient_id, and quantity_prescribed.",
            )

        with get_db() as session:
            drug = self._fetch_drug(session, drug_id)
            if drug is None or not drug.is_active:
                audit = self._make_audit_entry(
                    message,
                    ActionType.PRESCRIPTION_CREATED,
                    entity_type="Drug",
                    entity_id=drug_id,
                    payload={"reason": "drug_not_found_or_inactive", **payload},
                )
                self._write_audit(session, audit)
                return self._fail(
                    message,
                    ActionType.PRESCRIPTION_CREATED,
                    f"Drug '{drug_id}' not found or inactive.",
                    audit_entry=audit,
                )

            # Enrich clinical notes via LLM (non-blocking — falls back gracefully).
            llm_notes = self._call_llm_for_notes(drug, payload)
            combined_notes = payload.get("notes") or llm_notes

            rx = Prescription(
                drug_id=drug_id,
                facility_id=message.facility_id,
                patient_id=patient_id,
                doctor_agent_id=self.agent_id,
                quantity_prescribed=quantity_prescribed,
                status=PrescriptionStatus.PENDING,
                diagnosis_code=payload.get("diagnosis_code"),
                notes=combined_notes,
            )
            session.add(rx)
            session.flush()

            audit = self._make_audit_entry(
                message,
                ActionType.PRESCRIPTION_CREATED,
                entity_type="Prescription",
                entity_id=rx.id,
                payload={
                    "prescription_id": rx.id,
                    "drug_id": drug_id,
                    "drug_name": drug.name,
                    "patient_id": patient_id,
                    "quantity_prescribed": quantity_prescribed,
                    "diagnosis_code": payload.get("diagnosis_code"),
                    "is_controlled": drug.is_controlled,
                    "facility_id": message.facility_id,
                },
            )
            self._write_audit(session, audit)

        self.logger.info(
            "Prescription created for patient %s — drug %s x%d (Rx=%s)",
            patient_id,
            drug.name,
            quantity_prescribed,
            rx.id,
        )

        return self._ok(
            message,
            ActionType.PRESCRIPTION_CREATED,
            result={
                "prescription_id": rx.id,
                "drug_id": drug_id,
                "drug_name": drug.name,
                "patient_id": patient_id,
                "quantity_prescribed": quantity_prescribed,
                "status": PrescriptionStatus.PENDING,
                "is_controlled": drug.is_controlled,
            },
            audit_entry=audit,
        )

    async def _check_availability(self, message: AgentMessage) -> AgentResponse:
        drug_id: str | None = message.payload.get("drug_id")
        if not drug_id:
            return self._fail(
                message,
                ActionType.AVAILABILITY_CHECKED,
                "Payload must include drug_id.",
            )

        with get_db() as session:
            drug = self._fetch_drug(session, drug_id)
            if drug is None:
                return self._fail(
                    message,
                    ActionType.AVAILABILITY_CHECKED,
                    f"Drug '{drug_id}' not found.",
                )

            total_qty, batch_count, earliest_expiry = self._query_availability(
                session, drug_id, message.facility_id
            )
            is_low = total_qty < (drug.reorder_threshold * settings.low_stock_threshold_pct)

            result = {
                "drug_id": drug_id,
                "drug_name": drug.name,
                "facility_id": message.facility_id,
                "total_quantity": total_qty,
                "batch_count": batch_count,
                "earliest_expiry": earliest_expiry.isoformat() if earliest_expiry else None,
                "reorder_threshold": drug.reorder_threshold,
                "is_low_stock": is_low,
            }

            audit = self._make_audit_entry(
                message,
                ActionType.AVAILABILITY_CHECKED,
                entity_type="Drug",
                entity_id=drug_id,
                payload=result,
            )
            self._write_audit(session, audit)

        return self._ok(
            message,
            ActionType.AVAILABILITY_CHECKED,
            result=result,
            audit_entry=audit,
        )

    async def _request_restock(self, message: AgentMessage) -> AgentResponse:
        drug_id: str | None = message.payload.get("drug_id")
        quantity_needed: int = message.payload.get("quantity_needed", 0)
        reason: str = message.payload.get("reason", "low stock identified by doctor")

        if not drug_id:
            return self._fail(
                message,
                ActionType.RESTOCK_REQUESTED,
                "Payload must include drug_id.",
            )

        with get_db() as session:
            drug = self._fetch_drug(session, drug_id)
            drug_name = drug.name if drug else drug_id

            audit = self._make_audit_entry(
                message,
                ActionType.RESTOCK_REQUESTED,
                entity_type="Drug",
                entity_id=drug_id,
                payload={
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "quantity_needed": quantity_needed,
                    "reason": reason,
                    "facility_id": message.facility_id,
                },
            )
            self._write_audit(session, audit)

        self.logger.info(
            "Restock requested for drug %s — qty %d (%s)", drug_name, quantity_needed, reason
        )

        return self._ok(
            message,
            ActionType.RESTOCK_REQUESTED,
            result={
                "drug_id": drug_id,
                "drug_name": drug_name,
                "quantity_needed": quantity_needed,
                "reason": reason,
            },
            audit_entry=audit,
        )

    # ── Query helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_drug(session: Session, drug_id: str) -> Drug | None:
        return session.query(Drug).filter(Drug.id == drug_id).first()

    @staticmethod
    def _query_availability(
        session: Session, drug_id: str, facility_id: str
    ) -> tuple[int, int, datetime | None]:
        """Returns (total_quantity, batch_count, earliest_expiry) for available stock."""
        now = datetime.now(timezone.utc)
        batches = (
            session.query(StockLevel)
            .filter(
                StockLevel.drug_id == drug_id,
                StockLevel.facility_id == facility_id,
                StockLevel.quantity > 0,
                StockLevel.is_quarantined.is_(False),
                StockLevel.expiry_date > now,
            )
            .all()
        )
        if not batches:
            return 0, 0, None
        total = sum(b.quantity for b in batches)
        earliest = min(b.expiry_date for b in batches)
        return total, len(batches), earliest

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

    # ── LLM helper ─────────────────────────────────────────────────────────────

    def _call_llm_for_notes(self, drug: Drug, payload: dict) -> str:
        """
        Call the LLM to generate clinical notes for the prescription.
        Falls back to an empty string if the API key is unset or the call fails.
        """
        if not settings.openai_api_key:
            return ""

        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            user_msg = (
                f"Drug: {drug.name} ({drug.generic_name}), "
                f"Dosage form: {drug.dosage_form}, Strength: {drug.strength}. "
                f"Quantity prescribed: {payload.get('quantity_prescribed')}. "
                f"Diagnosis code: {payload.get('diagnosis_code', 'not specified')}. "
                f"Existing notes: {payload.get('notes', 'none')}. "
                "Generate clinical notes for this prescription."
            )
            response = client.chat.completions.create(
                model=settings.llm_model,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": DOCTOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            notes = data.get("clinical_notes", "")
            urgency = data.get("urgency", "routine")
            controlled_warn = data.get("is_controlled_drug_warning", False)

            parts = [notes]
            if urgency != "routine":
                parts.append(f"[Urgency: {urgency}]")
            if controlled_warn:
                parts.append("[CONTROLLED DRUG — additional audit required]")
            return " ".join(filter(None, parts))

        except Exception as exc:
            self.logger.warning("LLM call failed for clinical notes: %s", exc)
            return ""
