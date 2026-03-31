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
| Language | Python 3.11+ |
| LLM | Claude API (via Anthropic SDK) |
| Database | SQLite (via SQLAlchemy) |
| API | FastAPI + WebSockets |
| Dashboard | Streamlit |
| Testing | pytest |

## Project structure

```
estock/
├── main.py                          # App entry point
├── requirements.txt
├── pyproject.toml
├── .env.example                     # LLM API key template
│
├── core/                            # System backbone
│   ├── models.py                    # SQLAlchemy models
│   ├── database.py                  # DB engine & session factory
│   ├── schemas.py                   # Pydantic schemas
│   ├── orchestrator.py              # Central agent dispatcher
│   └── event_bus.py                 # Pub/sub event system
│
├── agents/                          # AI agent personas
│   ├── base_agent.py                # Abstract base class
│   ├── doctor_agent.py
│   ├── dispenser_agent.py
│   ├── stock_manager_agent.py
│   └── gov_official_agent.py
│
├── api/                             # Backend endpoints
│   ├── routes.py                    # REST API
│   └── websocket.py                 # Real-time event stream
│
├── dashboard/                       # Frontend UI
│   ├── app.py                       # Streamlit layout
│   └── components/
│       ├── stock_levels.py          # Inventory bar charts
│       ├── activity_feed.py         # Live agent action ticker
│       ├── alerts_panel.py          # Low stock & expiry alerts
│       └── audit_log.py             # Transaction history table
│
├── config/
│   ├── settings.py                  # Environment & model config
│   └── prompts.py                   # Agent system prompts
│
├── data/
│   ├── drugs_catalog.json           # 50+ seed medicines
│   └── seed_stock.json              # Initial inventory levels
│
├── scripts/
│   ├── seed_db.py                   # Populate database
│   ├── run_simulation.py            # Simulate a full day
│   ├── demo_scenario.py             # Curated demo sequence
│   └── generate_report.py           # Export audit reports
│
└── tests/
    ├── test_agents.py
    ├── test_orchestrator.py
    ├── test_database.py
    └── test_simulation.py
```

## Getting started

### Prerequisites

- Python 3.11 or higher
- An Anthropic API key (or whichever LLM provider you configure)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/estock.git
cd estock

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and add your API key
```

### Seed the database

```bash
python scripts/seed_db.py
```

This populates the SQLite database with 50+ medicines, initial stock levels, and expiry dates.

### Run the application

```bash
# Start the backend
python main.py

# In a separate terminal, start the dashboard
streamlit run dashboard/app.py
```

### Run the demo scenario

```bash
python scripts/demo_scenario.py
```

This runs a curated sequence: doctor prescribes → dispenser fulfills → stock drops below threshold → alert fires → government auditor flags the anomaly.

### Run the simulation

```bash
python scripts/run_simulation.py
```

Simulates a full day of activity: 15 prescriptions, 3 stock receipts, 2 expiry alerts, and 1 anomaly for the auditor to catch.

## Running tests

```bash
pytest tests/ -v
```

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your LLM API key | — |
| `LLM_MODEL` | Model identifier | `claude-sonnet-4-20250514` |
| `DATABASE_URL` | SQLite connection string | `sqlite:///estock.db` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `DASHBOARD_PORT` | Streamlit port | `8501` |
| `API_PORT` | FastAPI port | `8000` |

## Demo walkthrough

The demo follows a single narrative arc that showcases all four agents working together:

1. **Doctor prescribes** — The doctor agent examines a patient case and writes a prescription for Amoxicillin (500mg, 21 capsules) and Paracetamol (1g, 10 tablets).

2. **Dispenser fulfills** — The dispenser agent receives the prescription, validates it against available stock, deducts the quantities, and logs the dispensing event.

3. **Stock drops** — Amoxicillin falls below the reorder threshold. The stock manager agent detects this and fires a low-stock alert.

4. **Stock manager restocks** — A new shipment arrives. The stock manager receives it, updates inventory levels, and logs the batch number and expiry date.

5. **Anomaly detected** — The government official agent runs a routine audit, notices an unusually high volume of a controlled substance dispensed in the last 24 hours, and flags it for investigation.

6. **Compliance report** — The government official generates a compliance report summarizing stock movements, flagged anomalies, and expired medicine counts.

## Contributing

This project was built as a hackathon demo. If you'd like to extend it:

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