"""
Government Official agent — audits the supply chain and flags anomalies.

Responsibilities:
  1. Query the AuditLog to produce compliance reports.
  2. Detect anomalies: dispensing spikes vs. 30-day averages, controlled-drug
     irregularities, and any activity on expired/quarantined batches.
  3. Write new, immutable AuditLog entries to record each finding
     (never mutates existing rows).
  4. Optionally use the LLM to produce a narrative compliance summary.

Expected AgentMessage payloads
──────────────────────────────
AUDIT_REPORT_GENERATED:
  {
    "from_ts":          str | null,   # ISO-8601; defaults to 24 h ago
    "to_ts":            str | null,   # ISO-8601; defaults to now
    "include_anomalies": bool         # default true
  }

ANOMALY_FLAGGED:
  {
    "scan_hours":       int | null,   # hours of history to scan; default 24
    "drug_id":          str | null    # narrow scan to one drug (optional)
  }

COMPLIANCE_CHECKED:
  {
    "drug_id":          str | null,   # check one drug; null = all controlled drugs
    "from_ts":          str | null,
    "to_ts":            str | null
  }
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from agents.base_agent import BaseAgent
from config.prompts import GOV_OFFICIAL_SYSTEM_PROMPT
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


class GovOfficialAgent(BaseAgent):
    """Simulates the Ministry of Health auditor persona."""

    role = AgentRole.GOV_OFFICIAL

    async def handle(self, message: AgentMessage) -> AgentResponse:
        if message.action_type == ActionType.AUDIT_REPORT_GENERATED:
            return await self._generate_report(message)
        if message.action_type == ActionType.ANOMALY_FLAGGED:
            return await self._flag_anomalies(message)
        if message.action_type == ActionType.COMPLIANCE_CHECKED:
            return await self._check_compliance(message)

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
            f"GovOfficialAgent does not handle action_type '{message.action_type}'",
            audit_entry=audit,
        )

    # ── Action handlers ────────────────────────────────────────────────────────

    async def _generate_report(self, message: AgentMessage) -> AgentResponse:
        now = datetime.now(timezone.utc)
        from_ts = _parse_ts(message.payload.get("from_ts")) or (now - timedelta(hours=24))
        to_ts = _parse_ts(message.payload.get("to_ts")) or now
        include_anomalies: bool = message.payload.get("include_anomalies", True)

        with get_db() as session:
            stats = self._collect_stats(session, message.facility_id, from_ts, to_ts)
            anomaly_rows = (
                self._fetch_anomalies(session, message.facility_id, from_ts, to_ts)
                if include_anomalies
                else []
            )

            llm_analysis = self._call_llm_for_report(stats, anomaly_rows)

            report = {
                "facility_id": message.facility_id,
                "period_from": from_ts.isoformat(),
                "period_to": to_ts.isoformat(),
                "stats": stats,
                "anomaly_count": len(anomaly_rows),
                "anomalies": anomaly_rows,
                "llm_analysis": llm_analysis,
            }

            audit = self._make_audit_entry(
                message,
                ActionType.AUDIT_REPORT_GENERATED,
                entity_type="AuditLog",
                entity_id=None,
                payload=report,
            )
            self._write_audit(session, audit)

        self.logger.info(
            "Audit report generated for %s (%s → %s): %d anomalies",
            message.facility_id,
            from_ts.date(),
            to_ts.date(),
            len(anomaly_rows),
        )

        return self._ok(
            message,
            ActionType.AUDIT_REPORT_GENERATED,
            result=report,
            audit_entry=audit,
        )

    async def _flag_anomalies(self, message: AgentMessage) -> AgentResponse:
        """
        Scan recent activity for anomalies and write a new AuditLog entry for
        each one found. Two checks are performed:
          1. Dispensing spikes: 24-h total > anomaly_spike_multiplier × 30-day daily avg.
          2. Controlled-drug prescriptions without a corresponding dispense audit entry.
        """
        scan_hours: int = message.payload.get("scan_hours", 24)
        filter_drug_id: str | None = message.payload.get("drug_id")

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=scan_hours)
        thirty_days_ago = now - timedelta(days=30)

        flagged: list[dict] = []

        with get_db() as session:
            # ── Check 1: dispensing volume spikes ──────────────────────────────
            recent_counts = self._count_dispensed_by_drug(
                session, message.facility_id, window_start, now, filter_drug_id
            )
            historic_counts = self._count_dispensed_by_drug(
                session, message.facility_id, thirty_days_ago, now, filter_drug_id
            )

            for drug_id, recent_qty in recent_counts.items():
                historic_qty = historic_counts.get(drug_id, 0)
                daily_avg = historic_qty / 30 if historic_qty else 0
                if daily_avg > 0 and recent_qty > settings.anomaly_spike_multiplier * daily_avg:
                    reason = (
                        f"Dispensing spike: {recent_qty} units in {scan_hours}h "
                        f"vs. 30-day daily avg of {daily_avg:.1f} "
                        f"(threshold ×{settings.anomaly_spike_multiplier})"
                    )
                    flagged.append({"drug_id": drug_id, "reason": reason, "qty": recent_qty})
                    spike_audit = self._make_audit_entry(
                        message,
                        ActionType.ANOMALY_FLAGGED,
                        entity_type="Drug",
                        entity_id=drug_id,
                        payload={
                            "drug_id": drug_id,
                            "recent_qty": recent_qty,
                            "daily_avg": round(daily_avg, 2),
                            "scan_hours": scan_hours,
                            "reason": reason,
                        },
                        is_anomaly=True,
                        anomaly_reason=reason,
                    )
                    self._write_audit(session, spike_audit)

            # ── Check 2: controlled-drug prescriptions with no dispense record ─
            orphan_rxs = self._find_orphan_controlled_rxs(
                session, message.facility_id, window_start
            )
            for rx_id, drug_name in orphan_rxs:
                reason = (
                    f"Controlled drug '{drug_name}' prescription {rx_id} "
                    "has no DISPENSING_COMPLETE audit entry within the scan window."
                )
                flagged.append({"prescription_id": rx_id, "drug_name": drug_name, "reason": reason})
                orphan_audit = self._make_audit_entry(
                    message,
                    ActionType.ANOMALY_FLAGGED,
                    entity_type="Prescription",
                    entity_id=rx_id,
                    payload={"prescription_id": rx_id, "drug_name": drug_name, "reason": reason},
                    is_anomaly=True,
                    anomaly_reason=reason,
                )
                self._write_audit(session, orphan_audit)

        self.logger.info(
            "Anomaly scan complete for %s — %d anomalies flagged", message.facility_id, len(flagged)
        )

        return self._ok(
            message,
            ActionType.ANOMALY_FLAGGED,
            result={"anomalies_flagged": len(flagged), "details": flagged},
        )

    async def _check_compliance(self, message: AgentMessage) -> AgentResponse:
        """Produce a controlled-drug compliance summary for the given period."""
        now = datetime.now(timezone.utc)
        from_ts = _parse_ts(message.payload.get("from_ts")) or (now - timedelta(hours=24))
        to_ts = _parse_ts(message.payload.get("to_ts")) or now
        filter_drug_id: str | None = message.payload.get("drug_id")

        with get_db() as session:
            rows = self._fetch_controlled_drug_audit(
                session, message.facility_id, from_ts, to_ts, filter_drug_id
            )

            # Group by drug
            by_drug: dict[str, list[dict]] = defaultdict(list)
            for row in rows:
                by_drug[row["drug_id"]].append(row)

            compliance_summary = {
                "facility_id": message.facility_id,
                "period_from": from_ts.isoformat(),
                "period_to": to_ts.isoformat(),
                "controlled_drugs_active": len(by_drug),
                "total_audit_entries": len(rows),
                "by_drug": {
                    drug_id: {
                        "entry_count": len(entries),
                        "action_types": list({e["action_type"] for e in entries}),
                    }
                    for drug_id, entries in by_drug.items()
                },
            }

            audit = self._make_audit_entry(
                message,
                ActionType.COMPLIANCE_CHECKED,
                entity_type="AuditLog",
                entity_id=filter_drug_id,
                payload=compliance_summary,
            )
            self._write_audit(session, audit)

        return self._ok(
            message,
            ActionType.COMPLIANCE_CHECKED,
            result=compliance_summary,
            audit_entry=audit,
        )

    # ── Query helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _collect_stats(
        session: Session, facility_id: str, from_ts: datetime, to_ts: datetime
    ) -> dict:
        """Aggregate prescription and audit counts for the report period."""
        rxs = (
            session.query(Prescription)
            .filter(
                Prescription.facility_id == facility_id,
                Prescription.created_at >= from_ts,
                Prescription.created_at <= to_ts,
            )
            .all()
        )
        status_counts: dict[str, int] = defaultdict(int)
        for rx in rxs:
            status_counts[rx.status] += 1

        total_audit = (
            session.query(func.count(AuditLog.id))
            .filter(
                AuditLog.facility_id == facility_id,
                AuditLog.timestamp >= from_ts,
                AuditLog.timestamp <= to_ts,
            )
            .scalar()
        )

        return {
            "prescriptions_total": len(rxs),
            "prescriptions_by_status": dict(status_counts),
            "audit_entries_total": total_audit or 0,
        }

    @staticmethod
    def _fetch_anomalies(
        session: Session, facility_id: str, from_ts: datetime, to_ts: datetime
    ) -> list[dict]:
        rows = (
            session.query(AuditLog)
            .filter(
                AuditLog.facility_id == facility_id,
                AuditLog.is_anomaly.is_(True),
                AuditLog.timestamp >= from_ts,
                AuditLog.timestamp <= to_ts,
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
        return [
            {
                "id": r.id,
                "action_type": r.action_type,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "anomaly_reason": r.anomaly_reason,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in rows
        ]

    @staticmethod
    def _count_dispensed_by_drug(
        session: Session,
        facility_id: str,
        from_ts: datetime,
        to_ts: datetime,
        drug_id: str | None,
    ) -> dict[str, int]:
        """
        Sum quantity_dispensed per drug for DISPENSED/PARTIALLY_DISPENSED
        prescriptions within the time window.
        """
        query = session.query(
            Prescription.drug_id,
            func.sum(Prescription.quantity_dispensed),
        ).filter(
            Prescription.facility_id == facility_id,
            Prescription.dispensed_at >= from_ts,
            Prescription.dispensed_at <= to_ts,
            Prescription.status.in_(
                [PrescriptionStatus.DISPENSED, PrescriptionStatus.PARTIALLY_DISPENSED]
            ),
        )
        if drug_id:
            query = query.filter(Prescription.drug_id == drug_id)
        rows = query.group_by(Prescription.drug_id).all()
        return {row[0]: int(row[1] or 0) for row in rows}

    @staticmethod
    def _find_orphan_controlled_rxs(
        session: Session, facility_id: str, since: datetime
    ) -> list[tuple[str, str]]:
        """
        Return (prescription_id, drug_name) pairs for controlled-drug prescriptions
        created since `since` that have no DISPENSING_COMPLETE audit entry.
        """
        rxs = (
            session.query(Prescription)
            .join(Drug, Drug.id == Prescription.drug_id)
            .filter(
                Prescription.facility_id == facility_id,
                Prescription.created_at >= since,
                Drug.is_controlled.is_(True),
            )
            .all()
        )

        dispensed_rx_ids = {
            row.entity_id
            for row in session.query(AuditLog)
            .filter(
                AuditLog.facility_id == facility_id,
                AuditLog.action_type == ActionType.DISPENSING_COMPLETE,
                AuditLog.entity_type == "Prescription",
                AuditLog.timestamp >= since,
            )
            .all()
        }

        result = []
        for rx in rxs:
            if rx.id not in dispensed_rx_ids:
                drug_name = rx.drug.name if rx.drug else rx.drug_id
                result.append((rx.id, drug_name))
        return result

    @staticmethod
    def _fetch_controlled_drug_audit(
        session: Session,
        facility_id: str,
        from_ts: datetime,
        to_ts: datetime,
        drug_id: str | None,
    ) -> list[dict]:
        query = (
            session.query(AuditLog)
            .join(Drug, Drug.id == AuditLog.entity_id, isouter=True)
            .filter(
                AuditLog.facility_id == facility_id,
                AuditLog.timestamp >= from_ts,
                AuditLog.timestamp <= to_ts,
            )
        )
        if drug_id:
            query = query.filter(AuditLog.entity_id == drug_id)
        else:
            # Only entries that reference a controlled drug
            controlled_ids = {
                d.id
                for d in session.query(Drug).filter(Drug.is_controlled.is_(True)).all()
            }
            if not controlled_ids:
                return []
            query = query.filter(AuditLog.entity_id.in_(controlled_ids))

        return [
            {
                "id": r.id,
                "drug_id": r.entity_id,
                "action_type": r.action_type,
                "agent_role": r.agent_role,
                "timestamp": r.timestamp.isoformat(),
                "is_anomaly": r.is_anomaly,
            }
            for r in query.order_by(AuditLog.timestamp).all()
        ]

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

    def _call_llm_for_report(self, stats: dict, anomalies: list[dict]) -> dict:
        """
        Ask the LLM to produce a narrative compliance summary.
        Returns an empty dict if no API key is configured or the call fails.
        """
        if not settings.openai_api_key:
            return {}

        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            user_msg = (
                f"Audit statistics: {json.dumps(stats)}. "
                f"Anomalies detected: {json.dumps(anomalies)}. "
                "Produce a compliance analysis."
            )
            response = client.chat.completions.create(
                model=settings.llm_model,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": GOV_OFFICIAL_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            return json.loads(raw)

        except Exception as exc:
            self.logger.warning("LLM call failed for compliance report: %s", exc)
            return {}


# ── Utility ────────────────────────────────────────────────────────────────────

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
