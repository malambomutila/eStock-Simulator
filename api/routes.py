"""
FastAPI application: REST API, shared EventBus + orchestrator, WebSocket stream.

``main.py --serve`` loads this module via ``uvicorn api.routes:app``.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from api.schemas import (
    ActivityListResponse,
    AlertOut,
    AlertsResponse,
    AuditLogListResponse,
    AuditLogOut,
    DrugCatalogOut,
    DrugListResponse,
    HealthResponse,
    PrescriptionListResponse,
    PrescriptionOut,
    StockBatchOut,
    StockDrugSummaryOut,
    StockListResponse,
)
from api.websocket import create_websocket_router
from config.settings import settings
from core.database import get_db_session, init_db, verify_connection
from core.event_bus import EventBus
from core.models import ActionType, AgentRole, AuditLog, Drug, Prescription, StockLevel
from core.orchestrator import build_default_orchestrator

logger = logging.getLogger(__name__)

try:
    _APP_VERSION = pkg_version("estock-simulator")
except PackageNotFoundError:
    _APP_VERSION = "0.1.0"

# Process-wide bus: WebSocket and orchestrator must share this instance.
_shared_bus = EventBus()


def _parse_audit_date(value: str | None, *, end_of_day: bool) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_utc(dt: datetime) -> datetime:
    """SQLite often returns naive datetimes; treat as UTC for comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bus = _shared_bus
    init_db()
    app.state.orchestrator = build_default_orchestrator(bus=_shared_bus)
    logger.info("Orchestrator ready (shared EventBus)")
    yield


