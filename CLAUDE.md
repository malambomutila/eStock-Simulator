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
```

## Team status vs Eng 3 PR notes

Eng 2 has landed the orchestrator (priority queue, role routing, stock locks, doctor → dispenser follow-up), the in-process event bus, timeout/retry policy in `config/settings.py`, and `tests/test_orchestrator.py`. Use `build_default_orchestrator()` in `core/orchestrator.py` to register `DoctorAgent`, `GovOfficialAgent`, `DispenserAgent`, and the stock-manager placeholder.

**Still accurate from that PR:** `scripts/demo_scenario.py` remains **blocked primarily on Eng 4** (`agents/stock_manager_agent.py`, `scripts/run_simulation.py`) for the full scripted day; Eng 5 is still needed for REST/WebSocket + `dashboard/app.py` live wiring. Eng 3’s manual test plan (instantiate agents, call `handle()`) remains valid.

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
| `api/routes.py` | *(not yet implemented)* FastAPI REST endpoints |
| `api/websocket.py` | *(not yet implemented)* Real-time stream (Eng 5; subscribe to event bus topics) |
| `dashboard/app.py` | *(not yet implemented)* Gradio UI layout |
| `scripts/orchestrator_dry_run.py` | Eng 2 — scripted Rx→dispense smoke run via orchestrator |

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
| `scripts/demo_scenario.py` | Eng 3 | 3 | ✗ Blocked | Curated demo flow (needs Eng 4 simulation + Milestone 2 wiring; orchestrator is ready) |
| `agents/stock_manager_agent.py` | Eng 4 | 1 | ✗ Missing | Receive stock, expiry alerts, reorder triggers |
| `scripts/run_simulation.py` | Eng 4 | 2 | ✗ Missing | 15 Rx, 3 receipts, 2 expiry alerts, 1 anomaly |
| `api/routes.py` | Eng 5 | 1 | ✗ Missing | FastAPI REST endpoints |
| `dashboard/app.py` | Eng 5 | 2 | ✗ Missing | Gradio main layout |
| `api/websocket.py` | Eng 5 | 2 | ✗ Missing | Real-time WebSocket event stream |

**Milestones**:
- Milestone 1 (Phase 1 end): All components boot independently
- Milestone 2 (Phase 2 end): End-to-end flow works
- Milestone 3 (Phase 3 end): Demo-ready system
