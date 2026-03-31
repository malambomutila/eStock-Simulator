"""
SQLAlchemy ORM models for eStock-Simulator.

Table hierarchy:
    Drug  ──< StockLevel
    Drug  ──< Prescription
    AuditLog  (append-only; no FK cascade deletes)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


# Enumerations
class PrescriptionStatus(str, PyEnum):
    PENDING = "PENDING"
    DISPENSED = "DISPENSED"
    PARTIALLY_DISPENSED = "PARTIALLY_DISPENSED"
    CANCELLED = "CANCELLED"
    OUT_OF_STOCK = "OUT_OF_STOCK"


class ActionType(str, PyEnum):
    # Doctor actions
    PRESCRIPTION_CREATED = "PRESCRIPTION_CREATED"
    AVAILABILITY_CHECKED = "AVAILABILITY_CHECKED"
    RESTOCK_REQUESTED = "RESTOCK_REQUESTED"

    # Dispenser actions
    DISPENSING_STARTED = "DISPENSING_STARTED"
    DISPENSING_COMPLETE = "DISPENSING_COMPLETE"
    DISPENSING_FAILED = "DISPENSING_FAILED"
    STOCK_DEDUCTED = "STOCK_DEDUCTED"

    # Stock Manager actions
    STOCK_RECEIVED = "STOCK_RECEIVED"
    EXPIRY_CHECKED = "EXPIRY_CHECKED"
    REORDER_ALERT_RAISED = "REORDER_ALERT_RAISED"
    BATCH_EXPIRED = "BATCH_EXPIRED"

    # Gov Official actions
    AUDIT_REPORT_GENERATED = "AUDIT_REPORT_GENERATED"
    ANOMALY_FLAGGED = "ANOMALY_FLAGGED"
    COMPLIANCE_CHECKED = "COMPLIANCE_CHECKED"

    # Orchestrator / system
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class AgentRole(str, PyEnum):
    DOCTOR = "DOCTOR"
    DISPENSER = "DISPENSER"
    STOCK_MANAGER = "STOCK_MANAGER"
    GOV_OFFICIAL = "GOV_OFFICIAL"
    ORCHESTRATOR = "ORCHESTRATOR"
    SYSTEM = "SYSTEM"


class DrugCategory(str, PyEnum):
    ANTIBIOTIC = "ANTIBIOTIC"
    ANALGESIC = "ANALGESIC"
    ANTIMALARIAL = "ANTIMALARIAL"
    ANTIVIRAL = "ANTIVIRAL"
    ANTIFUNGAL = "ANTIFUNGAL"
    ANTIHYPERTENSIVE = "ANTIHYPERTENSIVE"
    ANTIDIABETIC = "ANTIDIABETIC"
    VACCINE = "VACCINE"
    VITAMIN_SUPPLEMENT = "VITAMIN_SUPPLEMENT"
    ANTIPARASITIC = "ANTIPARASITIC"
    RESPIRATORY = "RESPIRATORY"
    GASTROINTESTINAL = "GASTROINTESTINAL"
    CARDIOVASCULAR = "CARDIOVASCULAR"
    NEUROLOGICAL = "NEUROLOGICAL"
    OTHER = "OTHER"


# Drug
class Drug(Base):
    """Master drug catalogue — one row per unique medicine."""

    __tablename__ = "drugs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    generic_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[DrugCategory] = mapped_column(
        Enum(DrugCategory), nullable=False, index=True
    )
    dosage_form: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="e.g. tablet, syrup, injection"
    )
    strength: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="e.g. 500mg, 250mg/5ml"
    )
    unit: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="e.g. tablet, vial, bottle"
    )
    reorder_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100,
        comment="Minimum stock level before a reorder alert is raised"
    )
    is_controlled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Controlled/scheduled drug requiring extra audit scrutiny"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    stock_levels: Mapped[list[StockLevel]] = relationship(
        "StockLevel", back_populates="drug", cascade="all, delete-orphan"
    )
    prescriptions: Mapped[list[Prescription]] = relationship(
        "Prescription", back_populates="drug"
    )

    __table_args__ = (
        UniqueConstraint("generic_name", "strength", "dosage_form", name="uq_drug_identity"),
    )

    def __repr__(self) -> str:
        return f"<Drug {self.generic_name} {self.strength} ({self.dosage_form})>"


# StockLevel
class StockLevel(Base):
    """
    Per-batch stock record at a given facility.

    A single drug may have multiple active batches with different expiry dates.
    Dispensing always consumes from the earliest-expiring batch first (FEFO).
    """

    __tablename__ = "stock_levels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    drug_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    facility_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    batch_number: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_cost: Mapped[float] = mapped_column(
        Float, nullable=True, comment="Cost per unit in local currency"
    )
    expiry_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manufactured_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supplier: Mapped[str] = mapped_column(String(200), nullable=True)
    location_in_store: Mapped[str] = mapped_column(
        String(100), nullable=True, comment="Shelf or bin reference"
    )
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Quarantined batches must not be dispensed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    drug: Mapped[Drug] = relationship("Drug", back_populates="stock_levels")

    __table_args__ = (
        UniqueConstraint("drug_id", "facility_id", "batch_number", name="uq_stock_batch"),
        Index("ix_stock_expiry", "expiry_date"),
        Index("ix_stock_facility_drug", "facility_id", "drug_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<StockLevel drug={self.drug_id} batch={self.batch_number} "
            f"qty={self.quantity} expires={self.expiry_date.date()}>"
        )


@event.listens_for(StockLevel, "before_update")
def _stamp_updated_at(mapper, connection, target: StockLevel):  # noqa: ARG001
    target.updated_at = _utcnow()


# Prescription
class Prescription(Base):
    """
    Rx record created by the Doctor agent.

    Links a patient encounter to a drug order; the Dispenser resolves it.
    """

    __tablename__ = "prescriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    drug_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drugs.id"), nullable=False, index=True
    )
    facility_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Anonymised patient identifier"
    )
    doctor_agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity_prescribed: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_dispensed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    diagnosis_code: Mapped[str] = mapped_column(
        String(20), nullable=True, comment="ICD-10 code"
    )
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[PrescriptionStatus] = mapped_column(
        Enum(PrescriptionStatus),
        nullable=False,
        default=PrescriptionStatus.PENDING,
        index=True,
    )
    dispensed_by_agent_id: Mapped[str] = mapped_column(String(100), nullable=True)
    dispensed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    drug: Mapped[Drug] = relationship("Drug", back_populates="prescriptions")

    __table_args__ = (
        Index("ix_rx_status_facility", "status", "facility_id"),
        Index("ix_rx_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Prescription id={self.id[:8]} drug={self.drug_id[:8]} "
            f"qty={self.quantity_prescribed} status={self.status}>"
        )


# AuditLog
class AuditLog(Base):
    """
    Immutable, append-only event log.

    Every agent action — successful or failed — is recorded here. No rows
    are ever updated or deleted; this table is the source of truth for
    compliance reporting and anomaly detection.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    facility_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Logical identifier of the agent instance that performed this action"
    )
    agent_role: Mapped[AgentRole] = mapped_column(
        Enum(AgentRole), nullable=False, index=True
    )
    action_type: Mapped[ActionType] = mapped_column(
        Enum(ActionType), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=True,
        comment="Name of the affected ORM model, e.g. 'Prescription', 'StockLevel'"
    )
    entity_id: Mapped[str] = mapped_column(
        String(36), nullable=True,
        comment="Primary key of the affected row"
    )
    payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="Full context snapshot at the time of the action"
    )
    is_anomaly: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    anomaly_reason: Mapped[str] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str] = mapped_column(
        String(36), nullable=True, index=True,
        comment="Links multiple log entries that belong to the same orchestrator task"
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        Index("ix_audit_role_action", "agent_role", "action_type"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_timestamp_facility", "timestamp", "facility_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog {self.agent_role}:{self.action_type} "
            f"entity={self.entity_type}/{self.entity_id} ts={self.timestamp}>"
        )
