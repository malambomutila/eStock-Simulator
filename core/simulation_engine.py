"""
Animated simulation engine for the agent theater dashboard tab.

Each scenario runs real agents (DoctorAgent, DispenserAgent, StockManagerAgent,
GovOfficialAgent) through the orchestrator, then calls GPT-4o-mini to generate a
brief first-person narration for each step.  Events accumulate in a SimulationRun
object which the REST feed endpoint streams to the Gradio tab.

Public API
──────────
  start_simulation(scenario: str) → run_id: str
      Launch a background thread; returns immediately.

  get_run(run_id: str) → SimulationRun | None
      Read the live / finished run for polling.

Scenarios
─────────
  "Full Day"      15 Rx + 3 stock receipts + expiry scans + full audit
  "Anomaly"       Drain Tramadol → orphan controlled-drug Rx → Gov flags it
  "Limited Stock" High-volume prescriptions → reorder alerts
  "Expiry Alert"  Near-expiry batch scan + quarantine
  "Random"        LLM generates 4 patient cases → Doctor prescribes
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

SCENARIOS: dict[str, str] = {
    "Full Day":      "full_day",
    "Anomaly":       "anomaly",
    "Limited Stock": "limited_stock",
    "Expiry Alert":  "expiry_alert",
    "Random":        "random",
}

_PACING = 2.0  # seconds between steps (pacing delay)


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class SimEvent:
    seq:    int
    agent:  str        # "DOCTOR" | "DISPENSER" | "STOCK_MANAGER" | "GOV_OFFICIAL" | "SYSTEM"
    phase:  str        # human-readable phase label shown above the event
    speech: str        # narration sentence shown in the agent box
    detail: str | None = None   # smaller secondary line (drug + qty)
    status: str = "info"        # "info" | "success" | "warning" | "error" | "anomaly"
    ts:     str = ""


@dataclass
class SimulationRun:
    run_id:     str
    scenario:   str
    events:     list[SimEvent] = field(default_factory=list)
    status:     str = "starting"   # "starting" | "running" | "done" | "error"
    started_at: str = ""
    error:      str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _seq:  int = field(default=0, repr=False)

    def emit(
        self,
        agent:  str,
        phase:  str,
        speech: str,
        detail: str | None = None,
        status: str = "info",
    ) -> SimEvent:
        with self._lock:
            ev = SimEvent(
                seq=self._seq,
                agent=agent,
                phase=phase,
                speech=speech,
                detail=detail,
                status=status,
                ts=datetime.now(timezone.utc).strftime("%H:%M:%S"),
            )
            self.events.append(ev)
            self._seq += 1
            return ev

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "run_id":     self.run_id,
                "scenario":   self.scenario,
                "status":     self.status,
                "started_at": self.started_at,
                "error":      self.error,
                "events": [
                    {
                        "seq":    e.seq,
                        "agent":  e.agent,
                        "phase":  e.phase,
                        "speech": e.speech,
                        "detail": e.detail,
                        "status": e.status,
                        "ts":     e.ts,
                    }
                    for e in self.events
                ],
            }


# ── Global in-memory store ─────────────────────────────────────────────────────

_runs: dict[str, SimulationRun] = {}
_runs_lock = threading.Lock()


def get_run(run_id: str) -> SimulationRun | None:
    with _runs_lock:
        return _runs.get(run_id)


def list_runs() -> list[dict]:
    with _runs_lock:
        return [
            {"run_id": r.run_id, "scenario": r.scenario, "status": r.status,
             "started_at": r.started_at}
            for r in _runs.values()
        ]


def start_simulation(scenario: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    run = SimulationRun(
        run_id=run_id,
        scenario=scenario,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    with _runs_lock:
        _runs[run_id] = run
    t = threading.Thread(target=_run_thread, args=(run,), daemon=True)
    t.start()
    return run_id


def _run_thread(run: SimulationRun) -> None:
    try:
        asyncio.run(_run_scenario(run))
    except Exception as exc:
        logger.exception("Simulation run %s crashed: %s", run.run_id, exc)
        run.status = "error"
        run.error = str(exc)


# ── LLM narration ──────────────────────────────────────────────────────────────

_PERSONA: dict[str, str] = {
    "DOCTOR": (
        "You are Dr. Freeman Malambo, a clinical physician at a public health facility in Kenya. "
        "Speak in first person. Generate EXACTLY ONE sentence (≤ 30 words). "
        "RULE: You MUST name the exact drug (e.g. 'Amoxicillin 500mg Capsules') and the quantity. "
        "Example: 'I prescribed 14 capsules of Amoxicillin 500mg for patient PATIENT-001 to treat an upper respiratory infection.' "
        "Return only the sentence, no quotes, no prefix."
    ),
    "DISPENSER": (
        "You are Ms. Cynthia Dave Njeru, the pharmacy dispenser. Speak in first person. "
        "Generate EXACTLY ONE sentence (≤ 30 words). "
        "RULE: You MUST name the exact drug (e.g. 'Tramadol 50mg Capsules') and the exact quantity dispensed or refused. "
        "Example success: 'I dispensed 14 units of Amoxicillin 500mg Capsules, leaving 3,786 units in stock.' "
        "Example failure: 'I could not dispense Tramadol 50mg Capsules — no stock available at FACILITY-001.' "
        "Return only the sentence, no quotes, no prefix."
    ),
    "STOCK_MANAGER": (
        "You are Mr. Ibrahim, the pharmacy stock manager. Speak in first person. "
        "Generate EXACTLY ONE sentence (≤ 30 words). "
        "RULE: You MUST name the exact drug and state the exact quantity and new total. "
        "Example: 'I received 300 units of Amoxicillin 500mg Capsules from PharmaCo Ltd, bringing the total to 3,850 units.' "
        "Return only the sentence, no quotes, no prefix."
    ),
    "GOV_OFFICIAL": (
        "You are Inspector Hope Ogbons, a Ministry of Health compliance auditor. "
        "Speak in first person. Generate EXACTLY ONE sentence (≤ 35 words). "
        "RULE: You MUST name the exact drug involved in the finding and cite the exact numbers from the context. "
        "Example spike: 'I flagged Tramadol 50mg Capsules — 180 units dispensed in 24 hours versus a 6-unit daily average, exceeding the ×3 threshold.' "
        "Example orphan: 'I flagged prescription Rx-001 for Tramadol 50mg Capsules as a controlled-drug compliance violation — no DISPENSING_COMPLETE record found.' "
        "Return only the sentence, no quotes, no prefix."
    ),
}

_llm_client: Any = None


def _get_llm():
    global _llm_client
    if _llm_client is None and settings.openai_api_key:
        from openai import OpenAI
        _llm_client = OpenAI(api_key=settings.openai_api_key)
    return _llm_client


def _narrate(agent: str, context: str, fallback: str, system_prompt: str | None = None) -> str:
    """Legacy helper kept for anomaly/audit summary callers. Prefer _narrate_grounded()."""
    client = _get_llm()
    if not client:
        return fallback
    try:
        prompt = system_prompt or _PERSONA.get(agent, "Describe what you just did in one sentence.")
        resp = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.4,
            max_tokens=70,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": context},
            ],
        )
        text = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        return text or fallback
    except Exception as exc:
        logger.warning("Narration LLM call failed (%s): %s", agent, exc)
        return fallback


def _narrate_grounded(agent: str, template: str) -> str:
    """
    Lightly paraphrase *template* in the agent's voice while keeping every
    factual value (numbers, drug names, batch IDs, quantities) identical.

    The template is pre-built from real DB response values.  The LLM may only
    vary connecting words / phrasing.  A number-integrity guard falls back to
    the raw template if the LLM drifts from any token that looks like a number.
    """
    import re
    client = _get_llm()
    if not client:
        return template

    name, role = _AGENT_IDENTITY.get(agent, ("Staff", "Staff Member"))
    system = (
        f"You are {name}, {role}. "
        f"Rephrase the following statement in first person (≤ 35 words). "
        f"STRICT RULE: every number, drug name, unit count, batch ID, and supplier "
        f"name must appear EXACTLY as in the source — do not alter, round, or omit them. "
        f"You may vary only connecting words or sentence structure. "
        f"Return only the single sentence, no quotes, no prefix."
    )
    try:
        resp = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.4,
            max_tokens=70,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": template},
            ],
        )
        narrated = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        if not narrated:
            return template

        # Guard: every standalone number in the template must appear in the narration
        source_numbers = set(re.findall(r"\b\d[\d,]*\b", template))
        narrated_numbers = set(re.findall(r"\b\d[\d,]*\b", narrated))
        if source_numbers and not source_numbers.issubset(narrated_numbers):
            logger.debug(
                "Narration number drift for %s — reverting to template. "
                "Expected %s, got %s", agent, source_numbers, narrated_numbers
            )
            return template
        return narrated
    except Exception as exc:
        logger.warning("Grounded narration failed (%s): %s", agent, exc)
        return template


# ── Agent identity (name + role used by _narrate_grounded) ────────────────────

_AGENT_IDENTITY: dict[str, tuple[str, str]] = {
    "DOCTOR":        ("Dr. Freeman Malambo",   "Clinical Physician"),
    "DISPENSER":     ("Ms. Cynthia Dave Njeru", "Pharmacy Dispenser"),
    "STOCK_MANAGER": ("Mr. Ibrahim",            "Stock Manager"),
    "GOV_OFFICIAL":  ("Inspector Hope Ogbons",  "Ministry of Health Compliance Auditor"),
}


# ── Sweep/summary prompts (no single drug required) ───────────────────────────

_PROMPT_REORDER_SWEEP = (
    "You are Mr. Ibrahim, the pharmacy stock manager. Speak in first person. "
    "Generate EXACTLY ONE sentence (≤ 30 words) summarising the reorder sweep result you were just given. "
    "State exactly how many drugs were flagged and how many were checked. "
    "Do NOT invent drug names, quantities, or totals that are not in the context. "
    "Return only the sentence, no quotes."
)

_PROMPT_AUDIT_SUMMARY = (
    "You are Inspector Hope Ogbons, a Ministry of Health compliance auditor. "
    "Speak in first person. Generate EXACTLY ONE sentence (≤ 35 words) summarising the audit report. "
    "State the total anomalies found and reference any specific drugs mentioned in the context. "
    "Do NOT invent figures that are not in the context. "
    "Return only the sentence, no quotes."
)


# ── Drug-name resolver (for gov official anomaly context) ─────────────────────

def _resolve_drug_name(drug_id: str) -> str:
    """Look up the human-readable drug name from the DB. Falls back to drug_id."""
    try:
        from core.database import get_db
        from core.models import Drug
        with get_db() as session:
            drug = session.query(Drug).filter(Drug.id == drug_id).first()
            return drug.name if drug else drug_id
    except Exception:
        return drug_id


# ── Scenario runner entry point ────────────────────────────────────────────────

async def _run_scenario(run: SimulationRun) -> None:
    from core.models import AgentRole
    from core.orchestrator import build_default_orchestrator

    run.status = "running"
    fac = settings.simulation_facility_id

    # Fresh orchestrator per run (isolated state, LLM doctor)
    orch = build_default_orchestrator(use_simulation_doctor=False)

    run.emit("SYSTEM", "Init", f"Starting scenario: {run.scenario}", status="info")

    key = SCENARIOS.get(run.scenario, "full_day")
    if key == "anomaly":
        await _scenario_anomaly(run, orch, fac)
    elif key == "limited_stock":
        await _scenario_limited_stock(run, orch, fac)
    elif key == "expiry_alert":
        await _scenario_expiry_alert(run, orch, fac)
    elif key == "random":
        await _scenario_random(run, orch, fac)
    else:
        await _scenario_full_day(run, orch, fac)

    run.emit("SYSTEM", "Done", "✅ Simulation complete.", status="success")
    run.status = "done"


# ── Step helpers ───────────────────────────────────────────────────────────────

async def _dispatch_prescription(
    run: SimulationRun,
    orch: Any,
    fac: str,
    drug_id: str,
    patient_id: str,
    quantity: int,
    diagnosis_code: str,
    notes: str,
    phase: str,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id=fac,
        payload={
            "drug_id": drug_id,
            "patient_id": patient_id,
            "quantity_prescribed": quantity,
            "diagnosis_code": diagnosis_code,
            "notes": notes,
            "facility_id": fac,
        },
        priority=7,
    )

    # Doctor handles immediately; orchestrator queues dispense follow-up
    doctor_resp = await orch.dispatch_immediate(msg)
    r = doctor_resp.result or {}

    # Use drug_name from the DB response — never trust the caller's hint
    drug_name       = r.get("drug_name") or _resolve_drug_name(drug_id)
    qty_prescribed  = r.get("quantity_prescribed", quantity)

    if doctor_resp.success:
        template = (
            f"Prescribed {qty_prescribed} units of {drug_name} for patient {patient_id} "
            f"— {notes}."
        )
        speech = _narrate_grounded("DOCTOR", template)
        run.emit("DOCTOR", phase, speech,
                 detail=f"{drug_name} × {qty_prescribed} | {patient_id}",
                 status="info")
    else:
        run.emit("DOCTOR", phase,
                 f"Could not prescribe {drug_name}: {doctor_resp.error}",
                 status="error")
        return

    # Drain queue — runs exactly the one dispense follow-up
    disp_responses = await orch.run_until_idle(max_messages=2)
    for disp_resp in disp_responses:
        dr = disp_resp.result or {}
        if disp_resp.success:
            dispensed = dr.get("quantity_dispensed", qty_prescribed)
            remaining = dr.get("stock_remaining", "?")
            template = (
                f"Dispensed {dispensed} units of {drug_name} to patient {patient_id}; "
                f"{remaining} units remain in stock."
            )
            speech   = _narrate_grounded("DISPENSER", template)
            stat     = "success" if dispensed == qty_prescribed else "warning"
            run.emit("DISPENSER", phase, speech,
                     detail=f"Dispensed {dispensed}/{qty_prescribed} | Remaining: {remaining}",
                     status=stat)
        else:
            template = (
                f"Could not dispense {drug_name} — "
                f"{disp_resp.error or 'no stock available at this facility'}."
            )
            speech = _narrate_grounded("DISPENSER", template)
            run.emit("DISPENSER", phase, speech,
                     detail=f"Failed: {(disp_resp.error or '')[:60]}",
                     status="error")

    await asyncio.sleep(pacing)


async def _dispatch_stock_receipt(
    run: SimulationRun,
    orch: Any,
    fac: str,
    drug_id: str,
    drug_label: str,
    batch_number: str,
    quantity: int,
    expiry_date: str,
    supplier: str,
    phase: str,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.STOCK_RECEIVED,
        facility_id=fac,
        payload={
            "drug_id":      drug_id,
            "batch_number": batch_number,
            "quantity":     quantity,
            "expiry_date":  expiry_date,
            "facility_id":  fac,
            "supplier":     supplier,
            "unit_cost":    0.10,
        },
        requires_lock=True,
        priority=8,
    )

    resp = await orch.dispatch_immediate(msg)
    r = resp.result or {}

    # Use actual values from the DB response — not the caller's metadata
    actual_drug_name = _resolve_drug_name(r.get("drug_id") or drug_id)
    qty_added        = r.get("quantity_added", quantity)
    new_total        = r.get("new_total", "?")

    if resp.success:
        template = (
            f"Received {qty_added} units of {actual_drug_name} from {supplier}, "
            f"bringing the total to {new_total} units."
        )
        speech = _narrate_grounded("STOCK_MANAGER", template)
        run.emit("STOCK_MANAGER", phase, speech,
                 detail=f"+{qty_added} {actual_drug_name} | Total: {new_total}",
                 status="success")
    else:
        run.emit("STOCK_MANAGER", phase,
                 f"Stock receipt failed for {actual_drug_name}: {resp.error}",
                 status="error")

    await asyncio.sleep(pacing)


async def _dispatch_reorder_check(
    run: SimulationRun,
    orch: Any,
    fac: str,
    phase: str,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.REORDER_ALERT_RAISED,
        facility_id=fac,
        payload={},
        priority=5,
    )
    resp = await orch.dispatch_immediate(msg)
    r = resp.result or {}
    alerts  = r.get("alerts_raised", 0)
    checked = r.get("drugs_checked", "?")

    template = (
        f"Completed reorder sweep: {alerts} drug(s) flagged for reorder "
        f"out of {checked} active drugs checked."
    )
    speech = _narrate_grounded("STOCK_MANAGER", template)
    status = "warning" if alerts > 0 else "success"
    run.emit("STOCK_MANAGER", phase, speech,
             detail=f"{alerts} reorder alert(s)", status=status)

    await asyncio.sleep(pacing)


async def _dispatch_expiry_check(
    run: SimulationRun,
    orch: Any,
    fac: str,
    phase: str,
    scan_days: int = 365,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.STOCK_MANAGER,
        action_type=ActionType.EXPIRY_CHECKED,
        facility_id=fac,
        payload={"scan_days_ahead": scan_days, "quarantine_expired": True},
        requires_lock=True,
        priority=6,
    )
    resp = await orch.dispatch_immediate(msg)
    r = resp.result or {}
    expired  = r.get("expired_count", 0)
    near_exp = r.get("near_expiry_count", 0)
    scanned  = r.get("scanned_batches", "?")

    # Include sample drug names from the results so LLM can reference them
    all_details    = r.get("details", [])
    exp_batches    = [b for b in all_details if b.get("status") == "expired"]
    near_batches   = [b for b in all_details if b.get("status") == "near_expiry"]
    sample_expired  = [b.get("drug_name", b.get("drug_id", "")) for b in exp_batches[:2]]
    sample_near_exp = [b.get("drug_name", b.get("drug_id", "")) for b in near_batches[:2]]
    drug_mentions = ", ".join(filter(None, sample_expired + sample_near_exp)) or "no specific drugs named"

    template = (
        f"Scanned {scanned} batches in the {scan_days}-day window: "
        f"{expired} expired and quarantined, {near_exp} near expiry"
        + (f" — including {drug_mentions}" if drug_mentions != "no specific drugs named" else "")
        + "."
    )
    speech = _narrate_grounded("STOCK_MANAGER", template)
    status = "warning" if (expired + near_exp) > 0 else "success"
    run.emit("STOCK_MANAGER", phase, speech,
             detail=f"{expired} expired | {near_exp} near expiry", status=status)

    await asyncio.sleep(pacing)


async def _dispatch_anomaly_scan(
    run: SimulationRun,
    orch: Any,
    fac: str,
    phase: str,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    run.emit("GOV_OFFICIAL", phase, "Initiating anomaly scan of the last 24 hours…", status="info")
    await asyncio.sleep(0.5)

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.ANOMALY_FLAGGED,
        facility_id=fac,
        payload={"scan_hours": 24},
        priority=4,
    )
    resp = await orch.dispatch_immediate(msg)
    r = resp.result or {}
    flagged  = r.get("anomalies_flagged", 0)
    details  = r.get("details", [])

    for item in details[:5]:
        reason    = item.get("reason", "Anomaly detected.")
        item_drug_id  = item.get("drug_id")
        drug_name = item.get("drug_name")  # present for orphan-Rx items

        # Resolve drug_id → human name for dispensing-spike items
        if item_drug_id and not drug_name:
            drug_name = _resolve_drug_name(item_drug_id)

        drug_label = drug_name or item_drug_id or "unknown drug"
        # Build a grounded template from the actual reason string (contains real numbers)
        template = f"I flagged {drug_label}: {reason}"
        speech   = _narrate_grounded("GOV_OFFICIAL", template)
        run.emit("GOV_OFFICIAL", phase, speech, status="anomaly")
        await asyncio.sleep(0.4)

    if flagged == 0:
        run.emit("GOV_OFFICIAL", phase,
                 "No anomalies detected in the 24-hour scan window.", status="success")

    await asyncio.sleep(pacing)


async def _dispatch_audit_report(
    run: SimulationRun,
    orch: Any,
    fac: str,
    phase: str,
    pacing: float = _PACING,
) -> None:
    from core.models import ActionType, AgentRole
    from core.schemas import AgentMessage

    run.emit("GOV_OFFICIAL", phase, "Generating full compliance audit report…", status="info")
    await asyncio.sleep(0.5)

    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="sim-engine",
        recipient_role=AgentRole.GOV_OFFICIAL,
        action_type=ActionType.AUDIT_REPORT_GENERATED,
        facility_id=fac,
        payload={"include_anomalies": True},
        priority=3,
    )
    resp = await orch.dispatch_immediate(msg)
    r   = resp.result or {}
    llm = r.get("llm_analysis") or {}

    # Pull the narrative produced by GovOfficialAgent's own LLM call
    narrative = (
        llm.get("summary")
        or llm.get("narrative")
        or llm.get("compliance_summary")
        or llm.get("analysis")
        or ""
    )
    if not narrative:
        stats     = r.get("stats", {})
        narrative = (
            f"Audit complete: {stats.get('prescriptions_total', 0)} prescriptions, "
            f"{r.get('anomaly_count', 0)} anomalies flagged."
        )

    # Build a context that includes resolved drug names from anomaly details
    anomaly_details = r.get("anomalies", [])[:3]
    resolved_anomalies = []
    for a in anomaly_details:
        entity_type = a.get("entity_type", "")
        entity_id   = a.get("entity_id") or a.get("drug_id")
        anom_reason = a.get("anomaly_reason") or a.get("reason", "")
        drug_name   = a.get("drug_name")
        # Only resolve entity_id to drug name when it's a Drug entity
        if entity_type == "Drug" and entity_id and not drug_name:
            drug_name = _resolve_drug_name(entity_id)
        if drug_name:
            resolved_anomalies.append(f"{drug_name}: {anom_reason[:60]}")
        elif anom_reason:
            resolved_anomalies.append(anom_reason[:60])

    anomaly_count = r.get("anomaly_count", 0)
    stats         = r.get("stats", {})
    rx_total      = stats.get("prescriptions_total", 0)
    findings_str  = "; ".join(resolved_anomalies) if resolved_anomalies else "no specific drug anomalies"
    template = (
        f"Audit complete: {rx_total} prescriptions reviewed, "
        f"{anomaly_count} anomalies flagged — {findings_str}."
    )
    speech = _narrate_grounded("GOV_OFFICIAL", template)
    run.emit("GOV_OFFICIAL", phase, speech,
             detail=f"Anomalies: {r.get('anomaly_count', 0)}", status="info")

    await asyncio.sleep(pacing)


# ── Scenarios ──────────────────────────────────────────────────────────────────

async def _scenario_anomaly(run: SimulationRun, orch: Any, fac: str) -> None:
    """
    Drain Tramadol (drug-009) via two prescriptions → orphan controlled-drug Rx →
    Government Auditor flags it as anomaly.
    """
    phase = "Anomaly"
    run.emit("SYSTEM", phase,
             "🎯 Scenario: Controlled-drug anomaly — Tramadol dispensing trail", status="info")
    await asyncio.sleep(0.5)

    # Routine stock receipt to establish normal baseline
    await _dispatch_stock_receipt(
        run, orch, fac,
        drug_id="drug-001", drug_label="Amoxicillin 500mg",
        batch_number=f"ANM-AMX-{uuid.uuid4().hex[:6].upper()}",
        quantity=100, expiry_date="2028-12-31", supplier="PharmaCo Ltd",
        phase=phase,
    )

    # Two routine prescriptions (build activity baseline)
    run.emit("SYSTEM", phase, "📋 Routine consultations (building baseline activity)…", status="info")
    await _dispatch_prescription(
        run, orch, fac,
        drug_id="drug-001", patient_id="PATIENT-ANM-001", quantity=14,
        diagnosis_code="J06.9", notes="Upper respiratory infection",
        phase=phase,
    )
    await _dispatch_prescription(
        run, orch, fac,
        drug_id="drug-006", patient_id="PATIENT-ANM-002", quantity=20,
        diagnosis_code="R50.9", notes="Fever — symptomatic relief",
        phase=phase,
    )

    # Controlled drug: first Rx drains stock (PARTIALLY_DISPENSED)
    run.emit("SYSTEM", phase,
             "⚠️ Controlled drug (Tramadol) prescriptions — watch for anomaly…", status="warning")
    await _dispatch_prescription(
        run, orch, fac,
        drug_id="drug-009", patient_id="PATIENT-ANM-003", quantity=200,
        diagnosis_code="R52.2", notes="Post-operative chronic pain",
        phase=phase,
    )
    # Second Rx fails (out of stock) → orphan → anomaly
    await _dispatch_prescription(
        run, orch, fac,
        drug_id="drug-009", patient_id="PATIENT-ANM-004", quantity=50,
        diagnosis_code="R52.2", notes="Physiotherapy pain adjunct",
        phase=phase,
    )

    # Government audit
    run.emit("SYSTEM", phase, "🏛️ Government auditor initiating scan…", status="info")
    await _dispatch_anomaly_scan(run, orch, fac, phase=phase)
    await _dispatch_audit_report(run, orch, fac, phase=phase)


async def _scenario_limited_stock(run: SimulationRun, orch: Any, fac: str) -> None:
    """High-volume prescriptions → reorder alerts → Gov compliance check."""
    phase = "Limited Stock"
    run.emit("SYSTEM", phase,
             "📉 Scenario: High-volume day — testing low-stock and reorder triggers", status="info")
    await asyncio.sleep(0.5)

    prescriptions = [
        ("drug-002", "PATIENT-LS-001", 150, "J18.9", "Community-acquired pneumonia"),
        ("drug-007", "PATIENT-LS-002", 60,  "B54",   "Uncomplicated malaria — 3-day course"),
        ("drug-005", "PATIENT-LS-003", 80,  "N39.0", "Urinary tract infection"),
        ("drug-010", "PATIENT-LS-004", 50,  "A09",   "Acute gastroenteritis"),
        ("drug-016", "PATIENT-LS-005", 40,  "B02.9", "Herpes zoster treatment"),
    ]

    run.emit("SYSTEM", phase, "📋 Phase: High-demand consultations", status="info")
    for drug_id, patient, qty, diag, notes in prescriptions:
        await _dispatch_prescription(
            run, orch, fac,
            drug_id=drug_id, patient_id=patient, quantity=qty,
            diagnosis_code=diag, notes=notes,
            phase=phase,
        )

    run.emit("SYSTEM", phase, "🔔 Phase: Stock manager reorder sweep", status="info")
    await _dispatch_reorder_check(run, orch, fac, phase=phase)

    run.emit("SYSTEM", phase, "🏛️ Phase: Compliance check", status="info")
    await _dispatch_anomaly_scan(run, orch, fac, phase=phase)


async def _scenario_expiry_alert(run: SimulationRun, orch: Any, fac: str) -> None:
    """Stock manager scans for near-expiry batches and quarantines expired ones."""
    phase = "Expiry Alert"
    run.emit("SYSTEM", phase,
             "⏳ Scenario: Near-expiry batch scan, quarantine, and reorder", status="info")
    await asyncio.sleep(0.5)

    # Receive a batch that is close to expiry to make the scan interesting
    await _dispatch_stock_receipt(
        run, orch, fac,
        drug_id="drug-006", drug_label="Paracetamol 500mg",
        batch_number=f"EXP-PAR-{uuid.uuid4().hex[:6].upper()}",
        quantity=200, expiry_date="2026-06-30", supplier="MediSupply Inc",
        phase=phase,
    )
    await _dispatch_stock_receipt(
        run, orch, fac,
        drug_id="drug-001", drug_label="Amoxicillin 500mg",
        batch_number=f"EXP-AMX-{uuid.uuid4().hex[:6].upper()}",
        quantity=100, expiry_date="2026-05-15", supplier="PharmaCo Ltd",
        phase=phase,
    )

    run.emit("SYSTEM", phase, "🔍 Phase: Full expiry scan", status="info")
    await _dispatch_expiry_check(run, orch, fac, phase=phase, scan_days=365)
    await _dispatch_expiry_check(run, orch, fac, phase=phase, scan_days=90)

    run.emit("SYSTEM", phase, "🔔 Phase: Reorder check after expiry quarantine", status="info")
    await _dispatch_reorder_check(run, orch, fac, phase=phase)

    run.emit("SYSTEM", phase, "🏛️ Phase: Government audit", status="info")
    await _dispatch_anomaly_scan(run, orch, fac, phase=phase)


async def _scenario_random(run: SimulationRun, orch: Any, fac: str) -> None:
    """LLM generates 4 diverse patient cases → real DoctorAgent handles each."""
    from core.database import get_db
    from core.models import Drug

    phase = "Random"
    run.emit("SYSTEM", phase, "🎲 Scenario: LLM-generated random patient cases", status="info")
    await asyncio.sleep(0.5)

    # Fetch available drugs for the LLM to choose from
    with get_db() as session:
        drug_rows = session.query(Drug).filter(Drug.is_active.is_(True)).limit(20).all()
        catalog = [
            {"id": d.id, "name": d.name, "form": d.dosage_form,
             "strength": d.strength, "is_controlled": d.is_controlled}
            for d in drug_rows
        ]

    cases: list[dict] = []
    client = _get_llm()
    if client:
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                temperature=0.9,
                max_tokens=400,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are generating patient cases for a healthcare supply-chain simulation. "
                            "Return a JSON object with key 'cases' — an array of exactly 4 patient cases. "
                            "Each case must have: drug_id (from catalog), patient_id (e.g. PATIENT-RND-001), "
                            "quantity_prescribed (integer 10-40), diagnosis_code (ICD-10), symptoms (string). "
                            "Prefer non-controlled drugs. Use realistic clinical scenarios."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Drug catalog:\n{json.dumps(catalog, indent=2)}\n\nGenerate 4 patient cases.",
                    },
                ],
            )
            raw   = resp.choices[0].message.content or "{}"
            data  = json.loads(raw)
            cases = data.get("cases", [])
            run.emit("SYSTEM", phase,
                     f"LLM generated {len(cases)} patient case(s) — starting consultations…",
                     status="info")
        except Exception as exc:
            logger.warning("Random scenario LLM case generation failed: %s", exc)

    if not cases:
        cases = [
            {"drug_id": "drug-001", "patient_id": "PATIENT-RND-001",
             "quantity_prescribed": 14, "diagnosis_code": "J06.9", "symptoms": "Sore throat and mild fever"},
            {"drug_id": "drug-006", "patient_id": "PATIENT-RND-002",
             "quantity_prescribed": 20, "diagnosis_code": "R50.9", "symptoms": "High fever, chills"},
            {"drug_id": "drug-003", "patient_id": "PATIENT-RND-003",
             "quantity_prescribed": 10, "diagnosis_code": "N39.0", "symptoms": "Urinary burning, frequency"},
            {"drug_id": "drug-008", "patient_id": "PATIENT-RND-004",
             "quantity_prescribed": 15, "diagnosis_code": "M79.3", "symptoms": "Lower back pain"},
        ]
        run.emit("SYSTEM", phase, "Using fallback patient cases (LLM unavailable).", status="warning")

    run.emit("SYSTEM", phase, "📋 Phase: Doctor consultations", status="info")
    for case in cases:
        await _dispatch_prescription(
            run, orch, fac,
            drug_id=str(case.get("drug_id", "drug-001")),
            patient_id=str(case.get("patient_id", f"PATIENT-RND-{uuid.uuid4().hex[:4]}")),
            quantity=int(case.get("quantity_prescribed", 10)),
            diagnosis_code=str(case.get("diagnosis_code", "Z00.0")),
            notes=str(case.get("symptoms", "")),
            phase=phase,
        )

    run.emit("SYSTEM", phase, "🔔 Phase: Stock check after consultations", status="info")
    await _dispatch_reorder_check(run, orch, fac, phase=phase)

    run.emit("SYSTEM", phase, "🏛️ Phase: Gov audit", status="info")
    await _dispatch_anomaly_scan(run, orch, fac, phase=phase)


async def _scenario_full_day(run: SimulationRun, orch: Any, fac: str) -> None:
    """Full scripted day — mirrors the phases in scripts/run_simulation.py."""
    # Deep-copy to avoid mutating the module-level script lists
    from scripts.run_simulation import PRESCRIPTIONS, STOCK_RECEIPTS, EXPIRY_SCANS
    stock_receipts = copy.deepcopy(STOCK_RECEIPTS)
    prescriptions  = copy.deepcopy(PRESCRIPTIONS)
    expiry_scans   = copy.deepcopy(EXPIRY_SCANS)

    # Phase 1: Stock receipts
    run.emit("SYSTEM", "Stock Receipts",
             "📦 Phase 1: Receiving stock deliveries from suppliers", status="info")
    for receipt in stock_receipts:
        label      = receipt.pop("_label", receipt.get("drug_id", "?"))
        drug_label = label.split("(")[0].strip()
        await _dispatch_stock_receipt(
            run, orch, fac,
            drug_id=receipt["drug_id"],
            drug_label=drug_label,
            batch_number=receipt["batch_number"],
            quantity=receipt["quantity"],
            expiry_date=receipt["expiry_date"],
            supplier=receipt.get("supplier", "Supplier"),
            phase="Stock Receipts",
        )

    # Phase 2: Prescriptions
    run.emit("SYSTEM", "Prescriptions",
             "📋 Phase 2: Doctor consultations (15 patients today)", status="info")
    for i, rx in enumerate(prescriptions, 1):
        await _dispatch_prescription(
            run, orch, fac,
            drug_id=rx["drug_id"],
            patient_id=rx["patient_id"],
            quantity=rx["quantity_prescribed"],
            diagnosis_code=rx.get("diagnosis_code", "Z00.0"),
            notes=rx.get("notes", ""),
            phase=f"Prescriptions ({i}/15)",
        )

    # Phase 3: Expiry scans
    run.emit("SYSTEM", "Expiry Checks", "⏳ Phase 3: Expiry batch scans", status="info")
    for scan in expiry_scans:
        scan.pop("_label", None)
        await _dispatch_expiry_check(
            run, orch, fac,
            phase="Expiry Checks",
            scan_days=scan.get("scan_days_ahead", 365),
        )

    # Phase 4: Reorder
    run.emit("SYSTEM", "Reorder Check", "🔔 Phase 4: Facility-wide reorder level sweep", status="info")
    await _dispatch_reorder_check(run, orch, fac, phase="Reorder Check")

    # Phases 5–6: Government audit
    run.emit("SYSTEM", "Government Audit", "🏛️ Phases 5–6: Government audit", status="info")
    await _dispatch_anomaly_scan(run, orch, fac, phase="Government Audit")
    await _dispatch_audit_report(run, orch, fac, phase="Government Audit")
