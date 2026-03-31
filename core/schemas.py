"""
Pydantic v2 schemas for eStock-Simulator.

Three groups:
  1. Domain schemas   — request/response shapes for each ORM model.
  2. Agent message envelope — the common structure the orchestrator routes.
  3. API response wrappers  — paginated list, error envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.models import (
    ActionType,
    AgentRole,
    DrugCategory,
    PrescriptionStatus,
)

T = TypeVar("T")


# Shared helpers
class _OrmBase(BaseModel):
    """Base for all schemas that mirror ORM rows."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# Drug schemas
class DrugBase(_OrmBase):
    name: str = Field(..., max_length=200)
    generic_name: str = Field(..., max_length=200)
    category: DrugCategory
    dosage_form: str = Field(..., max_length=100)
    strength: str = Field(..., max_length=50)
    unit: str = Field(..., max_length=30)
    reorder_threshold: int = Field(default=100, ge=1)
    is_controlled: bool = False
    is_active: bool = True


class DrugCreate(DrugBase):
    pass


class DrugUpdate(_OrmBase):
    reorder_threshold: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class DrugSchema(DrugBase):
    id: str
    created_at: datetime


# StockLevel schemas
class StockLevelBase(_OrmBase):
    drug_id: str
    facility_id: str
    batch_number: str = Field(..., max_length=100)
    quantity: int = Field(..., ge=0)
    unit_cost: float | None = None
    expiry_date: datetime
    manufactured_date: datetime | None = None
    supplier: str | None = None
    location_in_store: str | None = None
    is_quarantined: bool = False


class StockLevelCreate(StockLevelBase):
    pass


class StockLevelUpdate(_OrmBase):
    quantity: int | None = Field(default=None, ge=0)
    is_quarantined: bool | None = None
    location_in_store: str | None = None


class StockLevelSchema(StockLevelBase):
    id: str
    created_at: datetime
    updated_at: datetime


class StockSummary(_OrmBase):
    """Rolled-up view of total available stock for a drug at a facility."""

    drug_id: str
    drug_name: str
    facility_id: str
    total_quantity: int
    earliest_expiry: datetime | None
    batch_count: int
    is_low_stock: bool


# Prescription schemas
class PrescriptionCreate(_OrmBase):
    drug_id: str
    facility_id: str
    patient_id: str = Field(..., max_length=100)
    doctor_agent_id: str
    quantity_prescribed: int = Field(..., ge=1)
    diagnosis_code: str | None = Field(default=None, max_length=20)
    notes: str | None = None

    @field_validator("quantity_prescribed")
    @classmethod
    def positive_quantity(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity_prescribed must be a positive integer")
        return v


class PrescriptionUpdate(_OrmBase):
    status: PrescriptionStatus | None = None
    quantity_dispensed: int | None = Field(default=None, ge=0)
    dispensed_by_agent_id: str | None = None
    dispensed_at: datetime | None = None
    notes: str | None = None


class PrescriptionSchema(_OrmBase):
    id: str
    drug_id: str
    facility_id: str
    patient_id: str
    doctor_agent_id: str
    quantity_prescribed: int
    quantity_dispensed: int
    diagnosis_code: str | None
    notes: str | None
    status: PrescriptionStatus
    dispensed_by_agent_id: str | None
    dispensed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# AuditLog schemas
class AuditLogCreate(_OrmBase):
    facility_id: str
    agent_id: str
    agent_role: AgentRole
    action_type: ActionType
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    is_anomaly: bool = False
    anomaly_reason: str | None = None
    correlation_id: str | None = None


class AuditLogSchema(AuditLogCreate):
    id: str
    timestamp: datetime


class AuditLogFilter(_OrmBase):
    """Query parameters for filtering the audit log."""

    facility_id: str | None = None
    agent_role: AgentRole | None = None
    action_type: ActionType | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    is_anomaly: bool | None = None
    correlation_id: str | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None


# Agent message envelope
class AgentMessage(BaseModel):
    """
    The canonical message format exchanged between agents and the orchestrator.

    Every agent call produces and consumes this envelope so the orchestrator
    can route, log, and conflict-check without inspecting message-specific fields.
    """

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Shared across all messages in one orchestrator task.",
    )
    sender_role: AgentRole
    sender_id: str
    recipient_role: AgentRole | None = None
    action_type: ActionType
    facility_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="1 = lowest, 10 = highest. Used by the orchestrator task queue.",
    )
    requires_lock: bool = Field(
        default=False,
        description="If True, the orchestrator must acquire a stock lock before dispatch.",
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now())

    model_config = ConfigDict(use_enum_values=True)


class AgentResponse(BaseModel):
    """Standardised reply returned by every agent action handler."""

    message_id: str
    correlation_id: str
    success: bool
    action_type: ActionType
    agent_id: str
    agent_role: AgentRole
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    audit_entry: AuditLogCreate | None = None

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def error_requires_failure(self) -> AgentResponse:
        if self.error and self.success:
            raise ValueError("An error message implies success=False.")
        return self


# Dispensing-specific payloads
class DispenseRequest(BaseModel):
    """Payload the orchestrator puts into an AgentMessage for the Dispenser."""

    prescription_id: str
    drug_id: str
    quantity_requested: int = Field(..., ge=1)
    facility_id: str
    doctor_agent_id: str
    correlation_id: str


class DispenseResult(BaseModel):
    """Returned in AgentResponse.result after a dispensing action."""

    prescription_id: str
    drug_id: str
    quantity_dispensed: int
    batch_number: str | None
    stock_remaining: int
    status: PrescriptionStatus


# API response wrappers
class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    correlation_id: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    database: bool
    version: str = "0.1.0"
