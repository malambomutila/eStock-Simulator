"""
eStock Simulator — Gradio dashboard.

Tabs (in order)
────────────────
  🎬 Simulate  Choose a scenario, watch agents converse live, data auto-refreshes
  📊 Overview  KPI badges · stock bar chart · live activity ticker
  📋 Audit Log Filterable, paginated, exportable audit trail (DB-direct)
  📦 Stock     Bar chart + inventory table
  ⏳ Expiry    Batch expiry heatmap + near-expiry table
  🔔 Alerts    Card-based alert feed (low stock + expiry)
  ⚡ Activity  Full-width coloured activity ticker

All tabs auto-refresh via gr.Timer — no manual refresh buttons needed.
"""

from __future__ import annotations

import httpx
import gradio as gr
import pandas as pd

from config.settings import settings
from dashboard.components.audit_log import build_audit_log_tab
from dashboard.components.simulate_tab import build_simulate_tab
from dashboard.components.charts import (
    build_activity_ticker_html,
    build_alert_cards_html,
    build_expiry_heatmap,
    build_kpi_html,
    build_stock_bar_chart,
)

_POLL_FAST = 10.0   # activity + alerts
_POLL_SLOW = 30.0   # stock + expiry


# ── API helpers ────────────────────────────────────────────────────────────────

def _api_base() -> str:
    host = "127.0.0.1" if settings.api_host in ("0.0.0.0", "") else settings.api_host
    return f"http://{host}:{settings.api_port}"


def _fac(suffix: str) -> str:
    return f"{_api_base()}/api/facilities/{settings.simulation_facility_id}{suffix}"


def _get(url: str, **params) -> dict | list | None:
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(url, params=params or None)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


# ── Fetch helpers ──────────────────────────────────────────────────────────────

def _fetch_drugs() -> list[dict]:
    data = _get(_fac("/stock"), include_batches=False)
    return (data or {}).get("drugs") or []


def _fetch_batches() -> list[dict]:
    data = _get(_fac("/stock"), include_batches=True)
    return (data or {}).get("batches") or []


def _fetch_alerts() -> list[dict]:
    data = _get(_fac("/alerts"))
    return (data or {}).get("items") or []


def _fetch_activity(limit: int = 60) -> list[dict]:
    data = _get(_fac("/activity"), limit=limit)
    return (data or {}).get("items") or []


def _fetch_prescriptions() -> int:
    data = _get(_fac("/prescriptions"), limit=1, offset=0)
    return (data or {}).get("total", 0)


def _fetch_anomaly_count() -> int:
    data = _get(_fac("/audit-log"), anomaly="true", page=1, page_size=1)
    return (data or {}).get("total", 0)


# ── Composed refresh functions ─────────────────────────────────────────────────

def refresh_overview():
    drugs     = _fetch_drugs()
    alerts    = _fetch_alerts()
    activity  = _fetch_activity(limit=40)
    rx_total  = _fetch_prescriptions()
    anomalies = _fetch_anomaly_count()

    low_stock   = sum(1 for a in alerts if a.get("alert_type") == "low_stock")
    near_expiry = sum(1 for a in alerts if a.get("alert_type") != "low_stock")

    kpi_html = build_kpi_html(
        total_drugs=len(drugs),
        low_stock=low_stock,
        near_expiry=near_expiry,
        anomalies=anomalies,
        total_prescriptions=rx_total,
    )
    chart       = build_stock_bar_chart(drugs[:25])
    ticker_html = build_activity_ticker_html(activity, limit=20)
    return kpi_html, chart, ticker_html


def refresh_stock():
    drugs = _fetch_drugs()
    chart = build_stock_bar_chart(drugs)
    if not drugs:
        return chart, pd.DataFrame(columns=["Drug", "Available", "Reorder at", "Status", "Unit"])
    rows = []
    for d in sorted(drugs, key=lambda x: x["drug_name"]):
        qty   = d["total_quantity"]
        thr   = max(d["reorder_threshold"], 1)
        ratio = qty / thr
        status = "🔴 Critical" if ratio < 0.20 else "🟡 Low" if ratio < 1.0 else "🟢 OK"
        rows.append({
            "Drug":       d["drug_name"],
            "Available":  qty,
            "Reorder at": thr,
            "Status":     status,
            "Unit":       d.get("unit", ""),
        })
    return chart, pd.DataFrame(rows)


