"""
Gradio entrypoint: audit log (direct DB) + stock / alerts / activity via REST polling.

Run the API first: ``python main.py --serve``, then ``python main.py --dash``.
"""

from __future__ import annotations

import httpx
import gradio as gr
import pandas as pd

from config.settings import settings
from dashboard.components.audit_log import build_audit_log_tab

_POLL_SECONDS = 30.0


def _api_base() -> str:
    return f"http://{settings.api_host}:{settings.api_port}"


def _facility_path(suffix: str) -> str:
    fac = settings.simulation_facility_id
    return f"{_api_base()}/api/facilities/{fac}{suffix}"


def fetch_stock_summary() -> pd.DataFrame:
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(_facility_path("/stock"), params={"include_batches": False})
            r.raise_for_status()
        data = r.json()
        drugs = data.get("drugs") or []
        if not drugs:
            return pd.DataFrame(
                columns=["Drug", "Total qty", "Reorder at", "Unit", "Form", "Strength"]
            )
        rows = [
            {
                "Drug": d["drug_name"],
                "Total qty": d["total_quantity"],
                "Reorder at": d["reorder_threshold"],
                "Unit": d["unit"],
                "Form": d["dosage_form"],
                "Strength": d["strength"],
            }
            for d in drugs
        ]
        return pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame({"Error": [str(exc)]})


def fetch_alerts() -> pd.DataFrame:
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(_facility_path("/alerts"))
            r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return pd.DataFrame(columns=["Type", "Drug", "Detail", "Qty", "Expiry"])
        rows = []
        for a in items:
            rows.append(
                {
                    "Type": a.get("alert_type", ""),
                    "Drug": a.get("drug_name", ""),
                    "Detail": a.get("detail", ""),
                    "Qty": a.get("current_quantity", ""),
                    "Expiry": a.get("expiry_date") or "",
                }
            )
        return pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame({"Error": [str(exc)]})


def fetch_activity() -> pd.DataFrame:
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(_facility_path("/activity"), params={"limit": 80})
            r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return pd.DataFrame(columns=["Time", "Role", "Action", "Entity", "Anomaly"])
        rows = []
        for row in items:
            ts = row.get("timestamp")
            rows.append(
                {
                    "Time": ts[:19] if isinstance(ts, str) else str(ts),
                    "Role": row.get("agent_role", ""),
                    "Action": row.get("action_type", ""),
                    "Entity": (row.get("entity_type") or "")
                    + " "
                    + (row.get("entity_id") or ""),
                    "Anomaly": row.get("is_anomaly", False),
                }
            )
        return pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame({"Error": [str(exc)]})


with gr.Blocks(title="eStock Simulator") as demo:
    gr.Markdown(
        f"# eStock Simulator\n\n"
        f"Facility **{settings.simulation_facility_id}** · API `{_api_base()}`. "
        f"Start the backend with `python main.py --serve` before using Stock / Alerts / Activity."
    )

    with gr.Tab("Audit Log"):
        build_audit_log_tab(facility_id=settings.simulation_facility_id)

    with gr.Tab("Stock"):
        stock_df = gr.Dataframe(label="Inventory (totals per drug)", interactive=False)
        stock_refresh = gr.Button("Refresh now")
        stock_timer = gr.Timer(_POLL_SECONDS)
        stock_refresh.click(fetch_stock_summary, outputs=stock_df)
        demo.load(fetch_stock_summary, outputs=stock_df)
        stock_timer.tick(fetch_stock_summary, outputs=stock_df)

    with gr.Tab("Alerts"):
        alerts_df = gr.Dataframe(label="Low stock & expiry", interactive=False)
        alerts_refresh = gr.Button("Refresh now")
        alerts_timer = gr.Timer(_POLL_SECONDS)
        alerts_refresh.click(fetch_alerts, outputs=alerts_df)
        demo.load(fetch_alerts, outputs=alerts_df)
        alerts_timer.tick(fetch_alerts, outputs=alerts_df)

    with gr.Tab("Activity"):
        activity_df = gr.Dataframe(label="Recent audit activity (REST)", interactive=False)
        activity_refresh = gr.Button("Refresh now")
        activity_timer = gr.Timer(_POLL_SECONDS)
        activity_refresh.click(fetch_activity, outputs=activity_df)
        demo.load(fetch_activity, outputs=activity_df)
        activity_timer.tick(fetch_activity, outputs=activity_df)
