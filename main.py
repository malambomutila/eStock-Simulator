"""
eStock-Simulator entry point.

Usage:
    python main.py            — verify DB connection and print system info
    python main.py --seed     — initialise and seed the database
    python main.py --serve    — start the FastAPI backend (Eng5)
    python main.py --dash     — launch the Gradio dashboard (Eng5)
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="eStock-Simulator — AI healthcare stock management system"
    )
    parser.add_argument("--seed", action="store_true", help="Seed the database")
    parser.add_argument("--seed-reset", action="store_true", help="Reset and re-seed the database")
    parser.add_argument("--serve", action="store_true", help="Start the FastAPI API server")
    parser.add_argument("--dash", action="store_true", help="Launch the Gradio dashboard")
    args = parser.parse_args()

    if args.seed or args.seed_reset:
        from scripts.seed_db import main as seed_main
        sys.argv = ["seed_db"] + (["--reset"] if args.seed_reset else []) + ["--summary"]
        seed_main()
        return

    if args.serve:
        import uvicorn
        from config.settings import settings
        uvicorn.run(
            "api.routes:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=settings.api_reload,
        )
        return

    if args.dash:
        try:
            from dashboard.app import demo  # Eng5 owns dashboard/app.py
            demo.launch()
        except ImportError:
            # Fallback: launch the audit log component standalone until Eng5's app is ready
            from dashboard.components.audit_log import launch_standalone
            from config.settings import settings
            console = __import__("rich.console", fromlist=["Console"]).Console()
            console.print(
                "[yellow]dashboard/app.py not found — launching audit log in standalone mode.[/yellow]"
            )
            launch_standalone(facility_id=settings.simulation_facility_id)
        return

    # Default: health check
    from core.database import verify_connection, init_db
    from config.settings import settings
    from rich.console import Console
    from rich import print as rprint

    console = Console()
    console.rule("[bold blue]eStock-Simulator")
    console.print(f"  DB URL  : [dim]{settings.database_url}[/dim]")
    console.print(f"  LLM     : [dim]{settings.llm_model}[/dim]")
    console.print(f"  Facility: [dim]{settings.simulation_facility_id}[/dim]")

    db_ok = verify_connection()
    status = "[green]OK[/green]" if db_ok else "[red]UNREACHABLE[/red]"
    console.print(f"  DB ping : {status}")

    if db_ok:
        init_db()
        console.print("\nRun [bold]python main.py --seed[/bold] to populate with sample data.")
    else:
        console.print("\n[yellow]Tip:[/yellow] Run [bold]python scripts/seed_db.py[/bold] to create the database.")


if __name__ == "__main__":
    main()
