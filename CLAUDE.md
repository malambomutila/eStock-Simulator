# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**eStock Simulator** is an AI-powered pharmaceutical supply chain simulator for healthcare facilities. Multiple autonomous AI agents (Doctor, Dispenser, Stock Manager, Government Auditor) coordinate through a central orchestrator against a shared SQLite drug inventory database. The system provides real-time visibility into drug stock, prescriptions, dispensing, restocking, and audit compliance.

## Commands

**Package manager**: `uv` (preferred)

```bash
# Install dependencies
uv sync

# Run health check / verify DB connection
python main.py

# Seed database (first-time setup)
python main.py --seed

# Seed with full reset (drop + recreate + seed)
python main.py --seed-reset

# Start FastAPI backend (port 8000)
python main.py --serve

# Launch Gradio dashboard
python main.py --dash

# Run tests
pytest tests/ -v

# Run a single test
pytest tests/test_orchestrator.py::test_followup_dispense_message_builds_dispenser_envelope -v

# Orchestrator dry run (doctor → auto-dispense chain; use --seed on first run)
PYTHONPATH=. python scripts/orchestrator_dry_run.py --seed

# Full-day simulation (all agents; seed DB on first run)
python scripts/run_simulation.py --seed
python scripts/run_simulation.py --seed --verbose

# Curated live demo (5 scenes; use --pause to narrate alongside the dashboard)
python scripts/demo_scenario.py --seed
python scripts/demo_scenario.py --pause
```

## Team status

All 5 engineering tracks have landed. Use `build_default_orchestrator()` in `core/orchestrator.py` to register all agents (`DoctorAgent`, `GovOfficialAgent`, `DispenserAgent`, `StockManagerAgent`). The full end-to-end simulation runs via `scripts/run_simulation.py`.

`scripts/demo_scenario.py` is the curated 5-scene narrative demo for live presentations. All milestones are complete.

## Architecture

### Core Flow
```
Doctor Agent → creates Prescription → Orchestrator dispatches → Dispenser Agent deducts stock
                                                              → Stock Manager monitors levels
Government Official Agent → audits AuditLog for anomalies
Dashboard (Gradio) ← Event Bus ← all agent actions
```

### Key Design Patterns

**Agent message passing**: All agents communicate via typed `AgentMessage` / `AgentResponse` envelopes (defined in `core/schemas.py`). The orchestrator routes by `recipient_role` and `action_type` (see `Orchestrator.dispatch_immediate` / `submit`). Agents that touch shared stock set `requires_lock=True` so the orchestrator acquires a per-`(facility_id, drug_id)` lock before dispatch.

**FEFO dispensing**: The dispenser always consumes stock from the earliest-expiring batch first (`ORDER BY expiry_date ASC`) to minimize waste.

**Immutable audit log**: Every action writes to `AuditLog` with a full JSON payload snapshot, anomaly flag, and `correlation_id` linking related actions. Never update or delete audit rows.

**Facility isolation**: All data carries `facility_id` — the simulator defaults to `FACILITY-001` (configurable in `.env`).

### Module Map

| Path | Purpose |
|------|---------|
| `core/models.py` | SQLAlchemy ORM: Drug, StockLevel, Prescription, AuditLog |
| `core/schemas.py` | Pydantic v2 schemas; AgentMessage/AgentResponse envelopes |
| `core/database.py` | Engine, SessionLocal, `get_db()`, `init_db()` |
| `core/orchestrator.py` | Central dispatcher, priority queue, stock locks, Rx→dispense follow-up |
| `core/event_bus.py` | In-process pub/sub (`estock.agent.response`, `estock.orchestrator.task.completed`) |
| `agents/base_agent.py` | Abstract base; `handle()`, `_ok()`, `_fail()`, `_make_audit_entry()` |
| `agents/dispenser_agent.py` | Complete reference implementation of an agent |
| `config/settings.py` | Pydantic BaseSettings; loads `.env`; exposes `settings` singleton |
| `config/prompts.py` | LLM system prompts (doctor + government official) |
| `data/drugs_catalog.json` | 50+ medicines; used by `scripts/seed_db.py` |
| `data/seed_stock.json` | Initial batch inventory; used by `scripts/seed_db.py` |
| `api/routes.py` | FastAPI app: health, facility stock/prescriptions/audit/activity/alerts, drugs; shared `EventBus` + `build_default_orchestrator` in lifespan |
| `api/websocket.py` | WebSocket `/ws/events` — subscribes to event bus topics, pushes JSON to clients |
| `dashboard/app.py` | Gradio tabs (audit, stock, alerts, activity) polling the REST API |
| `agents/stock_manager_agent.py` | Stock receipts (upsert batches), expiry scan + quarantine, reorder threshold sweep, restock-request acknowledgement |
| `scripts/orchestrator_dry_run.py` | Eng 2 — scripted Rx→dispense smoke run via orchestrator |
| `scripts/run_simulation.py` | Full-day simulation: 3 stock receipts, 15 Rx → dispense, 2 expiry scans, reorder check, anomaly detection, audit report |

