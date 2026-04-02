#!/usr/bin/env python3
"""
Scripted simulation of a full day at FACILITY-001.

Phases
──────
  1. Stock receipts      (3)  — StockManagerAgent receives new deliveries
  2. Prescriptions      (15)  — DoctorAgent creates Rx; orchestrator auto-chains DispenserAgent
  3. Expiry scans        (2)  — StockManagerAgent scans near-expiry / expired batches
  4. Reorder check       (1)  — StockManagerAgent flags drugs below threshold
  5. Anomaly detection   (1)  — GovOfficialAgent scans for dispensing anomalies
  6. Audit report        (1)  — GovOfficialAgent produces compliance summary

Anomaly design
──────────────
  Tramadol (drug-009) is a controlled drug with 180 seeded units.
  Rx-14 prescribes 200 units → PARTIALLY_DISPENSED (stock drained to 0, DISPENSING_COMPLETE written).
  Rx-15 prescribes 50 units  → OUT_OF_STOCK (DISPENSING_FAILED; no DISPENSING_COMPLETE).
  GovOfficialAgent then finds Rx-15: a controlled-drug prescription with no dispense audit → flags it.

Usage
─────
  uv run python scripts/run_simulation.py --seed      # seed DB first
  uv run python scripts/run_simulation.py             # if DB already seeded
  uv run python scripts/run_simulation.py --verbose   # show full agent result payloads
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")
logger = logging.getLogger("run_simulation")


# ── Scenario data ──────────────────────────────────────────────────────────────

FACILITY = "FACILITY-001"

STOCK_RECEIPTS = [
    {
        "drug_id": "drug-001",
        "batch_number": "SIM-AMX500-2026-A",
        "quantity": 300,
        "expiry_date": "2028-12-31",
        "facility_id": FACILITY,
        "supplier": "PharmaCo Ltd",
        "unit_cost": 0.15,
        "location_in_store": "SHELF-A1",
        "_label": "Amoxicillin 500mg (+300 units)",
    },
    {
        "drug_id": "drug-006",
        "batch_number": "SIM-PAR500-2026-A",
        "quantity": 500,
        "expiry_date": "2028-06-30",
        "facility_id": FACILITY,
        "supplier": "MediSupply Inc",
        "unit_cost": 0.05,
        "location_in_store": "SHELF-B1",
        "_label": "Paracetamol 500mg (+500 units)",
    },
    {
        "drug_id": "drug-011",
        "batch_number": "SIM-ART-2026-A",
        "quantity": 200,
        "expiry_date": "2027-09-30",
        "facility_id": FACILITY,
        "supplier": "GenMed Supplies",
        "unit_cost": 0.80,
        "location_in_store": "SHELF-C3",
        "_label": "Artemether-Lumefantrine 20/120mg (+200 units)",
    },
]

# 15 prescriptions — 13 routine, 2 controlled (designed to trigger anomaly on Rx-15)
PRESCRIPTIONS = [
    # ── Routine antibiotics (drug-001 Amoxicillin 500mg) ──
    {"drug_id": "drug-001", "patient_id": "PATIENT-SIM-001", "quantity_prescribed": 14,
     "diagnosis_code": "J06.9", "notes": "Upper respiratory tract infection"},
    {"drug_id": "drug-001", "patient_id": "PATIENT-SIM-002", "quantity_prescribed": 21,
     "diagnosis_code": "J02.9", "notes": "Acute pharyngitis"},
    {"drug_id": "drug-001", "patient_id": "PATIENT-SIM-003", "quantity_prescribed": 14,
     "diagnosis_code": "J20.9", "notes": "Acute bronchitis"},
    # ── Analgesic (drug-006 Paracetamol 500mg) ──
    {"drug_id": "drug-006", "patient_id": "PATIENT-SIM-004", "quantity_prescribed": 30,
     "diagnosis_code": "R50.9", "notes": "Fever — symptomatic relief"},
    {"drug_id": "drug-006", "patient_id": "PATIENT-SIM-005", "quantity_prescribed": 20,
     "diagnosis_code": "R51",   "notes": "Headache"},
    {"drug_id": "drug-006", "patient_id": "PATIENT-SIM-006", "quantity_prescribed": 30,
     "diagnosis_code": "M79.3", "notes": "Myalgia"},
    # ── Antimalarial (drug-011 Artemether-Lumefantrine) ──
    {"drug_id": "drug-011", "patient_id": "PATIENT-SIM-007", "quantity_prescribed": 24,
     "diagnosis_code": "B54",   "notes": "Uncomplicated malaria — 3-day course"},
    {"drug_id": "drug-011", "patient_id": "PATIENT-SIM-008", "quantity_prescribed": 24,
     "diagnosis_code": "B54",   "notes": "Uncomplicated malaria — 3-day course"},
    # ── Antibiotic (drug-003 Ciprofloxacin 500mg) ──
    {"drug_id": "drug-003", "patient_id": "PATIENT-SIM-009", "quantity_prescribed": 10,
     "diagnosis_code": "A09",   "notes": "Bacterial gastroenteritis"},
    {"drug_id": "drug-003", "patient_id": "PATIENT-SIM-010", "quantity_prescribed": 10,
     "diagnosis_code": "N39.0", "notes": "Urinary tract infection"},
    # ── Antibiotic (drug-004 Metronidazole 400mg) ──
    {"drug_id": "drug-004", "patient_id": "PATIENT-SIM-011", "quantity_prescribed": 21,
     "diagnosis_code": "A06.0", "notes": "Intestinal amoebiasis"},
    # ── Analgesic (drug-008 Ibuprofen 400mg) ──
    {"drug_id": "drug-008", "patient_id": "PATIENT-SIM-012", "quantity_prescribed": 20,
     "diagnosis_code": "M79.3", "notes": "Musculoskeletal pain"},
    # ── Antiviral (drug-015 Acyclovir) ──
    {"drug_id": "drug-015", "patient_id": "PATIENT-SIM-013", "quantity_prescribed": 35,
     "diagnosis_code": "B02.9", "notes": "Herpes zoster"},
    # ── Controlled: Tramadol drain (drug-009, 180 in stock) → PARTIALLY_DISPENSED ──
    {"drug_id": "drug-009", "patient_id": "PATIENT-SIM-014", "quantity_prescribed": 200,
     "diagnosis_code": "R52.2", "notes": "Chronic pain — post-operative"},
    # ── Controlled: Tramadol anomaly Rx (stock now 0) → OUT_OF_STOCK → orphan Rx ──
    {"drug_id": "drug-009", "patient_id": "PATIENT-SIM-015", "quantity_prescribed": 50,
     "diagnosis_code": "R52.2", "notes": "Chronic pain — physiotherapy adjunct"},
]

EXPIRY_SCANS = [
    {
        "scan_days_ahead": 365,
        "quarantine_expired": True,
        "_label": "Full facility expiry scan (365-day window)",
    },
    {
        "scan_days_ahead": 180,
        "drug_id": "drug-001",
        "quarantine_expired": False,
        "_label": "Amoxicillin 500mg near-expiry check (180-day window)",
    },
]


# ── Result tracking ─────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    phase: str
    label: str
    success: bool
    action_type: str
    summary: str
    error: str | None = None


@dataclass
class SimulationReport:
    steps: list[StepResult] = field(default_factory=list)

    def record(self, phase: str, label: str, response) -> StepResult:
        sr = StepResult(
            phase=phase,
            label=label,
            success=response.success,
            action_type=str(response.action_type),
            summary=_summarise(response),
            error=response.error,
        )
        self.steps.append(sr)
        return sr

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.success)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s.success)

    @property
    def system_errors(self) -> int:
        """Only SYSTEM_ERROR responses are unexpected failures; business failures
        (e.g. DISPENSING_FAILED for out-of-stock) are valid simulation outcomes."""
        return sum(1 for s in self.steps if "SYSTEM_ERROR" in s.action_type)


def _summarise(response) -> str:
    r = response.result or {}
    at = str(response.action_type)
    if "STOCK_RECEIVED" in at:
        return f"qty_added={r.get('quantity_added')} new_total={r.get('new_total')}"
    if "EXPIRY_CHECKED" in at:
        return (f"scanned={r.get('scanned_batches')} "
                f"expired={r.get('expired_count')} "
                f"near_expiry={r.get('near_expiry_count')}")
    if "REORDER_ALERT" in at:
        return f"drugs_checked={r.get('drugs_checked')} alerts={r.get('alerts_raised')}"
    if "PRESCRIPTION_CREATED" in at:
        return f"drug={r.get('drug_id','?')[:12]} qty={r.get('quantity_prescribed')}"
    if "DISPENSING_COMPLETE" in at:
        return (f"dispensed={r.get('quantity_dispensed')} "
                f"stock_left={r.get('stock_remaining')} "
                f"status={r.get('status')}")
    if "DISPENSING_FAILED" in at or "SYSTEM_ERROR" in at:
        return response.error or "see error"
    if "ANOMALY_FLAGGED" in at:
        return f"anomalies={r.get('anomalies_flagged', 0)}"
    if "AUDIT_REPORT" in at:
        stats = r.get("stats", {})
        return (f"prescriptions={stats.get('prescriptions_total',0)} "
                f"anomalies={r.get('anomaly_count',0)}")
    if "COMPLIANCE" in at:
        return f"controlled_drugs={r.get('controlled_drugs_active',0)}"
    return ""


# ── Print helpers ───────────────────────────────────────────────────────────────

def _banner(text: str) -> None:
    print(f"\n{'─' * 68}")
    print(f"  {text}")
    print(f"{'─' * 68}")


def _row(step: StepResult, verbose: bool = False) -> None:
    icon = "✓" if step.success else "✗"
    label = step.label[:40].ljust(40)
    summary = step.summary[:30].ljust(30) if not verbose else step.summary
    print(f"  {icon}  {label}  {summary}")
    if not step.success and step.error:
        print(f"       ERROR: {step.error}")


def _print_report(report: SimulationReport, verbose: bool) -> None:
    _banner("SIMULATION COMPLETE")
    current_phase = None
    for step in report.steps:
        if step.phase != current_phase:
            print(f"\n  [{step.phase}]")
            current_phase = step.phase
        _row(step, verbose)

    print(f"\n  Total steps   : {len(report.steps)}")
    print(f"  Succeeded     : {report.passed}")
    print(f"  Biz outcomes  : {report.failed - report.system_errors}  (expected e.g. OUT_OF_STOCK)")
    print(f"  System errors : {report.system_errors}")
    print()


# ── Simulation runner ───────────────────────────────────────────────────────────

async def _run(*, seed: bool, verbose: bool) -> int:
    from config.settings import settings
    from core.database import get_db, init_db, verify_connection
    from core.models import ActionType, AgentRole
    from core.orchestrator import build_default_orchestrator
    from core.schemas import AgentMessage

    if not verify_connection():
        logger.error("Database unreachable. Check DATABASE_URL or run with --seed.")
        return 1

    init_db()

    if seed:
        from scripts.seed_db import _load_json, seed_drugs, seed_stock

        with get_db() as session:
            drug_map = seed_drugs(session, _load_json(settings.drugs_catalog_path))
            seed_stock(session, _load_json(settings.seed_stock_path), drug_map)
        print("Database seeded.")

    orch = build_default_orchestrator(use_simulation_doctor=True)
    fac = settings.simulation_facility_id
    report = SimulationReport()

    _banner(f"eStock Simulator — scripted day at {fac}")

    # ── Phase 1: Stock Receipts ────────────────────────────────────────────────
    print("\n  [Phase 1] Stock Receipts")
    for receipt in STOCK_RECEIPTS:
        label = receipt.pop("_label")
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="simulation",
            recipient_role=AgentRole.STOCK_MANAGER,
            action_type=ActionType.STOCK_RECEIVED,
            facility_id=fac,
            payload=dict(receipt),
            requires_lock=True,
            priority=8,
        )
        resp = await orch.dispatch_immediate(msg)
        sr = report.record("Phase 1 — Stock Receipts", label, resp)
        _row(sr, verbose)

    # ── Phase 2: Prescriptions (→ auto-dispense via orchestrator queue) ────────
    print("\n  [Phase 2] Prescriptions (15) → auto-dispensed")
    rx_responses: list = []
    for i, rx_data in enumerate(PRESCRIPTIONS, start=1):
        label = f"Rx-{i:02d}  {rx_data['drug_id']}  {rx_data['patient_id']}"
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="simulation",
            recipient_role=AgentRole.DOCTOR,
            action_type=ActionType.PRESCRIPTION_CREATED,
            facility_id=fac,
            payload={
                "drug_id": rx_data["drug_id"],
                "patient_id": rx_data["patient_id"],
                "quantity_prescribed": rx_data["quantity_prescribed"],
                "diagnosis_code": rx_data.get("diagnosis_code"),
                "notes": rx_data.get("notes"),
                "facility_id": fac,
                "doctor_agent_id": "simulation-doctor",
            },
            priority=7,
        )
        # dispatch_immediate: DoctorAgent runs now, dispense follow-up is queued
        resp = await orch.dispatch_immediate(msg)
        sr = report.record("Phase 2 — Prescriptions", label, resp)
        _row(sr, verbose)
        rx_responses.append(resp)

    # Drain the orchestrator queue — runs all 15 dispense follow-ups
    print("\n  [Phase 2] Dispensing follow-ups...")
    dispense_responses = await orch.run_until_idle(max_messages=30)
    for i, resp in enumerate(dispense_responses, start=1):
        label = f"  Dispense-{i:02d}"
        sr = report.record("Phase 2 — Dispensing", label, resp)
        _row(sr, verbose)

    # ── Phase 3: Expiry Scans ──────────────────────────────────────────────────
    print("\n  [Phase 3] Expiry Scans")
    for scan in EXPIRY_SCANS:
        label = scan.pop("_label")
        msg = AgentMessage(
            sender_role=AgentRole.SYSTEM,
            sender_id="simulation",
            recipient_role=AgentRole.STOCK_MANAGER,
            action_type=ActionType.EXPIRY_CHECKED,
            facility_id=fac,
            payload=dict(scan),
            requires_lock=True,
            priority=6,
        )
        resp = await orch.dispatch_immediate(msg)
        sr = report.record("Phase 3 — Expiry Scans", label, resp)
        _row(sr, verbose)

    # ── Phase 4: Reorder Check ─────────────────────────────────────────────────
    print("\n  [Phase 4] Reorder Level Check")
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="simulation",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.REORDER_ALERT_RAISED,
        facility_id=fac,
        payload={},
        priority=5,
    )
    resp = await orch.dispatch_immediate(msg)
    sr = report.record("Phase 4 — Reorder Check", "Full facility reorder sweep", resp)
    _row(sr, verbose)

    # ── Phase 5: Anomaly Detection ─────────────────────────────────────────────
    print("\n  [Phase 5] Anomaly Detection")
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="simulation",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.ANOMALY_FLAGGED,
        facility_id=fac,
        payload={"scan_hours": 24},
        priority=4,
    )
    resp = await orch.dispatch_immediate(msg)
    sr = report.record("Phase 5 — Anomaly Detection", "24-hour anomaly scan", resp)
    _row(sr, verbose)
    anomaly_count = (resp.result or {}).get("anomalies_flagged", 0)

    # ── Phase 6: Audit Report ──────────────────────────────────────────────────
    print("\n  [Phase 6] Audit Report")
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="simulation",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.AUDIT_REPORT_GENERATED,
        facility_id=fac,
        payload={"include_anomalies": True},
        priority=3,
    )
    resp = await orch.dispatch_immediate(msg)
    sr = report.record("Phase 6 — Audit Report", "Full compliance audit report", resp)
    _row(sr, verbose)

    # ── Final summary ──────────────────────────────────────────────────────────
    _print_report(report, verbose)

    # Validate the key anomaly was detected
    if anomaly_count == 0:
        print("  WARNING: Expected at least 1 anomaly (orphan controlled-drug Rx), but none flagged.")
        print("           Check that Tramadol (drug-009) stock was fully drained by Rx-14.\n")
    else:
        print(f"  Anomaly check passed — {anomaly_count} anomaly/anomalies flagged as expected.\n")

    return 0 if report.system_errors == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(
        description="eStock Simulator — scripted full-day simulation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--seed",
        action="store_true",
        help="Seed the database with drugs + stock before running.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print full agent result payloads in the summary.",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(seed=args.seed, verbose=args.verbose)))


if __name__ == "__main__":
    main()
