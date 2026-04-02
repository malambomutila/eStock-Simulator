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
               │ (simulate, stock,│
               │  alerts, audit)  │
               └──────────────────┘
```

## Agent personas

| Agent | Role | Key actions |
|---|---|---|
| **Doctor** | Clinical prescriber | Creates prescriptions by `drug_id`; drug must exist in DB. Quantity is the caller's value. LLM enriches clinical notes. |
| **Dispenser** | Pharmacist | Reads the prescription from DB, deducts stock using FEFO (earliest-expiry-first), writes the real post-deduction remaining total back. |
| **Stock Manager** | Warehouse operator | Receives new stock batches (upsert on batch number), queries the DB total after each insert, raises reorder alerts. |
| **Gov. Official** | Ministry of Health auditor | Scans audit log for dispensing spikes (vs. 30-day baseline) and controlled-drug orphan prescriptions; generates LLM compliance reports. |

All agents communicate via typed `AgentMessage` / `AgentResponse` envelopes. Agent narration is **grounded** — every number and drug name in the dialogue is read directly from the database response before being passed to the LLM, which may only vary connecting words (see [Grounded narration](#grounded-narration)).

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| LLM | OpenAI API (`gpt-4o-mini` by default) |
| Database | SQLite (via SQLAlchemy) |
| API | FastAPI + WebSockets |
| Dashboard | Gradio + Plotly |
| Testing | pytest |

## Project structure

```
eStock-Simulator/
├── estock.py                        # Unified launcher (3 steps: seed → API → dashboard)
├── main.py                          # Individual service entry point
├── pyproject.toml
├── .env.example                     # LLM API key template
│
├── core/                            # System backbone
│   ├── models.py                    # SQLAlchemy ORM models + enums
│   ├── database.py                  # DB engine & session factory
│   ├── schemas.py                   # Pydantic v2 schemas & agent message envelopes
│   ├── orchestrator.py              # Central dispatcher: priority queue, stock locks, follow-ups
│   ├── event_bus.py                 # In-process pub/sub (→ WebSocket bridge)
│   └── simulation_engine.py         # Scenario runner: 5 built-in scenarios, grounded LLM narration
│
├── agents/                          # AI agent personas
│   ├── base_agent.py                # Abstract base: handle(), _ok(), _fail(), _make_audit_entry()
│   ├── doctor_agent.py              # Creates prescriptions, checks availability, requests restocks
│   ├── dispenser_agent.py           # FEFO dispensing, stock deduction, audit logging
│   ├── stock_manager_agent.py       # Receives stock, scans expiry, raises reorder alerts
│   └── gov_official_agent.py        # Audit queries, anomaly detection, compliance reports
│
├── api/                             # Backend endpoints
│   ├── routes.py                    # REST: stock, Rx, audit, alerts, simulate, shutdown
│   └── websocket.py                 # /ws/events — event bus → WebSocket bridge
│
├── dashboard/                       # Frontend UI
│   ├── app.py                       # Gradio multi-tab layout (6 tabs, auto-refresh)
│   └── components/
│       ├── simulate_tab.py          # 🎬 Animated agent theater (scenarios, polling, Close btn)
│       ├── charts.py                # Plotly stock bar chart, expiry heatmap, KPI cards
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
│   ├── run_simulation.py            # Full scripted day (reference data used by simulation engine)
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

`estock.py` is the unified launcher. It runs three steps, then keeps the services alive until you press `Ctrl+C` (or click **⏻ Close Application** in the dashboard):

```
Step 1 / 3  —  Seed database          (reset to clean state — 0 prescriptions, 0 audit logs)
Step 2 / 3  —  Start FastAPI backend  → http://0.0.0.0:8000
Step 3 / 3  —  Start Gradio dashboard → http://0.0.0.0:7860
```

To keep existing database data instead of resetting:

```bash
uv run python estock.py --no-reset
```

#### Port management

Before starting each service, `estock.py` automatically kills any orphaned process holding port `8000` or `7860` from a previous session. This prevents the common situation where Ctrl+C leaves background processes running and the next launch fails to bind.

Both ports are fixed — the API will not fall back to `8001`, the dashboard will not fall back to `7861`. If a port is truly in use (e.g. by another application), you will get a clear bind error.

### Run services individually

```bash
# Terminal 1 — seed the database (once)
uv run python main.py --seed

# Terminal 2 — FastAPI backend
uv run python main.py --serve

# Terminal 3 — Gradio dashboard (start after API is ready)
uv run python main.py --dash
```

### Access to Services

| Service | URL |
|---|---|
| REST API | `http://<server-ip>:8000` |
| Swagger UI | `http://<server-ip>:8000/docs` |
| WebSocket | `ws://<server-ip>:8000/ws/events` |
| Dashboard | `http://<server-ip>:7860` |