### Database Schema Highlights

- **Drug**: catalog entry with `is_controlled` flag (affects audit scrutiny) and `reorder_threshold`
- **StockLevel**: per-batch inventory; unique on `(drug_id, facility_id, batch_number)`; `is_quarantined` blocks dispensing
- **Prescription**: tracks lifecycle via `PrescriptionStatus` enum (PENDING → DISPENSED / PARTIALLY_DISPENSED / OUT_OF_STOCK / CANCELLED)
- **AuditLog**: append-only; has `is_anomaly` + `anomaly_reason` fields; indexed on `facility_id`, `agent_role`, `action_type`, `timestamp`

### Environment Configuration (`.env`)

Copy `.env.example` to `.env`. Key variables:

```
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
DATABASE_URL=sqlite:///data/estock.db
SIMULATION_FACILITY_ID=FACILITY-001
LOW_STOCK_THRESHOLD_PCT=0.20
REORDER_ALERT_THRESHOLD_DAYS=90
ANOMALY_SPIKE_MULTIPLIER=3.0
```

## Implementing a New Agent

Follow `agents/dispenser_agent.py` as the reference:

1. Subclass `BaseAgent` and set `role = AgentRole.<YOUR_ROLE>`
2. Implement `async handle(message: AgentMessage) -> AgentResponse`
3. Call `_write_audit()` (or equivalent) for every significant action
4. Return `self._ok(result=...)` or `self._fail(error=...)` — never raise from `handle()`
5. Set `requires_lock=True` on messages that modify shared stock

## Completion Status

Sourced from `stock_management_task_plan.xlsx` (4 phases, 3 milestones).

| File | Owner | Phase | Status | Notes |
|------|-------|-------|--------|-------|
| `core/models.py` | Eng 1 | 1 | ✓ Done | ORM fully defined |
| `core/database.py` | Eng 1 | 1 | ✓ Done | Engine, sessions, init |
| `core/schemas.py` | Eng 1 | 1 | ✓ Done | Pydantic schemas + AgentMessage envelopes |
| `scripts/seed_db.py` | Eng 1 | 1 | ✓ Done | Seeds 50+ drugs + stock batches |
| `agents/dispenser_agent.py` | Eng 1 | 2 | ✓ Done | FEFO dispensing, audit logging |
| `core/orchestrator.py` | Eng 2 | 1 | ✓ Done | Task queue, dispatcher, stock locking, follow-ups |
| `core/event_bus.py` | Eng 2 | 1 | ✓ Done | Pub/sub topics for Eng 5 bridge |
| `agents/doctor_agent.py` | Eng 3 | 1 | ✓ Done | Create Rx, check availability, request restock |
| `config/prompts.py` | Eng 3 | 1 | ✓ Done | LLM system prompts for doctor + gov official |
| `agents/gov_official_agent.py` | Eng 3 | 2 | ✓ Done | Audit queries, flag anomalies, compliance report |
| `scripts/demo_scenario.py` | Eng 3 | 3 | ✓ Done | 5-scene narrative demo: stock delivery → Rx → dispense → expiry/reorder → gov audit |
| `agents/stock_manager_agent.py` | Eng 4 | 1 | ✓ Done | Receive stock, expiry scan + quarantine, reorder alerts, restock-request handling |
| `scripts/run_simulation.py` | Eng 4 | 2 | ✓ Done | 15 Rx, 3 receipts, 2 expiry scans, reorder check, anomaly detection, audit report |
| `api/routes.py` | Eng 5 | 1 | ✓ Done | FastAPI REST + CORS + `Depends(get_db_session)`; `tests/test_api.py` |
| `dashboard/app.py` | Eng 5 | 2 | ✓ Done | Multi-tab Gradio; uses `settings.api_port` / `simulation_facility_id` |
| `api/websocket.py` | Eng 5 | 2 | ✓ Done | Bus → WebSocket bridge; `scripts/ws_smoke_test.py` for manual smoke |

**Milestones**:
- Milestone 1 (Phase 1 end): All components boot independently — ✓ Complete
- Milestone 2 (Phase 2 end): End-to-end flow works — ✓ Complete
- Milestone 3 (Phase 3 end): Demo-ready system — ✓ Complete