app = FastAPI(title="eStock Simulator API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(create_websocket_router(_shared_bus))


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    db_ok = verify_connection()
    return HealthResponse(ok=db_ok, database=db_ok, version=_APP_VERSION)


def _audit_base_query(
    facility_id: str,
    *,
    role: str | None,
    action: str | None,
    anomaly: str | None,
    from_date: str | None,
    to_date: str | None,
) -> Select:
    q = select(AuditLog).where(AuditLog.facility_id == facility_id)
    if role and role.strip() and role.strip() != "All":
        try:
            q = q.where(AuditLog.agent_role == AgentRole(role.strip()))
        except ValueError:
            pass
    if action and action.strip() and action.strip() != "All":
        try:
            q = q.where(AuditLog.action_type == ActionType(action.strip()))
        except ValueError:
            pass
    if anomaly == "true":
        q = q.where(AuditLog.is_anomaly.is_(True))
    elif anomaly == "false":
        q = q.where(AuditLog.is_anomaly.is_(False))
    from_ts = _parse_audit_date(from_date, end_of_day=False)
    to_ts = _parse_audit_date(to_date, end_of_day=True)
    if from_ts:
        q = q.where(AuditLog.timestamp >= from_ts)
    if to_ts:
        q = q.where(AuditLog.timestamp <= to_ts)
    return q


@app.get(
    "/api/facilities/{facility_id}/stock",
    response_model=StockListResponse,
    tags=["stock"],
)
def get_facility_stock(
    facility_id: str,
    db: Annotated[Session, Depends(get_db_session)],
    include_batches: bool = Query(False, description="Return per-batch rows instead of per-drug totals"),
) -> StockListResponse:
    if include_batches:
        stmt = (
            select(StockLevel, Drug)
            .join(Drug, Drug.id == StockLevel.drug_id)
            .where(StockLevel.facility_id == facility_id)
            .order_by(Drug.name, StockLevel.expiry_date)
        )
        rows = db.execute(stmt).all()
        batches: list[StockBatchOut] = []
        for sl, drug in rows:
            batches.append(
                StockBatchOut(
                    drug_id=drug.id,
                    drug_name=drug.name,
                    stock_level_id=sl.id,
                    batch_number=sl.batch_number,
                    quantity=sl.quantity,
                    expiry_date=sl.expiry_date,
                    is_quarantined=sl.is_quarantined,
                    unit_cost=sl.unit_cost,
                    location_in_store=sl.location_in_store,
                )
            )
        return StockListResponse(
            facility_id=facility_id,
            include_batches=True,
            batches=batches,
        )

    subq = (
        select(
            StockLevel.drug_id.label("drug_id"),
            func.sum(StockLevel.quantity).label("total_qty"),
        )
        .where(
            StockLevel.facility_id == facility_id,
            StockLevel.is_quarantined.is_(False),
        )
        .group_by(StockLevel.drug_id)
        .subquery()
    )
    stmt = (
        select(Drug, subq.c.total_qty)
        .join(subq, subq.c.drug_id == Drug.id)
        .order_by(Drug.name)
    )
    drugs_out: list[StockDrugSummaryOut] = []
    for drug, total_qty in db.execute(stmt).all():
        drugs_out.append(
            StockDrugSummaryOut(
                drug_id=drug.id,
                drug_name=drug.name,
                generic_name=drug.generic_name,
                strength=drug.strength,
                dosage_form=drug.dosage_form,
                unit=drug.unit,
                reorder_threshold=drug.reorder_threshold,
                total_quantity=int(total_qty or 0),
            )
        )
    return StockListResponse(
        facility_id=facility_id,
        include_batches=False,
        drugs=drugs_out,
    )


@app.get(
    "/api/facilities/{facility_id}/prescriptions",
    response_model=PrescriptionListResponse,
    tags=["prescriptions"],
)
def get_facility_prescriptions(
    facility_id: str,
    db: Annotated[Session, Depends(get_db_session)],
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PrescriptionListResponse:
    count_stmt = (
        select(func.count()).select_from(Prescription).where(Prescription.facility_id == facility_id)
    )
    total = db.execute(count_stmt).scalar_one()
    stmt = (
        select(Prescription)
        .where(Prescription.facility_id == facility_id)
        .order_by(Prescription.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    items = [
        PrescriptionOut(
            id=r.id,
            drug_id=r.drug_id,
            patient_id=r.patient_id,
            doctor_agent_id=r.doctor_agent_id,
            quantity_prescribed=r.quantity_prescribed,
            quantity_dispensed=r.quantity_dispensed,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            diagnosis_code=r.diagnosis_code,
            created_at=r.created_at,
            dispensed_at=r.dispensed_at,
        )
        for r in rows
    ]
    return PrescriptionListResponse(
        facility_id=facility_id,
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


@app.get(
    "/api/facilities/{facility_id}/audit-log",
    response_model=AuditLogListResponse,
    tags=["audit"],
)
def get_facility_audit_log(
    facility_id: str,
    db: Annotated[Session, Depends(get_db_session)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    role: str | None = Query(None),
    action: str | None = Query(None),
    anomaly: str | None = Query(
        None,
        description="Omit for all rows, or 'true' / 'false' for anomalies only / normal only",
    ),
    from_date: str | None = Query(None, description="YYYY-MM-DD"),
    to_date: str | None = Query(None, description="YYYY-MM-DD"),
) -> AuditLogListResponse:
    base = _audit_base_query(
        facility_id,
        role=role,
        action=action,
        anomaly=anomaly,
        from_date=from_date,
        to_date=to_date,
    )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = db.execute(count_stmt).scalar_one()
    stmt = (
        base.order_by(AuditLog.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(stmt).scalars().all()
    items = [
        AuditLogOut(
            id=r.id,
            facility_id=r.facility_id,
            agent_id=r.agent_id,
            agent_role=r.agent_role.value,
            action_type=r.action_type.value,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            payload=r.payload or {},
            is_anomaly=r.is_anomaly,
            anomaly_reason=r.anomaly_reason,
            correlation_id=r.correlation_id,
            timestamp=r.timestamp,
        )
        for r in rows
    ]
    return AuditLogListResponse(
        facility_id=facility_id,
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


@app.get(
    "/api/facilities/{facility_id}/activity",
    response_model=ActivityListResponse,
    tags=["activity"],
)
def get_facility_activity(
    facility_id: str,
    db: Annotated[Session, Depends(get_db_session)],
    limit: int = Query(50, ge=1, le=200),
) -> ActivityListResponse:
    stmt = (
        select(AuditLog)
        .where(AuditLog.facility_id == facility_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    items = [
        AuditLogOut(
            id=r.id,
            facility_id=r.facility_id,
            agent_id=r.agent_id,
            agent_role=r.agent_role.value,
            action_type=r.action_type.value,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            payload=r.payload or {},
            is_anomaly=r.is_anomaly,
            anomaly_reason=r.anomaly_reason,
            correlation_id=r.correlation_id,
            timestamp=r.timestamp,
        )
        for r in rows
    ]
    return ActivityListResponse(facility_id=facility_id, limit=limit, items=items)


@app.get(
    "/api/facilities/{facility_id}/alerts",
    response_model=AlertsResponse,
    tags=["alerts"],
)
def get_facility_alerts(
    facility_id: str,
    db: Annotated[Session, Depends(get_db_session)],
) -> AlertsResponse:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=settings.reorder_alert_threshold_days)
    low_frac = settings.low_stock_threshold_pct

    stmt_batches = (
        select(StockLevel, Drug)
        .join(Drug, Drug.id == StockLevel.drug_id)
        .where(StockLevel.facility_id == facility_id)
    )
    rows = db.execute(stmt_batches).all()
    alerts: list[AlertOut] = []

    totals: dict[str, tuple[Drug, int]] = {}
    for sl, drug in rows:
        if sl.is_quarantined:
            continue
        cur = totals.get(drug.id, (drug, 0))
        totals[drug.id] = (drug, cur[1] + sl.quantity)

    for drug_id, (drug, total_avail) in totals.items():
        threshold = max(1, drug.reorder_threshold)
        if total_avail < threshold * low_frac:
            alerts.append(
                AlertOut(
                    alert_type="low_stock",
                    drug_id=drug.id,
                    drug_name=drug.name,
                    detail=(
                        f"Available {total_avail} units is below "
                        f"{low_frac:.0%} of reorder threshold ({threshold})."
                    ),
                    current_quantity=total_avail,
                )
            )

    for sl, drug in rows:
        expiry = _as_utc(sl.expiry_date)
        if expiry < now:
            alerts.append(
                AlertOut(
                    alert_type="expired",
                    drug_id=drug.id,
                    drug_name=drug.name,
                    detail=f"Batch {sl.batch_number} expired on {expiry.date().isoformat()}.",
                    batch_id=sl.id,
                    expiry_date=expiry,
                    current_quantity=sl.quantity,
                )
            )
        elif expiry <= horizon:
            alerts.append(
                AlertOut(
                    alert_type="expiry_soon",
                    drug_id=drug.id,
                    drug_name=drug.name,
                    detail=(
                        f"Batch {sl.batch_number} expires on {expiry.date().isoformat()} "
                        f"(within {settings.reorder_alert_threshold_days} days)."
                    ),
                    batch_id=sl.id,
                    expiry_date=expiry,
                    current_quantity=sl.quantity,
                )
            )

    return AlertsResponse(facility_id=facility_id, items=alerts)


# ── Simulation endpoints ───────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    scenario: str = "Full Day"


class SimulateStartResponse(BaseModel):
    run_id: str
    scenario: str
    status: str


class SimulateFeedResponse(BaseModel):
    run_id: str
    scenario: str
    status: str
    started_at: str
    error: str | None
    events: list[dict]


@app.post("/api/simulate", response_model=SimulateStartResponse, tags=["simulate"])
def post_simulate(body: SimulateRequest) -> SimulateStartResponse:
    """Start a simulation scenario in the background. Returns run_id for polling."""
    from core.simulation_engine import SCENARIOS, start_simulation

    scenario = body.scenario if body.scenario in SCENARIOS else "Full Day"
    run_id = start_simulation(scenario)
    return SimulateStartResponse(run_id=run_id, scenario=scenario, status="starting")


@app.get(
    "/api/simulate/{run_id}/feed",
    response_model=SimulateFeedResponse,
    tags=["simulate"],
)
def get_simulate_feed(run_id: str) -> SimulateFeedResponse:
    """Poll the live event stream for a running or completed simulation."""
    from core.simulation_engine import get_run

    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Simulation run '{run_id}' not found.")
    snap = run.snapshot()
    return SimulateFeedResponse(**snap)


@app.get("/api/simulate", tags=["simulate"])
def list_simulations() -> dict:
    """List all simulation runs in this process session."""
    from core.simulation_engine import list_runs
    return {"runs": list_runs()}


@app.post("/api/shutdown", tags=["control"])
def shutdown_application() -> dict:
    """
    Gracefully stop the entire application (API + Dashboard).

    Sends SIGTERM to the estock.py launcher process group, which triggers its
    _shutdown() handler and terminates all child processes.  Falls back to
    killing the API process itself if the launcher PID is unavailable.
    """
    launcher_pid_str = os.environ.get("ESTOCK_LAUNCHER_PID", "")

    def _do_shutdown() -> None:
        import time
        time.sleep(0.3)  # let the HTTP response reach the client first
        try:
            if launcher_pid_str:
                pid = int(launcher_pid_str)
                os.kill(pid, signal.SIGTERM)
            else:
                # No launcher PID — kill the whole process group
                os.kill(0, signal.SIGTERM)
        except Exception:
            # Last resort: kill own process, estock.py watch-loop will detect it
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "shutting_down", "message": "Application shutdown initiated."}


@app.get("/api/drugs", response_model=DrugListResponse, tags=["drugs"])
def list_drugs(
    db: Annotated[Session, Depends(get_db_session)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> DrugListResponse:
    count_stmt = select(func.count()).select_from(Drug)
    total = db.execute(count_stmt).scalar_one()
    stmt = select(Drug).order_by(Drug.name).offset(offset).limit(limit)
    rows = db.execute(stmt).scalars().all()
    items = [
        DrugCatalogOut(
            id=d.id,
            name=d.name,
            generic_name=d.generic_name,
            category=d.category.value,
            dosage_form=d.dosage_form,
            strength=d.strength,
            unit=d.unit,
            reorder_threshold=d.reorder_threshold,
            is_controlled=d.is_controlled,
            is_active=d.is_active,
        )
        for d in rows
    ]
    return DrugListResponse(total=total, limit=limit, offset=offset, items=items)