Ensure ports `8000` and `7860` are open. Add your host to `CORS_ORIGINS` in `.env` if the dashboard's REST calls are blocked (see [Environment variables](#environment-variables)).

## Running tests

```bash
uv run pytest tests/ -v
```

## Dashboard

The Gradio dashboard has six tabs. All tabs auto-refresh — there are no manual refresh buttons.

| Tab | Contents |
|---|---|
| **🎬 Simulate** | Run scenarios; watch agents converse in real time (see below) |
| **📊 Overview** | KPI cards (total drugs, low-stock count, anomaly count) + stock bar chart + live activity feed |
| **📦 Stock** | Full stock level table with batch details |
| **⏳ Expiry** | Scatter heatmap of batch expiry dates; bubble size = quantity |
| **🔔 Alerts** | Low-stock and near-expiry alert cards |
| **⚡ Activity** | Scrollable audit log of every agent action |

### Simulate tab

Select a scenario from the dropdown and click **▶ Run Simulation**. The tab polls the backend every 1.5 seconds and streams each agent's speech into its conversation box as it is generated. All other tabs automatically reflect the database changes from the simulation.

Available scenarios:

| Scenario | What it exercises |
|---|---|
| **Anomaly** | Tramadol stock drained → orphan controlled-drug Rx → Gov. Official flags it |
| **Limited Stock** | 5 high-volume prescriptions → reorder alerts → compliance check |
| **Expiry Alert** | Near-expiry stock receipts → full batch scan → quarantine |
| **Random** | GPT-4o-mini generates 4 patient cases → full Doctor → Dispenser chain |
| **Full Day** | All 15 Rx + 3 receipts + expiry scans + reorder sweep + audit |

Click **⏻ Close Application** to send a shutdown signal to the launcher process, terminating the API and dashboard and freeing both ports cleanly.

## Simulation engine (`core/simulation_engine.py`)

Each scenario is an `async` function that orchestrates real agent calls through the same `Orchestrator` used in production. Scenarios do **not** mock anything — every prescription creates a real `Prescription` row, every dispense deducts real `StockLevel` rows, every stock receipt increments real batch quantities.

### Grounded narration

Agent speech is generated in two steps:

1. **Template built from DB response values** — after each agent call, the engine reads `AgentResponse.result` (which contains database-confirmed values: `drug_name`, `quantity_dispensed`, `stock_remaining`, `new_total`, etc.) and constructs a factual sentence using only those fields.

2. **LLM lightly paraphrases for style** — the template is passed to GPT-4o-mini with the instruction to keep every number, drug name, and quantity exactly as written. A **number-integrity guard** then compares all numeric tokens in the template against the LLM output; if any number has changed, the raw template is used instead.

This means the numbers in agent dialogue are always consistent with the database and with the values shown in the other dashboard tabs.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/simulate` | Start a simulation scenario in a background thread; returns `run_id` |
| `GET` | `/api/simulate/{run_id}/feed` | Poll live events and status for a running simulation |
| `GET` | `/api/simulate` | List all simulation runs in the current process session |
| `POST` | `/api/shutdown` | Send a graceful shutdown signal to the launcher process |

## Key design decisions

**FEFO dispensing** — the dispenser always consumes from the earliest-expiring batch first to minimise waste.

**Immutable audit log** — every agent action appends a row to `AuditLog`. No rows are ever updated or deleted. This table is the source of truth for compliance reporting and anomaly detection.

**Per-drug stock locks** — the orchestrator acquires an `asyncio.Lock` keyed by `(facility_id, drug_id)` before dispatching any message that touches stock, preventing concurrent dispense requests from double-spending the same batch.

**Facility isolation** — all data carries a `facility_id`. The default is `FACILITY-001` (configurable in `.env`). The architecture is multi-facility by design.

**Graceful LLM fallback** — clinical notes, compliance narratives, and agent dialogue are enriched by the LLM when `OPENAI_API_KEY` is set, but every agent falls back to template strings if the key is absent or the call fails. The system is fully functional without an API key; grounded narration simply returns the raw template.

**Clean process lifecycle** — `estock.py` exports its own PID as `ESTOCK_LAUNCHER_PID` into each child process's environment. The `/api/shutdown` endpoint reads this value and sends `SIGTERM` to the launcher, which then terminates API and dashboard child processes and frees both ports before exiting.

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
| `API_RELOAD` | Enable uvicorn auto-reload (overridden to `false` by `estock.py`) | `true` |
| `CORS_ORIGINS` | JSON list of allowed CORS origins for the REST API | see `.env.example` |

`DASHBOARD_PORT` defaults to `7860` and is controlled via `settings.dashboard_port` in code (not yet surfaced in `.env`).

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
