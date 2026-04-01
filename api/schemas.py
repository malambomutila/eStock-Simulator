"""Pydantic models for HTTP JSON responses (stable for dashboard clients)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    database: bool
    version: str


class StockBatchOut(BaseModel):
    drug_id: str
    drug_name: str
    stock_level_id: str
    batch_number: str
    quantity: int
    expiry_date: datetime
    is_quarantined: bool
    unit_cost: float | None = None
    location_in_store: str | None = None


class StockDrugSummaryOut(BaseModel):
    drug_id: str
    drug_name: str
    generic_name: str
    strength: str
    dosage_form: str
    unit: str
    reorder_threshold: int
    total_quantity: int


class StockListResponse(BaseModel):
    facility_id: str
    include_batches: bool
    drugs: list[StockDrugSummaryOut] = Field(default_factory=list)
    batches: list[StockBatchOut] = Field(default_factory=list)


class PrescriptionOut(BaseModel):
    id: str
    drug_id: str
    patient_id: str
    doctor_agent_id: str
    quantity_prescribed: int
    quantity_dispensed: int
    status: str
    diagnosis_code: str | None
    created_at: datetime
    dispensed_at: datetime | None

    model_config = {"from_attributes": True}


class PrescriptionListResponse(BaseModel):
    facility_id: str
    total: int
    limit: int
    offset: int
    items: list[PrescriptionOut]


class AuditLogOut(BaseModel):
    id: str
    facility_id: str
    agent_id: str
    agent_role: str
    action_type: str
    entity_type: str | None
    entity_id: str | None
    payload: dict[str, Any]
    is_anomaly: bool
    anomaly_reason: str | None
    correlation_id: str | None
    timestamp: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    facility_id: str
    total: int
    page: int
    page_size: int
    items: list[AuditLogOut]


class ActivityListResponse(BaseModel):
    facility_id: str
    limit: int
    items: list[AuditLogOut]


class AlertOut(BaseModel):
    alert_type: Literal["low_stock", "expiry_soon", "expired"]
    drug_id: str
    drug_name: str
    detail: str
    batch_id: str | None = None
    expiry_date: datetime | None = None
    current_quantity: int | None = None


class AlertsResponse(BaseModel):
    facility_id: str
    items: list[AlertOut]


class DrugCatalogOut(BaseModel):
    id: str
    name: str
    generic_name: str
    category: str
    dosage_form: str
    strength: str
    unit: str
    reorder_threshold: int
    is_controlled: bool
    is_active: bool

    model_config = {"from_attributes": True}


class DrugListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DrugCatalogOut]
