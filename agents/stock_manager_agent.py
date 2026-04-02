"""
Stock Manager agent — manages stock receipts, expiry scanning, and reorder alerts.

Responsibilities:
  1. Receive new stock deliveries (STOCK_RECEIVED): upsert StockLevel batches.
  2. Scan batches for expiry (EXPIRY_CHECKED): quarantine expired batches and
     emit near-expiry warnings within the configured alert window.
  3. Check reorder levels (REORDER_ALERT_RAISED): flag any drug whose available
     stock falls below its reorder_threshold.
  4. Acknowledge doctor restock requests (RESTOCK_REQUESTED): log the request
     and raise a targeted reorder alert for the specified drug.

Expected AgentMessage payloads
──────────────────────────────
STOCK_RECEIVED:
  {
    "drug_id":           str,           # must exist in the drugs table
    "batch_number":      str,
    "quantity":          int,           # ≥ 1
    "expiry_date":       str,           # ISO-8601
    "facility_id":       str,
    "supplier":          str | null,
    "unit_cost":         float | null,
    "manufactured_date": str | null,    # ISO-8601
    "location_in_store": str | null
  }

EXPIRY_CHECKED:
  {
    "scan_days_ahead":    int | null,   # default: settings.reorder_alert_threshold_days
    "drug_id":            str | null,   # narrow to one drug; null = all
    "quarantine_expired": bool          # default true
  }

REORDER_ALERT_RAISED:
  {
    "drug_id":     str | null,          # check one drug; null = all active drugs
    "force_check": bool                 # reserved; currently ignored
  }

RESTOCK_REQUESTED:
  {
    "drug_id":  str,
    "quantity": int | null,
    "reason":   str | null
  }
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from config.settings import settings
from core.database import get_db
from core.models import (
    ActionType,
    AgentRole,
    AuditLog,
    Drug,
    StockLevel,
)
from core.schemas import (
    AgentMessage,
    AgentResponse,
    AuditLogCreate,
    ExpiryCheckResult,
    ReorderCheckResult,
    StockReceiptRequest,
    StockReceiptResult,
)

logger = logging.getLogger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a timezone-aware datetime; return None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class StockManagerAgent(BaseAgent):
    """
    Simulates the pharmacy store manager persona.

    Manages incoming stock deliveries, expiry surveillance, and reorder
    threshold monitoring. The orchestrator must set requires_lock=True on
    STOCK_RECEIVED and EXPIRY_CHECKED messages because both modify StockLevel rows.
    """

    role = AgentRole.STOCK_MANAGER

    async def handle(self, message: AgentMessage) -> AgentResponse:
        """Route incoming messages to the appropriate action handler."""
        dispatch = {
            ActionType.STOCK_RECEIVED: self._receive_stock,
            ActionType.EXPIRY_CHECKED: self._check_expiry,
            ActionType.REORDER_ALERT_RAISED: self._check_reorder_levels,
            ActionType.RESTOCK_REQUESTED: self._handle_restock_request,
        }
        handler = dispatch.get(message.action_type)
        if handler:
            return await handler(message)

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
            f"StockManagerAgent does not handle action_type '{message.action_type}'",
            audit_entry=audit,
        )

    # ── Action handlers ────────────────────────────────────────────────────────

    async def _receive_stock(self, message: AgentMessage) -> AgentResponse:
        """
        Accept a new batch delivery.

        Upserts the StockLevel row (increments if batch_number already exists,
        inserts otherwise) then checks whether the new total still sits below
        the reorder threshold and writes a REORDER_ALERT_RAISED audit entry
        if so.
        """
        try:
            request = StockReceiptRequest(**message.payload)
        except Exception as exc:
            return self._fail(
                message,
                ActionType.STOCK_RECEIVED,
                f"Invalid STOCK_RECEIVED payload: {exc}",
            )

        expiry_dt = _parse_dt(request.expiry_date)
        if expiry_dt is None:
            return self._fail(
                message,
                ActionType.STOCK_RECEIVED,
                f"Cannot parse expiry_date '{request.expiry_date}' — expected ISO-8601.",
            )

        manufactured_dt = _parse_dt(request.manufactured_date)

        with get_db() as session:
            drug = session.query(Drug).filter(Drug.id == request.drug_id).first()
            if drug is None:
                audit = self._make_audit_entry(
                    message,
                    ActionType.STOCK_RECEIVED,
                    entity_type="Drug",
                    entity_id=request.drug_id,
                    payload={"reason": "drug_not_found", **request.model_dump()},
                )
                self._write_audit(session, audit)
                return self._fail(
                    message,
                    ActionType.STOCK_RECEIVED,
                    f"Drug '{request.drug_id}' not found in catalog.",
                    audit_entry=audit,
                )

            if not drug.is_active:
                audit = self._make_audit_entry(
                    message,
                    ActionType.STOCK_RECEIVED,
                    entity_type="Drug",
                    entity_id=drug.id,
                    payload={"reason": "drug_inactive", **request.model_dump()},
                )
                self._write_audit(session, audit)
                return self._fail(
                    message,
                    ActionType.STOCK_RECEIVED,
                    f"Drug '{drug.name}' is inactive — stock receipt rejected.",
                    audit_entry=audit,
                )

            existing = (
                session.query(StockLevel)
                .filter(
                    StockLevel.drug_id == request.drug_id,
                    StockLevel.facility_id == request.facility_id,
                    StockLevel.batch_number == request.batch_number,
                )
                .first()
            )

            if existing:
                existing.quantity += request.quantity
                if request.unit_cost is not None:
                    existing.unit_cost = request.unit_cost
                if request.location_in_store is not None:
                    existing.location_in_store = request.location_in_store
                batch_id = existing.id
                session.flush()
            else:
                new_batch = StockLevel(
                    drug_id=request.drug_id,
                    facility_id=request.facility_id,
                    batch_number=request.batch_number,
                    quantity=request.quantity,
                    expiry_date=expiry_dt,
                    manufactured_date=manufactured_dt,
                    supplier=request.supplier,
                    unit_cost=request.unit_cost,
                    location_in_store=request.location_in_store,
                    is_quarantined=False,
                )
                session.add(new_batch)
                session.flush()
                batch_id = new_batch.id

            new_total = self._total_available(session, request.drug_id, request.facility_id)

            receipt_result = StockReceiptResult(
                drug_id=request.drug_id,
                batch_number=request.batch_number,
                stock_level_id=batch_id,
                quantity_added=request.quantity,
                new_total=new_total,
            )

            audit = self._make_audit_entry(
                message,
                ActionType.STOCK_RECEIVED,
                entity_type="StockLevel",
                entity_id=batch_id,
                payload={
                    **receipt_result.model_dump(),
                    "drug_name": drug.name,
                    "supplier": request.supplier,
                    "expiry_date": expiry_dt.isoformat(),
                    "facility_id": request.facility_id,
                },
            )
            self._write_audit(session, audit)

            # If total is still below the drug's reorder threshold after receipt,
            # write an informational reorder alert so the dashboard surfaces it.
            if new_total < drug.reorder_threshold:
                deficit = drug.reorder_threshold - new_total
                reorder_audit = self._make_audit_entry(
                    message,
                    ActionType.REORDER_ALERT_RAISED,
                    entity_type="Drug",
                    entity_id=drug.id,
                    payload={
                        "drug_id": drug.id,
                        "drug_name": drug.name,
                        "current_qty": new_total,
                        "reorder_threshold": drug.reorder_threshold,
                        "deficit": deficit,
                        "triggered_by": "post_receipt_check",
                    },
                )
                self._write_audit(session, reorder_audit)
                self.logger.warning(
                    "Post-receipt reorder alert: drug %s still %d units below threshold (%d/%d)",
                    drug.name,
                    deficit,
                    new_total,
                    drug.reorder_threshold,
                )

        self.logger.info(
            "Stock received: %d units of '%s' (batch %s), new total=%d",
            request.quantity,
            drug.name,
            request.batch_number,
            new_total,
        )

        return self._ok(
            message,
            ActionType.STOCK_RECEIVED,
            result=receipt_result.model_dump(),
            audit_entry=audit,
        )

    async def _check_expiry(self, message: AgentMessage) -> AgentResponse:
        """
        Scan all batches for the facility and handle expired / near-expiry stock.

        - Expired batches (expiry_date <= now): quarantine them (if
          quarantine_expired=True) and write BATCH_EXPIRED audit entries with
          is_anomaly=True.
        - Near-expiry batches (now < expiry_date <= now + scan_days_ahead days):
          write EXPIRY_CHECKED audit entries as informational warnings.
        """
        scan_days: int = int(
            message.payload.get("scan_days_ahead", settings.reorder_alert_threshold_days)
        )
        filter_drug_id: str | None = message.payload.get("drug_id")
        quarantine_expired: bool = message.payload.get("quarantine_expired", True)

        now = datetime.now(timezone.utc)
        expiry_cutoff = now + timedelta(days=scan_days)

        expired_details: list[dict] = []
        near_expiry_details: list[dict] = []

        with get_db() as session:
            batches = self._get_active_batches(session, message.facility_id, filter_drug_id)

            for batch in batches:
                expiry = batch.expiry_date
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)

                drug_name = batch.drug.name if batch.drug else batch.drug_id

                if expiry <= now:
                    # Already expired
                    if quarantine_expired and not batch.is_quarantined:
                        batch.is_quarantined = True
                        session.flush()

                    detail = {
                        "batch_id": batch.id,
                        "drug_id": batch.drug_id,
                        "drug_name": drug_name,
                        "batch_number": batch.batch_number,
                        "expiry_date": expiry.isoformat(),
                        "quantity": batch.quantity,
                        "quarantined": batch.is_quarantined,
                        "status": "expired",
                    }
                    expired_details.append(detail)

                    expired_audit = self._make_audit_entry(
                        message,
                        ActionType.BATCH_EXPIRED,
                        entity_type="StockLevel",
                        entity_id=batch.id,
                        payload=detail,
                        is_anomaly=True,
                        anomaly_reason=(
                            f"Batch {batch.batch_number} of '{drug_name}' "
                            f"expired on {expiry.date()} with {batch.quantity} units remaining."
                        ),
                    )
                    self._write_audit(session, expired_audit)

                elif expiry <= expiry_cutoff:
                    # Near-expiry warning
                    days_left = (expiry - now).days
                    detail = {
                        "batch_id": batch.id,
                        "drug_id": batch.drug_id,
                        "drug_name": drug_name,
                        "batch_number": batch.batch_number,
                        "expiry_date": expiry.isoformat(),
                        "days_until_expiry": days_left,
                        "quantity": batch.quantity,
                        "status": "near_expiry",
                    }
                    near_expiry_details.append(detail)

                    near_expiry_audit = self._make_audit_entry(
                        message,
                        ActionType.EXPIRY_CHECKED,
                        entity_type="StockLevel",
                        entity_id=batch.id,
                        payload=detail,
                    )
                    self._write_audit(session, near_expiry_audit)

            all_details = expired_details + near_expiry_details

            # Summary audit entry
            summary_audit = self._make_audit_entry(
                message,
                ActionType.EXPIRY_CHECKED,
                entity_type="StockLevel",
                entity_id=None,
                payload={
                    "facility_id": message.facility_id,
                    "scan_days_ahead": scan_days,
                    "drug_filter": filter_drug_id,
                    "scanned_batches": len(batches),
                    "expired_count": len(expired_details),
                    "near_expiry_count": len(near_expiry_details),
                    "quarantine_expired": quarantine_expired,
                },
            )
            self._write_audit(session, summary_audit)

        check_result = ExpiryCheckResult(
            facility_id=message.facility_id,
            scanned_batches=len(batches),
            expired_count=len(expired_details),
            near_expiry_count=len(near_expiry_details),
            details=all_details,
        )

        self.logger.info(
            "Expiry scan complete for %s: %d scanned, %d expired, %d near-expiry",
            message.facility_id,
            check_result.scanned_batches,
            check_result.expired_count,
            check_result.near_expiry_count,
        )

        return self._ok(
            message,
            ActionType.EXPIRY_CHECKED,
            result=check_result.model_dump(),
            audit_entry=summary_audit,
        )

    async def _check_reorder_levels(self, message: AgentMessage) -> AgentResponse:
        """
        Sweep all active drugs at the facility and raise an alert for any whose
        available stock is below its reorder_threshold.
        """
        filter_drug_id: str | None = message.payload.get("drug_id")

        alerts: list[dict] = []

        with get_db() as session:
            query = session.query(Drug).filter(Drug.is_active.is_(True))
            if filter_drug_id:
                query = query.filter(Drug.id == filter_drug_id)
            drugs = query.all()

            for drug in drugs:
                total = self._total_available(session, drug.id, message.facility_id)
                if total < drug.reorder_threshold:
                    deficit = drug.reorder_threshold - total
                    alert = {
                        "drug_id": drug.id,
                        "drug_name": drug.name,
                        "current_qty": total,
                        "reorder_threshold": drug.reorder_threshold,
                        "deficit": deficit,
                        "is_controlled": drug.is_controlled,
                    }
                    alerts.append(alert)

                    drug_audit = self._make_audit_entry(
                        message,
                        ActionType.REORDER_ALERT_RAISED,
                        entity_type="Drug",
                        entity_id=drug.id,
                        payload=alert,
                    )
                    self._write_audit(session, drug_audit)

            # Summary audit entry
            summary_audit = self._make_audit_entry(
                message,
                ActionType.REORDER_ALERT_RAISED,
                entity_type="Drug",
                entity_id=filter_drug_id,
                payload={
                    "facility_id": message.facility_id,
                    "drugs_checked": len(drugs),
                    "alerts_raised": len(alerts),
                    "drug_filter": filter_drug_id,
                },
            )
            self._write_audit(session, summary_audit)

        reorder_result = ReorderCheckResult(
            facility_id=message.facility_id,
            drugs_checked=len(drugs),
            alerts_raised=len(alerts),
            details=alerts,
        )

        self.logger.info(
            "Reorder check for %s: %d drugs checked, %d alerts raised",
            message.facility_id,
            reorder_result.drugs_checked,
            reorder_result.alerts_raised,
        )

        return self._ok(
            message,
            ActionType.REORDER_ALERT_RAISED,
            result=reorder_result.model_dump(),
            audit_entry=summary_audit,
        )

    async def _handle_restock_request(self, message: AgentMessage) -> AgentResponse:
        """
        Acknowledge a restock request forwarded from the Doctor agent.

        Logs the request and raises a targeted REORDER_ALERT_RAISED audit entry
        for the specified drug so the dashboard and auditor can surface it.
        """
        drug_id: str | None = message.payload.get("drug_id")
        quantity: int | None = message.payload.get("quantity")
        reason: str | None = message.payload.get("reason")

        if not drug_id:
            return self._fail(
                message,
                ActionType.RESTOCK_REQUESTED,
                "RESTOCK_REQUESTED payload missing required field 'drug_id'.",
            )

        with get_db() as session:
            drug = session.query(Drug).filter(Drug.id == drug_id).first()
            drug_name = drug.name if drug else drug_id
            total = self._total_available(session, drug_id, message.facility_id)
            reorder_threshold = drug.reorder_threshold if drug else 0

            request_audit = self._make_audit_entry(
                message,
                ActionType.RESTOCK_REQUESTED,
                entity_type="Drug",
                entity_id=drug_id,
                payload={
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "quantity_requested": quantity,
                    "reason": reason,
                    "current_qty": total,
                    "reorder_threshold": reorder_threshold,
                    "requested_by": message.sender_id,
                },
            )
            self._write_audit(session, request_audit)

            # Always raise a reorder alert for the specific drug so it's visible
            reorder_audit = self._make_audit_entry(
                message,
                ActionType.REORDER_ALERT_RAISED,
                entity_type="Drug",
                entity_id=drug_id,
                payload={
                    "drug_id": drug_id,
                    "drug_name": drug_name,
                    "current_qty": total,
                    "reorder_threshold": reorder_threshold,
                    "deficit": max(0, reorder_threshold - total),
                    "triggered_by": "restock_request",
                    "requester": message.sender_id,
                },
            )
            self._write_audit(session, reorder_audit)

        self.logger.info(
            "Restock request acknowledged: drug=%s qty=%s reason=%s (current stock: %d)",
            drug_name,
            quantity,
            reason,
            total,
        )

        return self._ok(
            message,
            ActionType.RESTOCK_REQUESTED,
            result={
                "drug_id": drug_id,
                "drug_name": drug_name,
                "quantity_requested": quantity,
                "current_qty": total,
                "reorder_alert_raised": True,
            },
            audit_entry=request_audit,
        )

    # ── Query helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _total_available(session: Session, drug_id: str, facility_id: str) -> int:
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
    def _get_active_batches(
        session: Session, facility_id: str, drug_id: str | None = None
    ) -> list[StockLevel]:
        """
        Return all non-quarantined StockLevel rows for a facility, optionally
        filtered to a single drug. Loads the drug relationship for name lookups.
        """
        from sqlalchemy.orm import joinedload

        query = (
            session.query(StockLevel)
            .options(joinedload(StockLevel.drug))
            .filter(
                StockLevel.facility_id == facility_id,
                StockLevel.is_quarantined.is_(False),
                StockLevel.quantity > 0,
            )
        )
        if drug_id:
            query = query.filter(StockLevel.drug_id == drug_id)
        return query.all()

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