def refresh_expiry():
    from datetime import datetime, timezone
    batches = _fetch_batches()
    alerts  = _fetch_alerts()
    chart   = build_expiry_heatmap(batches)
    near    = [a for a in alerts if a.get("alert_type") != "low_stock"]
    if not near:
        return chart, pd.DataFrame(columns=["Drug", "Batch", "Qty", "Expiry", "Days left"])
    now  = datetime.now(timezone.utc)
    rows = []
    for a in sorted(near, key=lambda x: x.get("expiry_date") or ""):
        exp_str = (a.get("expiry_date") or "")[:10]
        try:
            exp = datetime.fromisoformat((a.get("expiry_date") or "").replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            days = (exp - now).days
        except Exception:
            days = "?"
        rows.append({
            "Drug":      a.get("drug_name", "?"),
            "Batch":     a.get("batch_number", ""),
            "Qty":       a.get("current_quantity", ""),
            "Expiry":    exp_str,
            "Days left": days,
        })
    return chart, pd.DataFrame(rows)


def refresh_alerts():
    alerts = _fetch_alerts()
    cards  = build_alert_cards_html(alerts)
    if not alerts:
        return cards, pd.DataFrame(columns=["Type", "Drug", "Detail", "Qty", "Expiry"])
    rows = [{
        "Type":   a.get("alert_type", ""),
        "Drug":   a.get("drug_name", ""),
        "Detail": a.get("detail", ""),
        "Qty":    a.get("current_quantity", ""),
        "Expiry": (a.get("expiry_date") or "")[:10],
    } for a in alerts]
    return cards, pd.DataFrame(rows)


def refresh_activity():
    items = _fetch_activity(limit=80)
    return build_activity_ticker_html(items, limit=80)


# ── CSS ────────────────────────────────────────────────────────────────────────

_CSS = """
.gradio-container { font-family: 'Inter', 'Segoe UI', sans-serif !important; max-width: 1400px !important; }
.gr-dataframe td, .gr-dataframe th { padding: 6px 10px !important; font-size: 13px !important; }
.plot-container { background: transparent !important; }
"""


# ── App ────────────────────────────────────────────────────────────────────────

LAUNCH_KWARGS: dict = dict(
    theme=gr.themes.Soft(
        primary_hue="indigo",
        neutral_hue="slate",
        font=gr.themes.GoogleFont("Inter"),
    ),
    css=_CSS,
)

with gr.Blocks(title="eStock Simulator") as demo:

    fac = settings.simulation_facility_id
    api = _api_base()

    gr.HTML(f"""
    <div style="
        background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
        border-bottom:1px solid #334155;
        padding:20px 28px 16px;
        margin-bottom:4px;
    ">
        <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
            <div>
                <h1 style="margin:0;font-size:24px;font-weight:700;color:#e2e8f0;letter-spacing:-0.02em">
                    💊 eStock Simulator
                </h1>
                <div style="color:#64748b;font-size:13px;margin-top:4px">
                    Facility <b style="color:#94a3b8">{fac}</b>
                    &nbsp;·&nbsp; API
                    <code style="background:#1e293b;padding:1px 6px;border-radius:4px;color:#60a5fa">{api}</code>
                    &nbsp;·&nbsp;
                    <span style="color:#475569">Use the 🎬 Simulate tab to run a scenario — other tabs update automatically</span>
                </div>
            </div>
        </div>
    </div>
    """)

    # ── Simulate tab — FIRST ─────────────────────────────────────────────────
    with gr.Tab("🎬 Simulate"):
        build_simulate_tab()

    # ── Overview tab ──────────────────────────────────────────────────────────
    with gr.Tab("📊 Overview"):
        kpi_box        = gr.HTML()
        overview_chart = gr.Plot(label="Stock bar chart")
        gr.Markdown("### Live Activity Feed")
        overview_ticker = gr.HTML()
        overview_timer  = gr.Timer(value=_POLL_FAST)

        demo.load(refresh_overview, outputs=[kpi_box, overview_chart, overview_ticker])
        overview_timer.tick(refresh_overview, outputs=[kpi_box, overview_chart, overview_ticker])

    # ── Audit Log tab ─────────────────────────────────────────────────────────
    with gr.Tab("📋 Audit Log"):
        build_audit_log_tab(facility_id=fac)

    # ── Stock tab ─────────────────────────────────────────────────────────────
    with gr.Tab("📦 Stock"):
        stock_chart = gr.Plot(label="Stock levels")
        stock_table = gr.Dataframe(label="Inventory detail", interactive=False, wrap=False)
        stock_timer = gr.Timer(value=_POLL_SLOW)

        demo.load(refresh_stock, outputs=[stock_chart, stock_table])
        stock_timer.tick(refresh_stock, outputs=[stock_chart, stock_table])

    # ── Expiry tab ────────────────────────────────────────────────────────────
    with gr.Tab("⏳ Expiry"):
        expiry_chart = gr.Plot(label="Batch expiry heatmap")
        expiry_table = gr.Dataframe(label="Near-expiry batches", interactive=False, wrap=False)
        expiry_timer = gr.Timer(value=_POLL_SLOW)

        demo.load(refresh_expiry, outputs=[expiry_chart, expiry_table])
        expiry_timer.tick(refresh_expiry, outputs=[expiry_chart, expiry_table])

    # ── Alerts tab ────────────────────────────────────────────────────────────
    with gr.Tab("🔔 Alerts"):
        alert_cards = gr.HTML()
        alert_table = gr.Dataframe(label="All alerts", interactive=False, wrap=False)
        alerts_timer = gr.Timer(value=_POLL_FAST)

        demo.load(refresh_alerts, outputs=[alert_cards, alert_table])
        alerts_timer.tick(refresh_alerts, outputs=[alert_cards, alert_table])

    # ── Activity tab ──────────────────────────────────────────────────────────
    with gr.Tab("⚡ Activity"):
        activity_ticker = gr.HTML()
        activity_timer  = gr.Timer(value=_POLL_FAST)

        demo.load(refresh_activity, outputs=[activity_ticker])
        activity_timer.tick(refresh_activity, outputs=[activity_ticker])
