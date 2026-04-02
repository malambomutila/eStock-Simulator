# eStock — Electronic Stock Management System

An AI-powered electronic stock management system for healthcare facilities, where multiple autonomous agents simulate real-world supply chain roles — a doctor prescribing, a pharmacist dispensing, a warehouse manager restocking, and a government official auditing — all coordinated through a central orchestrator against a shared drug inventory.

## Why this exists

In many healthcare systems across Africa and the developing world, drug stockouts kill. Medicines expire on shelves while clinics nearby run dry. Paper-based tracking can't keep up, and the disconnect between prescribers, dispensers, and oversight bodies means problems surface too late.

eStock demonstrates how AI agents can automate and simulate the full pharmaceutical supply chain — from prescription to dispensing to restocking to audit — providing real-time visibility and anomaly detection.

## Architecture

```
┌─────────────────────────────────────────┐
│          Central Stock Database          │
│     (drugs, stock levels, Rx, logs)      │
└──────────────────┬──────────────────────┘
                   │
          ┌────────▼────────┐
          │   Orchestrator   │
          │  (routes tasks,  │
          │ resolves conflicts│
          └───┬───┬───┬───┬─┘
              │   │   │   │
     ┌────────┘   │   │   └────────┐
     ▼            ▼   ▼            ▼
 ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
 │ Doctor │ │Dispenser│ │ Stock  │ │  Gov   │
 │ Agent  │ │ Agent   │ │Manager │ │Official│
 └────────┘ └────────┘ └────────┘ └────────┘
              │                       │
              ▼                       ▼
        ┌───────────┐         ┌────────────┐
        │ Event Log │         │ Audit Trail│
        └─────┬─────┘         └──────┬─────┘
              └──────────┬───────────┘
                         ▼
               ┌──────────────────┐
               │    Dashboard     │
               │ (stock, alerts,  │
               │  activity, audit)│
               └──────────────────┘
```

## Agent personas

| Agent | Role | Key actions |
|---|---|---|
| **Doctor** | Clinical prescriber | Creates prescriptions, checks drug availability, requests restocks |
| **Dispenser** | Pharmacist | Fills prescriptions, deducts stock, logs dispensing events |
| **Stock Manager** | Warehouse operator | Receives new stock, monitors expiry dates, triggers reorder alerts |
| **Gov. Official** | Ministry of Health auditor | Runs audit queries, flags anomalies, generates compliance reports |

All agents share a single LLM backend with distinct system prompts that shape their persona and decision-making behavior.

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| LLM | OpenAI API (`gpt-4o-mini` by default) |
| Database | SQLite (via SQLAlchemy) |
| API | FastAPI + WebSockets |
| Dashboard | Gradio |
| Testing | pytest |

## Project structure

```
eStock-Simulator/
├── estock.py                        # Unified launcher (seed + API + dashboard + simulation)
├── main.py                          # Individual service entry point
├── pyproject.toml
├── .env.example                     # LLM API key template
│
├── core/                            # System backbone
│   ├── models.py                    # SQLAlchemy ORM models + enums
│   ├── database.py                  # DB engine & session factory
│   ├── schemas.py                   # Pydantic v2 schemas & agent message envelopes
│   ├── orchestrator.py              # Central dispatcher: priority queue, stock locks, follow-ups
│   └── event_bus.py                 # In-process pub/sub (→ WebSocket bridge)
│
├── agents/                          # AI agent personas
│   ├── base_agent.py                # Abstract base: handle(), _ok(), _fail(), _make_audit_entry()
│   ├── doctor_agent.py              # Creates prescriptions, checks availability, requests restocks
│   ├── dispenser_agent.py           # FEFO dispensing, stock deduction, audit logging
│   ├── stock_manager_agent.py       # Receives stock, scans expiry, raises reorder alerts
│   └── gov_official_agent.py        # Audit queries, anomaly detection, compliance reports
│
├── api/                             # Backend endpoints
│   ├── routes.py                    # FastAPI REST: stock, prescriptions, audit, alerts, activity
│   └── websocket.py                 # /ws/events — event bus → WebSocket bridge
│
├── dashboard/                       # Frontend UI
│   ├── app.py                       # Gradio multi-tab layout
│   └── components/
│       ├── activity_feed.py         # Live agent action ticker
│       ├── alerts_panel.py          # Low stock & expiry alerts
│       └── audit_log.py             # Transaction history table
│
├── config/
│   ├── settings.py                  # Pydantic BaseSettings; loads .env
│   └── prompts.py                   # LLM system prompts (doctor + gov official)
│
├── data/
│   ├── drugs_catalog.json           # 55 seed medicines
│   └── seed_stock.json              # Initial batch inventory (58 entries)
│
├── scripts/
│   ├── seed_db.py                   # Populate database from JSON catalogs
│   ├── run_simulation.py            # Full scripted day: 15 Rx, 3 receipts, 2 expiry scans, 1 anomaly
│   └── orchestrator_dry_run.py      # Smoke test: single Rx → dispense chain
│
├── logs/                            # Created at runtime by estock.py
│   ├── api.log                      # FastAPI / uvicorn output
│   └── dashboard.log                # Gradio output
│
└── tests/
    ├── test_agents.py
    ├── test_orchestrator.py
    ├── test_api.py
    └── test_simulation.py
```

## Getting started

### Prerequisites

