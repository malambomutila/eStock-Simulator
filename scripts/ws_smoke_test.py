#!/usr/bin/env python3
"""
Smoke-test the WebSocket endpoint (run API server in another terminal first).

    PYTHONPATH=. uv run python scripts/ws_smoke_test.py

By default the server emits **no** events until something uses the orchestrator,
so the first receive uses a timeout — that still proves the socket connected.

Optional: pass a URL (e.g. custom port):

    PYTHONPATH=. uv run python scripts/ws_smoke_test.py ws://127.0.0.1:8000/ws/events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def main(url: str, wait_s: float, max_msgs: int) -> int:
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            print("connected:", url, file=sys.stderr)
            for _ in range(max_msgs):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
                except asyncio.TimeoutError:
                    print(
                        f"(no message within {wait_s}s — normal if nothing published to the event bus)",
                        file=sys.stderr,
                    )
                    return 0
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    print(raw)
                else:
                    print(json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to connect: {exc}", file=sys.stderr)
        print("Is the API running?  PYTHONPATH=. uv run python main.py --serve", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Connect to /ws/events and print messages.")
    p.add_argument(
        "url",
        nargs="?",
        default="ws://127.0.0.1:8000/ws/events",
        help="WebSocket URL",
    )
    p.add_argument(
        "-w",
        "--wait",
        type=float,
        default=3.0,
        help="Seconds to wait for each message (default: 3)",
    )
    p.add_argument(
        "-n",
        "--messages",
        type=int,
        default=1,
        help="Max messages to print (default: 1)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.url, args.wait, args.messages)))
