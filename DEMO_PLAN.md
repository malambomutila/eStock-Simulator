# Plan: scripts/demo_scenario.py

## Context

`demo_scenario.py` is the last missing file for Milestone 3 (demo-ready system). All its
dependencies — orchestrator, all four agents, and the full simulation — are now in place.

Unlike `run_simulation.py` (automated, 15 Rx, machine-readable report), `demo_scenario.py`
is a curated, narrative-driven walkthrough intended for live demos and stakeholder presentations.
It tells a human story with rich printed commentary, hand-picked drug/patient pairings, and
deliberate pacing — fewer events, more explanation.

---

## What the Script Must Do

### Structure: 5 scenes (not 6 phases)

**Scene 1 — Morning stock delivery**
- StockManagerAgent receives 2 deliveries (Amoxicillin + Paracetamol)
- Narrative: "The pharmacy receives morning deliveries..."
- Uses `STOCK_RECEIVED` → `dispatch_immediate()` with `requires_lock=True`

**Scene 2 — Doctor consultations (5 Rx)**
- DoctorAgent creates 5 prescriptions (mix of drugs, patients)
- Uses `PRESCRIPTION_CREATED` → `dispatch_immediate()` per Rx
- After all 5, `run_until_idle()` drains the auto-dispense follow-ups
- Narrative per Rx: "Dr. [X] prescribes [drug] for [patient]..."
- Narrative per dispense: "Dispenser fulfils/partially fulfils/cannot fill..."

**Scene 3 — Controlled drug anomaly (2 Rx for Tramadol)**
- First Rx drains the Tramadol stock (PARTIALLY_DISPENSED)
- Second Rx gets OUT_OF_STOCK — orphan controlled-drug prescription
- This is the anomaly setup; narrate it clearly

**Scene 4 — Expiry & reorder audit**
- StockManagerAgent runs `EXPIRY_CHECKED` scan (365-day window)
- StockManagerAgent runs `REORDER_ALERT_RAISED` sweep
- Narrative: "Stock manager scans for expiring batches and low stock..."

**Scene 5 — Government audit**
- GovOfficialAgent runs `ANOMALY_FLAGGED` (detects the orphan Tramadol Rx)
- GovOfficialAgent runs `AUDIT_REPORT_GENERATED`
- Narrative: "Auditor flags 1 anomaly — controlled drug prescribed but never dispensed"

---

## Key Differences from run_simulation.py

| | run_simulation.py | demo_scenario.py |
|---|---|---|
| Rx count | 15 | 7 (5 routine + 2 Tramadol) |
| Output | Structured report table | Narrative prose + outcome per step |
| Pacing | Fires all at once | Scene-by-scene with separator banners |
| Purpose | CI / end-to-end verification | Live demo / stakeholder presentation |
| Verbose flag | `--verbose` for payloads | `--quiet` to suppress commentary |

---

## Implementation Details

### Orchestrator setup
```python
orch = build_default_orchestrator(use_simulation_doctor=True)
```

### AgentMessage pattern (same as run_simulation.py)
```python
AgentMessage(
    sender_role=AgentRole.SYSTEM,
    sender_id="demo",
    recipient_role=AgentRole.DOCTOR,
    action_type=ActionType.PRESCRIPTION_CREATED,
    facility_id=FACILITY,
    payload={...},
    priority=7,
)
```

### Narrative print helpers
- `_scene(title)` — prints a banner with scene number/title
- `_step(label, response)` — prints ✓/✗ + one-line human outcome
- `_highlight(text)` — prints a highlighted callout for anomalies

### CLI args
- `--seed` — seed DB before running (same as run_simulation.py)
- `--quiet` — suppress scene commentary, show only outcomes
- `--pause` — wait for Enter key between scenes (for live presentations so the dashboard can be shown)

---

## Scenario Data (hand-picked)

**Stock receipts:**
- drug-001 (Amoxicillin 500mg): batch SIM-DEMO-AMX-A, qty 200
- drug-006 (Paracetamol 500mg): batch SIM-DEMO-PAR-A, qty 400

**Prescriptions (routine):**
- drug-001, PATIENT-DEMO-001, qty 14 — upper respiratory infection
- drug-006, PATIENT-DEMO-002, qty 30 — fever
- drug-003 (Ciprofloxacin), PATIENT-DEMO-003, qty 10 — UTI
- drug-011 (Artemether-Lumefantrine), PATIENT-DEMO-004, qty 24 — malaria
- drug-006, PATIENT-DEMO-005, qty 20 — headache

**Anomaly prescriptions:**
- drug-009 (Tramadol), PATIENT-DEMO-006, qty 200 — chronic pain (drains ~180 units → PARTIALLY_DISPENSED)
- drug-009 (Tramadol), PATIENT-DEMO-007, qty 50 — chronic pain (OUT_OF_STOCK → orphan Rx)

---

## Dashboard Integration

`demo_scenario.py` runs in its own process with its own in-process `EventBus`. This has two consequences:

**What WILL update in the dashboard (all tabs read SQLite):**
- Audit Log tab — direct SQLite query, updates within ~1s of each scene completing
- Stock tab — REST poll every 30s, reflects new batches and deducted stock
- Alerts tab — REST poll every 30s, reflects reorder alerts raised by StockManager
- Activity tab — REST poll every 30s, reflects all agent actions

**What will NOT update:**
- WebSocket `/ws/events` stream — in-process bus, no cross-process events

**Recommended live demo sequence:**
1. Start `python main.py --serve` (Terminal 1)
2. Start `python main.py --dash` (Terminal 2) — show the empty dashboard to the audience
3. Run `python scripts/demo_scenario.py --pause` (Terminal 3)
4. After each scene completes, switch to the browser and show the dashboard updating
5. The `--pause` flag waits for Enter between scenes, giving time to narrate the dashboard

The 30-second poll lag means the presenter should linger on the dashboard for a moment after each scene, or note to the audience that the data is live and refreshes automatically.

---

## Critical Files

| File | Role |
|------|------|
| `scripts/run_simulation.py` | Primary pattern to follow (structure, AgentMessage construction, result tracking) |
| `scripts/orchestrator_dry_run.py` | Secondary reference (minimal orchestrator usage) |
| `core/orchestrator.py` | `build_default_orchestrator()`, `dispatch_immediate()`, `run_until_idle()` |
| `core/schemas.py` | `AgentMessage`, `AgentResponse`, `ActionType`, `AgentRole` |
| `core/models.py` | `ActionType` and `AgentRole` enum values |

### Reuse from run_simulation.py
- `_summarise(response)` — can be imported or copied; maps action_type to a one-liner
- `SimulationReport` / `StepResult` dataclasses — reuse or simplify
- Seed helper pattern: `seed_drugs()` / `seed_stock()` calls

---

## Verification

```bash
# First run (seeds DB)
python scripts/demo_scenario.py --seed

# Subsequent runs (DB already seeded)
python scripts/demo_scenario.py

# Live presentation mode (pauses between scenes for dashboard viewing)
python scripts/demo_scenario.py --pause

# Quiet mode (output only)
python scripts/demo_scenario.py --quiet
```

**Expected outcome:**
- 2 stock receipt steps: both ✓
- 5 routine Rx + 5 dispense steps: all ✓ DISPENSED
- 2 Tramadol Rx: doctor steps ✓, dispenses show PARTIALLY_DISPENSED + OUT_OF_STOCK
- Expiry scan + reorder sweep: both ✓
- Anomaly detection: ✓ with `anomalies_flagged >= 1`
- Audit report: ✓

Script exits with code 0 if no SYSTEM_ERRORs, 1 otherwise.