- Python 3.12 or higher
- An OpenAI API key
- [`uv`](https://github.com/astral-sh/uv) (recommended package manager)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/estock.git
cd eStock-Simulator

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

### Run everything with one command

```bash
uv run python estock.py
```

`estock.py` is the unified launcher. It runs all four steps in sequence — seed the database, start the API, start the dashboard, run the simulation — then keeps the services alive until you press `Ctrl+C`.

```
Step 1 / 4  —  Seed database          (55 drugs, 58 stock batches)
Step 2 / 4  —  Start FastAPI backend  → http://0.0.0.0:8000
Step 3 / 4  —  Start Gradio dashboard → http://0.0.0.0:7860
Step 4 / 4  —  Run simulation         (15 Rx · 3 receipts · 2 expiry scans · 1 anomaly)
```

To wipe and re-seed the database before starting:

```bash
uv run python estock.py --reset
```

API and dashboard logs are written to `logs/api.log` and `logs/dashboard.log`.

### Run services individually

If you prefer to run each component in its own terminal:

```bash
# Terminal 1 — seed the database (once)
uv run python main.py --seed

# Terminal 2 — FastAPI backend
uv run python main.py --serve

# Terminal 3 — Gradio dashboard (start after API is ready)
uv run python main.py --dash

# Terminal 4 — run the simulation
uv run python scripts/run_simulation.py
```

### Access from a remote machine

By default, both services bind to `0.0.0.0` and are reachable from any host that can reach the server:

| Service | URL |
|---|---|
| REST API | `http://<server-ip>:8000` |
| Swagger UI | `http://<server-ip>:8000/docs` |
| WebSocket | `ws://<server-ip>:8000/ws/events` |
| Dashboard | `http://<server-ip>:7860` |

If you are running behind a firewall or on a VM, ensure ports `8000` and `7860` are open. Add your host to `CORS_ORIGINS` in `.env` if the dashboard's REST calls are blocked by the browser (see [Environment variables](#environment-variables)).

## Running tests

```bash
uv run pytest tests/ -v
```

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key | — |
| `LLM_MODEL` | Model identifier | `gpt-4o-mini` |
| `LLM_TEMPERATURE` | Sampling temperature | `0.3` |
| `LLM_MAX_TOKENS` | Token cap per agent call | `1024` |
| `DATABASE_URL` | SQLAlchemy connection string | `sqlite:///data/estock.db` |
| `SIMULATION_FACILITY_ID` | Facility being simulated | `FACILITY-001` |
| `LOW_STOCK_THRESHOLD_PCT` | Fraction of reorder threshold that triggers a low-stock alert | `0.20` |
| `REORDER_ALERT_THRESHOLD_DAYS` | Days until expiry that triggers a near-expiry warning | `90` |
| `ANOMALY_SPIKE_MULTIPLIER` | Consumption multiple over 30-day daily average to flag a spike | `3.0` |
| `API_HOST` | FastAPI bind address (`0.0.0.0` for remote access) | `0.0.0.0` |
| `API_PORT` | FastAPI port | `8000` |
| `API_RELOAD` | Enable uvicorn auto-reload (set `false` in production) | `true` |
| `CORS_ORIGINS` | JSON list of allowed CORS origins for the REST API | see `.env.example` |

## Demo walkthrough

Run `uv run python estock.py` and open the dashboard at `http://<server>:7860`. The simulation plays out in six phases:

1. **Stock receipts** — The stock manager receives three deliveries: Amoxicillin (+300 units), Paracetamol (+500 units), and Artemether-Lumefantrine (+200 units). Each receipt is logged with batch number, supplier, expiry date, and updated totals.

2. **15 prescriptions** — The doctor agent creates prescriptions across six drugs (antibiotics, analgesics, antimalarials, a controlled opioid). The orchestrator auto-chains each to the dispenser. 14 are fulfilled; the 15th — a second Tramadol prescription after stock is exhausted — returns `OUT_OF_STOCK`.

3. **Expiry scans** — The stock manager scans all 61 batches twice: once facility-wide (365-day window) and once narrowed to Amoxicillin (180-day window). Near-expiry batches are flagged; any truly expired batches are quarantined automatically.

4. **Reorder check** — A full sweep across all 55 active drugs surfaces those below their reorder threshold. Each triggers a `REORDER_ALERT_RAISED` audit entry visible in the dashboard Alerts tab.

5. **Anomaly detected** — The government official scans the last 24 hours and finds the Tramadol prescription that was created but never dispensed — a controlled-drug orphan. It is flagged as an anomaly with `is_anomaly=True` in the audit log.

6. **Compliance report** — The government official generates a full audit report covering all prescriptions, dispensing events, anomaly counts, and a narrative LLM summary (when an API key is configured).

## Key design decisions

**FEFO dispensing** — the dispenser always consumes from the earliest-expiring batch first to minimise waste.

**Immutable audit log** — every agent action appends a row to `AuditLog`. No rows are ever updated or deleted. This table is the source of truth for compliance reporting and anomaly detection.

**Per-drug stock locks** — the orchestrator acquires an `asyncio.Lock` keyed by `(facility_id, drug_id)` before dispatching any message that touches stock, preventing concurrent dispense requests from double-spending the same batch.

**Facility isolation** — all data carries a `facility_id`. The default is `FACILITY-001` (configurable in `.env`). The architecture is multi-facility by design.

**Graceful LLM fallback** — doctor clinical notes and government compliance narratives are enriched by the LLM when `OPENAI_API_KEY` is set, but every agent falls back gracefully if the key is absent or the call fails. The system is fully functional without an API key.

## Contributing

This project was built as a team simulation exercise. If you'd like to extend it:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -m 'Add your feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

## License

<!-- TODO: Choose a license -->
TBD

## Team

<!-- TODO: Add team members -->
Built by a team of 5 engineers in a single day.