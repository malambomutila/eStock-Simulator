# Eng 5 — HTTP API + WebSocket (what was added)

## When does the event bus publish (and the WebSocket receive anything)?

Only when the **orchestrator** finishes an agent turn **successfully** inside `core/orchestrator.py`: after `agent.handle(message)` returns, it publishes to `estock.agent.response` and `estock.orchestrator.task.completed`.

- **REST calls** (read stock, audit, health, etc.) **do not** publish — they only hit the database.
- **Idle server** → no publishes → WebSocket clients see nothing until something runs through the orchestrator in **that same process**.
- **Scripts** like `orchestrator_dry_run.py` run in **another process** with their own bus, so they **won’t** show up on the API server’s WebSocket.

To see live WS traffic later, you need a flow that **`submit`s / `dispatch_immediate`s messages to `app.state.orchestrator`** in the running API process (e.g. a future `POST` endpoint or an in-app demo).

---

## What was implemented

- **`api/routes.py`** — FastAPI `app`: lifespan (`init_db`, shared `EventBus`, `build_default_orchestrator`), CORS (`settings.cors_origins`), REST routes, includes WebSocket router.
- **`api/websocket.py`** — `/ws/events` bridges the sync `EventBus` to WebSocket clients (optional `?topics=`).
- **`api/schemas.py`** — JSON response shapes for the API.
- **`config/settings.py`** — `cors_origins` for Gradio / local dev.
- **`scripts/ws_smoke_test.py`** — quick check: connect + timeout if nothing publishes (proves the socket works).

### REST endpoints (summary)

| Method | Path |
|--------|------|
| GET | `/health` |
| GET | `/api/facilities/{facility_id}/stock` |
| GET | `/api/facilities/{facility_id}/prescriptions` |
| GET | `/api/facilities/{facility_id}/audit-log` |
| GET | `/api/facilities/{facility_id}/activity` |
| GET | `/api/facilities/{facility_id}/alerts` |
| GET | `/api/drugs` |

### Run

```bash
PYTHONPATH=. uv run python main.py --serve
```

WebSocket: `ws://127.0.0.1:8000/ws/events` (port from `settings.api_port`).
