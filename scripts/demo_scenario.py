#!/usr/bin/env python3
"""
Curated demo walkthrough for eStock Simulator.

Tells a human story across 5 scenes, suitable for live stakeholder presentations.
All four agents participate; the dashboard updates via SQLite polling (30s lag).

Scenes
──────
  1. Morning stock delivery      — StockManagerAgent receives 2 batches
  2. Doctor consultations (5 Rx) — DoctorAgent + DispenserAgent auto-chain
  3. Controlled drug anomaly     — Tramadol drained → orphan prescription
  4. Expiry & reorder audit      — StockManagerAgent sweeps the facility
  5. Government audit            — GovOfficialAgent flags the anomaly

Usage
─────
  python scripts/demo_scenario.py --seed     # seed DB on first run
  python scripts/demo_scenario.py            # subsequent runs
  python scripts/demo_scenario.py --pause    # pause between scenes (live demo)
  python scripts/demo_scenario.py --quiet    # outcomes only, no narrative

Dashboard
─────────
  Run alongside:
    python main.py --serve   (Terminal 1)
    python main.py --dash    (Terminal 2)
  All tabs update from shared SQLite; allow up to 30s for polling.
  WebSocket events are in-process only and will not stream from this script.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

FACILITY = "FACILITY-001"

# ── Scenario data ───────────────────────────────────────────────────────────────

STOCK_RECEIPTS = [
    {
        "drug_id": "drug-001",
        "batch_number": "SIM-DEMO-AMX-A",
        "quantity": 200,
        "expiry_date": "2028-12-31",
        "facility_id": FACILITY,
        "supplier": "PharmaCo Ltd",
        "unit_cost": 0.15,
        "location_in_store": "SHELF-A1",
        "_label": "Amoxicillin 500mg — 200 units from PharmaCo Ltd",
    },
    {
        "drug_id": "drug-006",
        "batch_number": "SIM-DEMO-PAR-A",
        "quantity": 400,
        "expiry_date": "2028-06-30",
        "facility_id": FACILITY,
        "supplier": "MediSupply Inc",
        "unit_cost": 0.05,
        "location_in_store": "SHELF-B1",
        "_label": "Paracetamol 500mg — 400 units from MediSupply Inc",
    },
]

ROUTINE_PRESCRIPTIONS = [
    {
        "drug_id": "drug-001",
        "patient_id": "PATIENT-DEMO-001",
        "quantity_prescribed": 14,
        "diagnosis_code": "J06.9",
        "notes": "Upper respiratory tract infection",
        "_label": "Amoxicillin 500mg × 14  →  PATIENT-DEMO-001  (URTI)",
    },
    {
        "drug_id": "drug-006",
        "patient_id": "PATIENT-DEMO-002",
        "quantity_prescribed": 30,
        "diagnosis_code": "R50.9",
        "notes": "Fever — symptomatic relief",
        "_label": "Paracetamol 500mg × 30  →  PATIENT-DEMO-002  (Fever)",
    },
    {
        "drug_id": "drug-003",
        "patient_id": "PATIENT-DEMO-003",
        "quantity_prescribed": 10,
        "diagnosis_code": "N39.0",
        "notes": "Urinary tract infection",
        "_label": "Ciprofloxacin 500mg × 10  →  PATIENT-DEMO-003  (UTI)",
    },
    {
        "drug_id": "drug-011",
        "patient_id": "PATIENT-DEMO-004",
        "quantity_prescribed": 24,
        "diagnosis_code": "B54",
        "notes": "Uncomplicated malaria — 3-day course",
        "_label": "Artemether-Lumefantrine × 24  →  PATIENT-DEMO-004  (Malaria)",
    },
    {
        "drug_id": "drug-006",
        "patient_id": "PATIENT-DEMO-005",
        "quantity_prescribed": 20,
        "diagnosis_code": "R51",
        "notes": "Headache",
        "_label": "Paracetamol 500mg × 20  →  PATIENT-DEMO-005  (Headache)",
    },
]

ANOMALY_PRESCRIPTIONS = [
    {
        "drug_id": "drug-009",
        "patient_id": "PATIENT-DEMO-006",
        "quantity_prescribed": 200,
        "diagnosis_code": "R52.2",
        "notes": "Chronic pain — post-operative (CONTROLLED)",
        "_label": "Tramadol 50mg × 200  →  PATIENT-DEMO-006  [CONTROLLED — drains stock]",
    },
    {
        "drug_id": "drug-009",
        "patient_id": "PATIENT-DEMO-007",
        "quantity_prescribed": 50,
        "diagnosis_code": "R52.2",
        "notes": "Chronic pain — physiotherapy adjunct (CONTROLLED)",
        "_label": "Tramadol 50mg ×  50  →  PATIENT-DEMO-007  [CONTROLLED — expected OUT_OF_STOCK]",
    },
]


# ── Print helpers ────────────────────────────────────────────────────────────────

_SCENE_NUM = 0


def _scene(title: str, narrative: str = "", *, pause: bool = False, quiet: bool = False) -> None:
    global _SCENE_NUM
    _SCENE_NUM += 1
    print(f"\n{'═' * 70}")
    print(f"  Scene {_SCENE_NUM} — {title}")
    print(f"{'═' * 70}")
    if narrative and not quiet:
        print(f"\n  {narrative}\n")
    if pause:
        input("  [Press Enter to run this scene...] ")


def _step(label: str, response, *, quiet: bool = False) -> None:
    icon = "✓" if response.success else "✗"
    at = str(response.action_type)
    r = response.result or {}

    if "STOCK_RECEIVED" in at:
        detail = f"qty_added={r.get('quantity_added')}  new_total={r.get('new_total')}"
    elif "DISPENSING_COMPLETE" in at:
        detail = (
            f"dispensed={r.get('quantity_dispensed')}  "
            f"remaining={r.get('stock_remaining')}  "
            f"status={r.get('status')}"
        )
    elif "DISPENSING_FAILED" in at:
        detail = response.error or "dispensing failed"
    elif "PRESCRIPTION_CREATED" in at:
        detail = f"prescription_id={r.get('prescription_id', '?')}"
    elif "EXPIRY_CHECKED" in at:
        detail = (
            f"scanned={r.get('scanned_batches')}  "
            f"expired={r.get('expired_count')}  "
            f"near_expiry={r.get('near_expiry_count')}"
        )
    elif "REORDER_ALERT" in at:
        detail = f"drugs_checked={r.get('drugs_checked')}  alerts={r.get('alerts_raised')}"
    elif "ANOMALY_FLAGGED" in at:
        detail = f"anomalies_flagged={r.get('anomalies_flagged', 0)}"
    elif "AUDIT_REPORT" in at:
        stats = r.get("stats", {})
        detail = (
            f"prescriptions={stats.get('prescriptions_total', 0)}  "
            f"anomalies={r.get('anomaly_count', 0)}"
        )
    else:
        detail = response.error or ""

    label_col = label[:52].ljust(52)
    print(f"  {icon}  {label_col}  {detail}")
    if not response.success and response.error and not quiet:
        print(f"       Note: {response.error}")


def _highlight(text: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(f"\n  *** {text} ***\n")


def _pause_for_dashboard(*, pause: bool, quiet: bool) -> None:
    if pause:
        if not quiet:
            print("\n  [Switch to the dashboard now — data has been written to the database]")
        input("  [Press Enter to continue to the next scene...] ")


# ── Main runner ──────────────────────────────────────────────────────────────────

async def _run(*, seed: bool, pause: bool, quiet: bool) -> int:
    from config.settings import settings
    from core.database import get_db, init_db, verify_connection
    from core.models import ActionType, AgentRole
    from core.orchestrator import build_default_orchestrator
    from core.schemas import AgentMessage

    if not verify_connection():
        print("ERROR: Database unreachable. Run with --seed or check DATABASE_URL.")
        return 1

    init_db()

    if seed:
        from scripts.seed_db import _load_json, seed_drugs, seed_stock
        with get_db() as session:
            drug_map = seed_drugs(session, _load_json(settings.drugs_catalog_path))
            seed_stock(session, _load_json(settings.seed_stock_path), drug_map)
        print("Database seeded.\n")

    orch = build_default_orchestrator(use_simulation_doctor=True)
    fac = settings.simulation_facility_id
    system_errors = 0

    print(f"\n{'═' * 70}")
    print(f"  eStock Simulator — Live Demo  ({fac})")
    print(f"{'═' * 70}")
    if not quiet:
        print(
            "\n  This walkthrough drives all four agents through a curated day:\n"
            "  Stock Manager → Doctor → Dispenser → Government Auditor\n"
            "\n  Dashboard tabs (stock, alerts, activity, audit) update from the\n"
            "  shared SQLite database and refresh automatically every 30 seconds.\n"
        )

    # ── Scene 1: Morning stock delivery ────────────────────────────────────────
    _scene(
        "Morning Stock Delivery",
        "The pharmacy store manager receives two supplier deliveries and books them "
        "into the inventory system.",
        pause=pause,
        quiet=quiet,
    )
    for receipt in STOCK_RECEIPTS:
        label = receipt.pop("_label")
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="demo",
            recipient_role=AgentRole.STOCK_MANAGER,
            action_type=ActionType.STOCK_RECEIVED,
            facility_id=fac,
            payload=dict(receipt),
            requires_lock=True,
            priority=8,
        )
        resp = await orch.dispatch_immediate(msg)
        if "SYSTEM_ERROR" in str(resp.action_type):
            system_errors += 1
        _step(label, resp, quiet=quiet)

    _pause_for_dashboard(pause=pause, quiet=quiet)

    # ── Scene 2: Doctor consultations (5 routine Rx) ────────────────────────────
    _scene(
        "Doctor Consultations — Routine Prescriptions",
        "The on-duty doctor sees five patients. Each prescription is automatically "
        "forwarded to the dispenser via the orchestrator.",
        pause=pause,
        quiet=quiet,
    )
    for rx in ROUTINE_PRESCRIPTIONS:
        label = rx.pop("_label")
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="demo",
            recipient_role=AgentRole.DOCTOR,
            action_type=ActionType.PRESCRIPTION_CREATED,
            facility_id=fac,
            payload={
                "drug_id": rx["drug_id"],
                "patient_id": rx["patient_id"],
                "quantity_prescribed": rx["quantity_prescribed"],
                "diagnosis_code": rx.get("diagnosis_code"),
                "notes": rx.get("notes"),
                "facility_id": fac,
                "doctor_agent_id": "demo-doctor",
            },
            priority=7,
        )
        resp = await orch.dispatch_immediate(msg)
        if "SYSTEM_ERROR" in str(resp.action_type):
            system_errors += 1
        _step(f"Rx  {label}", resp, quiet=quiet)

    if not quiet:
        print("\n  Dispensing follow-ups...")
    dispense_responses = await orch.run_until_idle(max_messages=10)
    for resp in dispense_responses:
        if "SYSTEM_ERROR" in str(resp.action_type):
            system_errors += 1
        r = resp.result or {}
        drug_id = r.get("drug_id", "?")
        dispensed = r.get("quantity_dispensed", "?")
        _step(f"    Dispense  {drug_id}  ×{dispensed}", resp, quiet=quiet)

    _pause_for_dashboard(pause=pause, quiet=quiet)

    # ── Scene 3: Controlled drug anomaly ───────────────────────────────────────
    _scene(
        "Controlled Drug — Anomaly Setup",
        "Two patients are prescribed Tramadol (a controlled drug). The first "
        "prescription exhausts the remaining stock. The second cannot be filled — "
        "creating an orphan controlled-drug prescription that the auditor will flag.",
        pause=pause,
        quiet=quiet,
    )
    for rx in ANOMALY_PRESCRIPTIONS:
        label = rx.pop("_label")
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="demo",
            recipient_role=AgentRole.DOCTOR,
            action_type=ActionType.PRESCRIPTION_CREATED,
            facility_id=fac,
            payload={
                "drug_id": rx["drug_id"],
                "patient_id": rx["patient_id"],
                "quantity_prescribed": rx["quantity_prescribed"],
                "diagnosis_code": rx.get("diagnosis_code"),
                "notes": rx.get("notes"),
                "facility_id": fac,
                "doctor_agent_id": "demo-doctor",
            },
            priority=7,
        )
        resp = await orch.dispatch_immediate(msg)
        if "SYSTEM_ERROR" in str(resp.action_type):
            system_errors += 1
        _step(f"Rx  {label}", resp, quiet=quiet)

    if not quiet:
        print("\n  Dispensing follow-ups...")
    dispense_responses = await orch.run_until_idle(max_messages=5)
    for resp in dispense_responses:
        if "SYSTEM_ERROR" in str(resp.action_type):
            system_errors += 1
        r = resp.result or {}
        drug_id = r.get("drug_id", "?")
        dispensed = r.get("quantity_dispensed", "?")
        label = f"    Dispense  {drug_id}  ×{dispensed}"
        _step(label, resp, quiet=quiet)
        if not resp.success or (r.get("status") in ("OUT_OF_STOCK", "PARTIALLY_DISPENSED")):
            _highlight(
                f"Controlled drug Rx for {drug_id} — status: {r.get('status', 'FAILED')}",
                quiet=quiet,
            )

    _pause_for_dashboard(pause=pause, quiet=quiet)

    # ── Scene 4: Expiry & reorder audit ────────────────────────────────────────
    _scene(
        "Expiry Scan & Reorder Check",
        "The stock manager scans all batches for near-expiry stock and checks "
        "whether any drugs have fallen below their reorder threshold.",
        pause=pause,
        quiet=quiet,
    )

    # Expiry scan
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="demo",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.EXPIRY_CHECKED,
        facility_id=fac,
        payload={"scan_days_ahead": 365, "quarantine_expired": True},
        requires_lock=True,
        priority=6,
    )
    resp = await orch.dispatch_immediate(msg)
    if "SYSTEM_ERROR" in str(resp.action_type):
        system_errors += 1
    _step("Expiry scan — 365-day window, quarantine expired", resp, quiet=quiet)

    # Reorder check
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="demo",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.REORDER_ALERT_RAISED,
        facility_id=fac,
        payload={},
        priority=5,
    )
    resp = await orch.dispatch_immediate(msg)
    if "SYSTEM_ERROR" in str(resp.action_type):
        system_errors += 1
    _step("Reorder check — full facility sweep", resp, quiet=quiet)
    alerts = (resp.result or {}).get("alerts_raised", 0)
    if alerts > 0:
        _highlight(f"{alerts} drug(s) below reorder threshold — check Alerts tab", quiet=quiet)

    _pause_for_dashboard(pause=pause, quiet=quiet)

    # ── Scene 5: Government audit ───────────────────────────────────────────────
    _scene(
        "Government Audit",
        "The government auditor scans the last 24 hours of activity for anomalies, "
        "then generates a full compliance report.",
        pause=pause,
        quiet=quiet,
    )

    # Anomaly detection
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="demo",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.ANOMALY_FLAGGED,
        facility_id=fac,
        payload={"scan_hours": 24},
        priority=4,
    )
    resp = await orch.dispatch_immediate(msg)
    if "SYSTEM_ERROR" in str(resp.action_type):
        system_errors += 1
    _step("Anomaly scan — 24-hour window", resp, quiet=quiet)
    anomalies = (resp.result or {}).get("anomalies_flagged", 0)
    if anomalies > 0:
        _highlight(
            f"{anomalies} anomaly/anomalies flagged — controlled drug Rx with no dispense record",
            quiet=quiet,
        )

    # Audit report
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="demo",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.AUDIT_REPORT_GENERATED,
        facility_id=fac,
        payload={"include_anomalies": True},
        priority=3,
    )
    resp = await orch.dispatch_immediate(msg)
    if "SYSTEM_ERROR" in str(resp.action_type):
        system_errors += 1
    _step("Compliance audit report", resp, quiet=quiet)

    _pause_for_dashboard(pause=pause, quiet=quiet)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 70}")
    print("  Demo complete")
    print(f"{'═' * 70}")
    if anomalies == 0:
        print(
            "\n  WARNING: Expected at least 1 anomaly (orphan Tramadol Rx) but none detected."
            "\n           Ensure Tramadol (drug-009) had ~180 seeded units before running.\n"
        )
    else:
        print(f"\n  Anomaly check passed — {anomalies} anomaly/anomalies flagged as expected.")

    if system_errors > 0:
        print(f"  {system_errors} unexpected system error(s) occurred — review output above.\n")
    else:
        print("  No system errors.\n")

    return 0 if system_errors == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(
        description="eStock Simulator — curated live demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--seed", action="store_true", help="Seed the database before running.")
    p.add_argument(
        "--pause",
        action="store_true",
        help="Pause between scenes for live dashboard narration.",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress narrative, show outcomes only.")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(seed=args.seed, pause=args.pause, quiet=args.quiet)))


if __name__ == "__main__":
    main()
