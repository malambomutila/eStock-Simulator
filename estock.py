#!/usr/bin/env python3
"""
estock.py — Single-file launcher for the eStock Simulator.

Runs the complete system in order:
  1. Seeds the database (drugs + stock batches)
  2. Starts the FastAPI backend        → http://127.0.0.1:8000
  3. Starts the Gradio dashboard       → http://127.0.0.1:7860
  4. Runs the full scripted simulation (15 Rx · 3 receipts · 2 expiry scans · 1 anomaly)

After the simulation completes the API and dashboard stay running.
Open the dashboard and click "Refresh now" on each tab to explore results.
Press Ctrl+C to stop everything.

Usage:
    uv run python estock.py           # normal first run
    uv run python estock.py --reset   # drop + re-seed DB, then run
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
LOG_DIR = ROOT / "logs"

# Background processes we need to clean up on exit
_procs: list[subprocess.Popen] = []
_log_files: list = []


# ── Cleanup ────────────────────────────────────────────────────────────────────

def _shutdown(sig=None, frame=None) -> None:
    print("\n\n  Shutting down all services...", flush=True)
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in _procs:
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    for f in _log_files:
        try:
            f.close()
        except Exception:
            pass
    print("  All stopped. Goodbye.\n", flush=True)
    sys.exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    bar = "─" * 64
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar, flush=True)


def _ok(msg: str) -> None:
    print(f"  ✓  {msg}", flush=True)


def _info(msg: str) -> None:
    print(f"  ·  {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"  ✗  {msg}", flush=True)


def _base_env() -> dict[str, str]:
    """Environment variables inherited by all child processes."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["API_RELOAD"] = "false"       # disable uvicorn file-watcher for subprocess stability
    env["PYTHONUNBUFFERED"] = "1"     # ensures subprocess output is not buffered
    return env


def _open_log(name: str):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    f = open(path, "w", buffering=1)
    _log_files.append(f)
    return f, path


def _run(*cmd: str, check: bool = True) -> int:
    """Run a command in the foreground, inheriting this process's stdout/stderr."""
    result = subprocess.run(list(cmd), cwd=ROOT, env=_base_env())
    if check and result.returncode != 0:
        _err(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        _shutdown()
    return result.returncode


def _spawn(*cmd: str, log_name: str) -> tuple[subprocess.Popen, Path]:
    """
    Start a long-running process in the background.
    stdout+stderr go to a log file so the console stays clean.
    """
    log_f, log_path = _open_log(log_name)
    proc = subprocess.Popen(
        list(cmd),
        cwd=ROOT,
        env=_base_env(),
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    _procs.append(proc)
    return proc, log_path


def _wait_for_http(url: str, label: str, timeout: int = 45) -> bool:
    """Poll url every second until it returns HTTP 200 or timeout expires."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    print(f"\r  ✓  {label} ready.{' ' * 30}", flush=True)
                    return True
        except Exception:
            pass
        dots = "." * (attempt % 4 + 1)
        print(f"\r  ·  Waiting for {label}{dots}{' ' * 20}", end="", flush=True)
        attempt += 1
        time.sleep(1)
    print(f"\r  ✗  {label} did not respond within {timeout}s.{' ' * 10}", flush=True)
    return False


def _check_alive(label: str, proc: subprocess.Popen, log_path: Path) -> None:
    """Abort if a background process has already died."""
    if proc.poll() is not None:
        _err(f"{label} exited early (code {proc.returncode}).")
        _info(f"Check the log: {log_path}")
        _shutdown()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="eStock Simulator — unified launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and re-seed the database before starting.",
    )
    args = parser.parse_args()

    _banner("eStock Simulator — Unified Launcher")
    _info(f"Project root : {ROOT}")
    _info(f"Python       : {PYTHON}")
    _info(f"Logs dir     : {LOG_DIR}")

    # ── Step 1: Seed database ──────────────────────────────────────────────────
    _banner("Step 1 / 4  —  Seed database")
    seed_flag = "--seed-reset" if args.reset else "--seed"
    _run(PYTHON, "main.py", seed_flag)
    _ok("Database seeded")

    # ── Step 2: FastAPI backend ────────────────────────────────────────────────
    _banner("Step 2 / 4  —  Start FastAPI backend  (port 8000)")
    api_proc, api_log = _spawn(PYTHON, "main.py", "--serve", log_name="api.log")
    _info(f"API log → {api_log}")

    if not _wait_for_http("http://127.0.0.1:8000/health", "API"):
        _check_alive("API server", api_proc, api_log)
        _err("API did not become ready — check logs/api.log")
        _shutdown()

    _check_alive("API server", api_proc, api_log)
    _ok("REST API    →  http://127.0.0.1:8000")
    _ok("Swagger UI  →  http://127.0.0.1:8000/docs")
    _ok("WebSocket   →  ws://127.0.0.1:8000/ws/events")

    # ── Step 3: Gradio dashboard ───────────────────────────────────────────────
    _banner("Step 3 / 4  —  Start Gradio dashboard  (port 7860)")
    dash_proc, dash_log = _spawn(PYTHON, "main.py", "--dash", log_name="dashboard.log")
    _info(f"Dashboard log → {dash_log}")

    dash_ready = _wait_for_http("http://127.0.0.1:7860", "Dashboard", timeout=45)
    _check_alive("Gradio dashboard", dash_proc, dash_log)

    if dash_ready:
        _ok("Dashboard  →  http://127.0.0.1:7860")
    else:
        _info("Dashboard may still be loading — try http://127.0.0.1:7860 in a moment")

    # ── Step 4: Simulation ─────────────────────────────────────────────────────
    _banner("Step 4 / 4  —  Run simulation")
    _info("15 prescriptions · 3 stock receipts · 2 expiry scans · 1 anomaly")
    _info("Simulation output follows:\n")

    # Small buffer so the dashboard finishes its startup before agents start writing
    time.sleep(2)

    # DB already seeded in step 1 — run without --seed to avoid duplicate inserts
    _run(PYTHON, "scripts/run_simulation.py")

    # ── All done — keep services running ──────────────────────────────────────
    _banner("All services running  —  press Ctrl+C to stop")
    lines = [
        "",
        "  REST API   →  http://127.0.0.1:8000",
        "  Swagger    →  http://127.0.0.1:8000/docs",
        "  Dashboard  →  http://127.0.0.1:7860",
        "",
        "  Dashboard tabs to explore:",
        "    Audit Log  — every agent action, anomaly flags, correlation IDs",
        "    Stock      — live inventory totals per drug",
        "    Alerts     — low-stock + near-expiry warnings",
        "    Activity   — last 80 audit entries (role · action · anomaly flag)",
        "",
        "  Agent logs:",
        f"    tail -f {api_log}",
        f"    tail -f {dash_log}",
        "",
    ]
    print("\n".join(lines), flush=True)

    # Health-watch loop — exit if a service crashes
    while True:
        time.sleep(3)
        for label, proc, log in [
            ("API server", api_proc, api_log),
            ("Dashboard", dash_proc, dash_log),
        ]:
            if proc.poll() is not None:
                _err(f"{label} exited unexpectedly (code {proc.returncode}).")
                _info(f"Log: {log}")
                _shutdown()


if __name__ == "__main__":
    main()
