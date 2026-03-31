"""
Seed the database with the drugs catalogue and initial stock levels.

Run:
    python scripts/seed_db.py [--reset]

Flags:
    --reset   Drop and recreate all tables before seeding (destructive).
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table
from rich import print as rprint

from config.settings import settings
from core.database import Base, engine, get_db, init_db, verify_connection
from core.models import Drug, StockLevel

console = Console()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")


# Helpers
def _parse_date(value: str) -> datetime:
    """Parse YYYY-MM-DD into a timezone-aware datetime at midnight UTC."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        console.print(f"[red]Seed file not found:[/red] {path}")
        sys.exit(1)
    with path.open() as fh:
        return json.load(fh)


# Seeding logic
def seed_drugs(session, catalog: list[dict]) -> dict[str, Drug]:
    """
    Upsert drugs from the catalogue JSON.

    Returns a mapping of drug id → Drug ORM object for use by stock seeding.
    """
    existing = {d.id: d for d in session.query(Drug).all()}
    inserted, updated = 0, 0

    for entry in catalog:
        drug_id = entry["id"]
        if drug_id in existing:
            drug = existing[drug_id]
            drug.reorder_threshold = entry.get("reorder_threshold", drug.reorder_threshold)
            drug.is_active = entry.get("is_active", True)
            updated += 1
        else:
            drug = Drug(
                id=drug_id,
                name=entry["name"],
                generic_name=entry["generic_name"],
                category=entry["category"],
                dosage_form=entry["dosage_form"],
                strength=entry["strength"],
                unit=entry["unit"],
                reorder_threshold=entry.get("reorder_threshold", 100),
                is_controlled=entry.get("is_controlled", False),
                is_active=entry.get("is_active", True),
            )
            session.add(drug)
            existing[drug_id] = drug
            inserted += 1

    session.flush()
    console.print(
        f"  [green]Drugs[/green]  inserted={inserted}  updated={updated}  "
        f"total={len(existing)}"
    )
    return existing


def seed_stock(session, stock_entries: list[dict], drug_map: dict[str, Drug]) -> None:
    """
    Upsert stock level batches.

    Uses (drug_id, facility_id, batch_number) as the natural key so re-running
    the script does not duplicate rows.
    """
    existing = {
        (s.drug_id, s.facility_id, s.batch_number): s
        for s in session.query(StockLevel).all()
    }
    inserted, updated, skipped = 0, 0, 0

    for entry in stock_entries:
        drug_id = entry["drug_id"]
        if drug_id not in drug_map:
            console.print(
                f"  [yellow]WARNING[/yellow] drug_id '{drug_id}' not in catalogue — skipping batch"
            )
            skipped += 1
            continue

        key = (drug_id, entry["facility_id"], entry["batch_number"])
        if key in existing:
            stock = existing[key]
            stock.quantity = entry["quantity"]
            stock.unit_cost = entry.get("unit_cost")
            updated += 1
        else:
            stock = StockLevel(
                drug_id=drug_id,
                facility_id=entry["facility_id"],
                batch_number=entry["batch_number"],
                quantity=entry["quantity"],
                unit_cost=entry.get("unit_cost"),
                expiry_date=_parse_date(entry["expiry_date"]),
                manufactured_date=(
                    _parse_date(entry["manufactured_date"])
                    if entry.get("manufactured_date")
                    else None
                ),
                supplier=entry.get("supplier"),
                location_in_store=entry.get("location_in_store"),
                is_quarantined=entry.get("is_quarantined", False),
            )
            session.add(stock)
            inserted += 1

    session.flush()
    console.print(
        f"  [green]Stock[/green]   inserted={inserted}  updated={updated}  "
        f"skipped={skipped}  total={inserted + updated}"
    )


def print_summary(session) -> None:
    """Print a Rich table summarising current stock after seeding."""
    rows = (
        session.query(Drug, StockLevel)
        .join(StockLevel, Drug.id == StockLevel.drug_id)
        .order_by(Drug.category, Drug.generic_name)
        .all()
    )

    table = Table(title="Seeded Stock Summary", show_lines=False)
    table.add_column("Drug", style="cyan", no_wrap=True)
    table.add_column("Category", style="magenta")
    table.add_column("Batch", style="dim")
    table.add_column("Qty", justify="right", style="green")
    table.add_column("Expires", style="yellow")
    table.add_column("Location", style="dim")

    for drug, stock in rows:
        expired = stock.expiry_date.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
        qty_str = f"[red]{stock.quantity}[/red]" if expired else str(stock.quantity)
        table.add_row(
            f"{drug.generic_name} {drug.strength}",
            drug.category.value,
            stock.batch_number,
            qty_str,
            stock.expiry_date.strftime("%Y-%m-%d"),
            stock.location_in_store or "—",
        )

    console.print(table)


# Entry point
def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the eStock-Simulator database.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop all tables and recreate before seeding (destructive).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        default=True,
        help="Print a stock summary table after seeding (default: on).",
    )
    parser.add_argument(
        "--no-summary",
        action="store_false",
        dest="summary",
        help="Skip the summary table.",
    )
    args = parser.parse_args()

    console.rule("[bold blue]eStock-Simulator  —  Database Seeder")
    console.print(f"  DB: [dim]{settings.database_url}[/dim]")

    if not verify_connection():
        console.print("[red]Cannot connect to database. Aborting.[/red]")
        sys.exit(1)

    if args.reset:
        console.print("[yellow]--reset flag set: dropping all tables...[/yellow]")
        Base.metadata.drop_all(bind=engine)
        console.print("  Tables dropped.")

    console.print("Initialising tables...")
    init_db()

    catalog = _load_json(settings.drugs_catalog_path)
    stock_entries = _load_json(settings.seed_stock_path)

    console.print(f"\nLoaded [bold]{len(catalog)}[/bold] drugs and "
                  f"[bold]{len(stock_entries)}[/bold] stock entries from seed files.\n")
    console.print("Seeding...")

    with get_db() as session:
        drug_map = seed_drugs(session, catalog)
        seed_stock(session, stock_entries, drug_map)

    console.print("\n[bold green]Seeding complete.[/bold green]")

    if args.summary:
        with get_db() as session:
            print_summary(session)


if __name__ == "__main__":
    main()
