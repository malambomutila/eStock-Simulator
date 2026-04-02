#!/usr/bin/env python3
"""
estock.py — Single-file launcher for the eStock Simulator.

Starts the system in three steps:
  1. Resets and seeds the database (clean slate — zero prescriptions / audit logs)
  2. Starts the FastAPI backend        → http://0.0.0.0:8000
  3. Starts the Gradio dashboard       → http://0.0.0.0:7860

The dashboard stays running. Open the 🎬 Simulate tab to run scenarios.
Press Ctrl+C to stop everything.

Usage:
    uv run python estock.py                # fresh reset + start
    uv run python estock.py --no-reset     # keep existing DB data, just start
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

ROOT   = Path(__file__).resolve().parent
PYTHON = sys.executable
LOG_DIR = ROOT / "logs"

_procs:     list[subprocess.Popen] = []
_log_files: list                   = []


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


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    bar = "─" * 64
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)


def _ok(msg: str)   -> None: print(f"  ✓  {msg}", flush=True)
def _info(msg: str) -> None: print(f"  ·  {msg}", flush=True)
def _err(msg: str)  -> None: print(f"  ✗  {msg}", flush=True)


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"]           = str(ROOT)
    env["API_RELOAD"]           = "false"
    env["PYTHONUNBUFFERED"]     = "1"
    env["ESTOCK_LAUNCHER_PID"]  = str(os.getpid())   # used by /api/shutdown
    return env


def _open_log(name: str):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    f = open(path, "w", buffering=1)
    _log_files.append(f)
    return f, path


def _run(*cmd: str) -> int:
    result = subprocess.run(list(cmd), cwd=ROOT, env=_base_env())
    if result.returncode != 0:
        _err(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        _shutdown()
    return result.returncode


def _free_port(port: int, label: str = "") -> None:
    """Kill any process currently listening on *port* so we can claim it cleanly."""
    name = f"{label} (port {port})" if label else f"port {port}"
    killed = False

    # Primary: fuser -k (most Linux distros)
    try:
        r = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            killed = True
    except FileNotFoundError:
        # Fallback: lsof (available on Ubuntu/Debian without psmisc)
        try:
            r2 = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in r2.stdout.splitlines() if p.strip()]
            for pid in pids:
                try:
                    subprocess.run(["kill", "-9", pid], timeout=3)
                except Exception:
                    pass
            if pids:
                killed = True
        except Exception:
            pass
    except Exception:
        pass

    if killed:
        _info(f"Released {name} (orphaned process killed)")
        time.sleep(1)   # give the OS a moment to release the socket


def _spawn(*cmd: str, log_name: str) -> tuple[subprocess.Popen, Path]:
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


def _wait_for_http(url: str, label: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    attempt  = 0
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
        "--no-reset",
        action="store_true",
        help="Keep existing database data instead of resetting to a clean state.",
    )
    args = parser.parse_args()

    _banner("eStock Simulator — Unified Launcher")
    _info(f"Project root : {ROOT}")
    _info(f"Python       : {PYTHON}")
    _info(f"Logs dir     : {LOG_DIR}")

    # ── Step 1: Seed database ──────────────────────────────────────────────────
    _banner("Step 1 / 3  —  Initialise database")
    if args.no_reset:
        _info("--no-reset: keeping existing database data")
        _run(PYTHON, "main.py", "--seed")
        _ok("Database ready (existing data preserved)")
    else:
        _info("Resetting database to clean state (use --no-reset to skip)")
        _run(PYTHON, "main.py", "--seed-reset")
        _ok("Database reset and seeded  (0 prescriptions · 0 audit logs)")

    # ── Step 2: FastAPI backend ────────────────────────────────────────────────
    _banner("Step 2 / 3  —  Start FastAPI backend  (port 8000)")
    _free_port(8000, "API")
    api_proc, api_log = _spawn(PYTHON, "main.py", "--serve", log_name="api.log")
    _info(f"API log → {api_log}")

    if not _wait_for_http("http://127.0.0.1:8000/health", "API"):
        _check_alive("API server", api_proc, api_log)
        _err("API did not become ready — check logs/api.log")
        _shutdown()

    # Confirm the *new* process is the one that's alive (not an old orphan)
    _check_alive("API server", api_proc, api_log)
    _ok("REST API    →  http://0.0.0.0:8000")
    _ok("Swagger UI  →  http://0.0.0.0:8000/docs")
    _ok("WebSocket   →  ws://0.0.0.0:8000/ws/events")

    # ── Step 3: Gradio dashboard ───────────────────────────────────────────────
    _banner("Step 3 / 3  —  Start Gradio dashboard  (port 7860)")
    _free_port(7860, "Dashboard")
    dash_proc, dash_log = _spawn(PYTHON, "main.py", "--dash", log_name="dashboard.log")
    _info(f"Dashboard log → {dash_log}")

    dash_ready = _wait_for_http("http://127.0.0.1:7860", "Dashboard", timeout=90)
    _check_alive("Gradio dashboard", dash_proc, dash_log)

    if dash_ready:
        _ok("Dashboard  →  http://0.0.0.0:7860")
    else:
        _info("Dashboard may still be loading — try the URL in a moment")

    # ── Ready ──────────────────────────────────────────────────────────────────
    _banner("System ready  —  press Ctrl+C to stop")
    print(f"""
  Open the dashboard and use the 🎬 Simulate tab to run a scenario.
  All agent dialogue is generated live by GPT-4o-mini.

  REST API   →  http://0.0.0.0:8000
  Swagger    →  http://0.0.0.0:8000/docs
  Dashboard  →  http://0.0.0.0:7860

  Simulate tab scenarios:
    Anomaly       — controlled-drug orphan prescription → Gov flags it
    Limited Stock — high-volume day → reorder alerts
    Expiry Alert  — near-expiry batch scan + quarantine
    Random        — LLM picks 4 patient cases → full chain
    Full Day      — all 15 Rx + receipts + expiry + audit

  Logs:
    tail -f {api_log}
    tail -f {dash_log}
""", flush=True)

    # Health-watch loop
    while True:
        time.sleep(3)
        for label, proc, log in [
            ("API server", api_proc, api_log),
            ("Dashboard",  dash_proc, dash_log),
        ]:
            if proc.poll() is not None:
                _err(f"{label} exited unexpectedly (code {proc.returncode}).")
                _info(f"Log: {log}")
                _shutdown()


if __name__ == "__main__":
    main()
