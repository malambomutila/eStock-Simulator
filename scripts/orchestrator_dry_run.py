#!/usr/bin/env python3
"""
Simulated end-to-end dry run: SimulationDoctor creates an Rx → orchestrator
enqueues DispenserAgent → stock updates + audit (via dispenser).

Usage (from repo root):
    python scripts/orchestrator_dry_run.py --seed
    python scripts/orchestrator_dry_run.py   # expects an already-seeded DB

Requires: same ``DATABASE_URL`` / default ``data/estock.db`` as the app.
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("orchestrator_dry_run")


async def _run(*, seed: bool) -> int:
    from config.settings import settings
    from core.database import get_db, init_db, verify_connection
    from core.models import ActionType, AgentRole, StockLevel
    from core.orchestrator import build_default_orchestrator
    from core.schemas import AgentMessage
    from sqlalchemy import select

    if not verify_connection():
        logger.error("Database unreachable. Check DATABASE_URL / run with --seed.")
        return 1

    init_db()

    if seed:
        from scripts.seed_db import _load_json, seed_drugs, seed_stock

        with get_db() as session:
            drug_map = seed_drugs(session, _load_json(settings.drugs_catalog_path))
            seed_stock(session, _load_json(settings.seed_stock_path), drug_map)
        logger.info("Database seeded.")

    with get_db() as session:
        sl = session.scalars(
            select(StockLevel).where(
                StockLevel.facility_id == settings.simulation_facility_id,
                StockLevel.quantity > 2,
            ).limit(1)
        ).first()
        if sl is None:
            logger.error(
                "No stock row with quantity > 2 for facility %s — run with --seed.",
                settings.simulation_facility_id,
            )
            return 1
        drug_id = sl.drug_id
        fac = settings.simulation_facility_id

    orch = build_default_orchestrator(use_simulation_doctor=True)
    msg = AgentMessage(
        sender_role=AgentRole.SYSTEM,
        sender_id="orchestrator-dry-run",
        recipient_role=AgentRole.DOCTOR,
        action_type=ActionType.PRESCRIPTION_CREATED,
        facility_id=fac,
        payload={
            "drug_id": drug_id,
            "facility_id": fac,
            "patient_id": "PATIENT-DRY-RUN",
            "doctor_agent_id": "dry-run-doctor",
            "quantity_prescribed": 2,
        },
    )
    orch.submit(msg)
    responses = await orch.run_until_idle(max_messages=10)

    for i, r in enumerate(responses):
        logger.info(
            "Step %d: success=%s action=%s correlation_id=%s error=%s",
            i + 1,
            r.success,
            r.action_type,
            r.correlation_id,
            r.error,
        )

    if len(responses) < 2 or not all(r.success for r in responses):
        logger.error("Dry run did not complete successfully.")
        return 1

    logger.info("Dry run OK (%d orchestrator step(s)).", len(responses))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="eStock orchestrator simulated dry run")
    p.add_argument(
        "--seed",
        action="store_true",
        help="Load drugs + stock from JSON into the configured database first",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(seed=args.seed)))


if __name__ == "__main__":
    main()
