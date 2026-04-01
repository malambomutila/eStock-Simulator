"""
Audit Log dashboard component — Gradio implementation.

Renders a filterable, paginated transaction history table backed by the
AuditLog table. Anomalous rows are highlighted in amber.

Public API
──────────
    build_audit_log_tab(facility_id)
        Call this inside an existing gr.Blocks() / gr.Tab() context.
        Eng5's dashboard/app.py uses it like:

            with gr.Blocks() as demo:
                with gr.Tab("Audit Log"):
                    build_audit_log_tab(facility_id=settings.simulation_facility_id)

    launch_standalone(facility_id)
        Spin up a self-contained Gradio app — useful for isolated dev/testing.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import gradio as gr
import pandas as pd

from core.database import get_db
from core.models import ActionType, AgentRole, AuditLog


# Constants
_PAGE_SIZE = 50

_ACTION_EMOJI: dict[str, str] = {
    ActionType.PRESCRIPTION_CREATED.value:    "📋",
    ActionType.AVAILABILITY_CHECKED.value:    "🔍",
    ActionType.RESTOCK_REQUESTED.value:       "📩",
    ActionType.DISPENSING_STARTED.value:      "🏃",
    ActionType.DISPENSING_COMPLETE.value:     "✅",
    ActionType.DISPENSING_FAILED.value:       "❌",
    ActionType.STOCK_DEDUCTED.value:          "📉",
    ActionType.STOCK_RECEIVED.value:          "📦",
    ActionType.REORDER_ALERT_RAISED.value:    "🔔",
    ActionType.EXPIRY_CHECKED.value:          "⏳",
    ActionType.BATCH_EXPIRED.value:           "🗑️",
    ActionType.ANOMALY_FLAGGED.value:         "🚨",
    ActionType.AUDIT_REPORT_GENERATED.value:  "📊",
    ActionType.COMPLIANCE_CHECKED.value:      "🏛️",
    ActionType.CONFLICT_RESOLVED.value:       "⚖️",
    ActionType.SYSTEM_ERROR.value:            "💥",
}

_ROLE_OPTIONS = ["All"] + [r.value for r in AgentRole]
_ACTION_OPTIONS = ["All"] + [a.value for a in ActionType]
_ANOMALY_OPTIONS = ["All", "Anomalies only", "Normal only"]

_TABLE_COLS = ["Timestamp", "Role", "Action", "Agent", "Entity", "⚠️", "Reason", "Corr. ID"]


# Core query
def _query(
    facility_id: str | None,
    role: str,
    action: str,
    anomaly_filter: str,
    from_date: str,
    to_date: str,
    page: int,
) -> tuple[pd.DataFrame, int]:
    """
    Query AuditLog applying all active filters.

    Returns (page_dataframe, total_matching_rows).
    """
    from_ts = _parse_date(from_date, start_of_day=True)
    to_ts = _parse_date(to_date, start_of_day=False)
    is_anomaly: bool | None = None
    if anomaly_filter == "Anomalies only":
        is_anomaly = True
    elif anomaly_filter == "Normal only":
        is_anomaly = False

    with get_db() as session:
        q = session.query(AuditLog)

        if facility_id:
            q = q.filter(AuditLog.facility_id == facility_id)
        if role and role != "All":
            q = q.filter(AuditLog.agent_role == role)
        if action and action != "All":
            q = q.filter(AuditLog.action_type == action)
        if is_anomaly is not None:
            q = q.filter(AuditLog.is_anomaly == is_anomaly)
        if from_ts:
            q = q.filter(AuditLog.timestamp >= from_ts)
        if to_ts:
            q = q.filter(AuditLog.timestamp <= to_ts)

        total = q.count()
        rows = (
            q.order_by(AuditLog.timestamp.desc())
            .offset((page - 1) * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
            .all()
        )

    if not rows:
        return pd.DataFrame(columns=_TABLE_COLS), total

    data = []
    for row in rows:
        action_val = row.action_type.value if hasattr(row.action_type, "value") else str(row.action_type)
        role_val = row.agent_role.value if hasattr(row.agent_role, "value") else str(row.agent_role)
        emoji = _ACTION_EMOJI.get(action_val, "")
        data.append({
            "Timestamp":  row.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Role":       role_val,
            "Action":     f"{emoji} {action_val}",
            "Agent":      row.agent_id,
            "Entity":     f"{row.entity_type or '—'} / {row.entity_id[:8] if row.entity_id else '—'}",
            "⚠️":         "🚨" if row.is_anomaly else "",
            "Reason":     row.anomaly_reason or "",
            "Corr. ID":   row.correlation_id[:8] if row.correlation_id else "—",
            # kept for styling; dropped before display
            "_anomaly":   row.is_anomaly,
        })

    return pd.DataFrame(data), total


def _style_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a display-ready DataFrame.

    Rows flagged as anomalies get their Role and Action prefixed with a
    warning marker so they stand out even without CSS row colouring
    (Gradio's gr.Dataframe doesn't support per-row background styles).
    """
    out = df.drop(columns=["_anomaly"], errors="ignore").copy()
    if "_anomaly" in df.columns:
        mask = df["_anomaly"].astype(bool)
        out.loc[mask, "Role"] = "⚡ " + out.loc[mask, "Role"]
    return out


# Metrics helpers
def _metrics_md(df: pd.DataFrame, total: int, page: int) -> str:
    anomaly_count = int(df["_anomaly"].sum()) if "_anomaly" in df.columns and not df.empty else 0
    total_pages = max(1, -(-total // _PAGE_SIZE))
    return (
        f"**Total events (filtered):** {total} &nbsp;|&nbsp; "
        f"**Page:** {page} / {total_pages} &nbsp;|&nbsp; "
        f"**Shown:** {len(df)} &nbsp;|&nbsp; "
        f"**Anomalies on page:** {'🚨 ' if anomaly_count else ''}{anomaly_count}"
    )


def _page_info_md(total: int, page: int) -> str:
    total_pages = max(1, -(-total // _PAGE_SIZE))
    return f"Page **{page}** of **{total_pages}** ({total} total)"


# Date helpers
def _parse_date(value: str, start_of_day: bool) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        if not start_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _default_from() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")


def _default_to() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# CSV export
def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    export = df.drop(columns=["_anomaly"], errors="ignore")
    buf = io.BytesIO()
    export.to_csv(buf, index=False)
    return buf.getvalue()


# Gradio UI builder
def build_audit_log_tab(facility_id: str | None = None) -> None:
    """
    Declare all Gradio components and wire event handlers.

    Must be called inside an active gr.Blocks() context:

        with gr.Blocks() as demo:
            with gr.Tab("Audit Log"):
                build_audit_log_tab(facility_id="FACILITY-001")
    """

    # State
    page_state = gr.State(value=1)
    # Holds the raw DataFrame (including _anomaly column) for export reuse
    raw_df_state = gr.State(value=pd.DataFrame())

    # Header
    gr.Markdown("## 📋 Event Log & Audit Trail")
    gr.Markdown(
        "_Immutable transaction history — every agent action is recorded here._"
    )

    # Filters
    with gr.Row():
        role_dd = gr.Dropdown(
            choices=_ROLE_OPTIONS,
            value="All",
            label="Agent Role",
            scale=2,
        )
        action_dd = gr.Dropdown(
            choices=_ACTION_OPTIONS,
            value="All",
            label="Action Type",
            scale=3,
        )
        anomaly_radio = gr.Radio(
            choices=_ANOMALY_OPTIONS,
            value="All",
            label="Anomaly Filter",
            scale=2,
        )

    with gr.Row():
        from_box = gr.Textbox(
            value=_default_from(),
            label="From date (YYYY-MM-DD)",
            placeholder="e.g. 2026-01-01",
            scale=2,
        )
        to_box = gr.Textbox(
            value=_default_to(),
            label="To date (YYYY-MM-DD)",
            placeholder="e.g. 2026-12-31",
            scale=2,
        )
        with gr.Column(scale=1):
            refresh_btn = gr.Button("🔄 Refresh", variant="secondary", size="sm")
            clear_btn = gr.Button("✕ Clear filters", variant="stop", size="sm")

    # Metrics
    metrics_md = gr.Markdown(value="")

    # Table
    table = gr.Dataframe(
        headers=_TABLE_COLS,
        datatype=["str"] * len(_TABLE_COLS),
        interactive=False,
        wrap=False,
        row_count=(min(_PAGE_SIZE, 20), "dynamic"),
        col_count=(len(_TABLE_COLS), "fixed"),
        label="Audit events",
    )

    # Pagination
    with gr.Row():
        prev_btn = gr.Button("← Previous", size="sm", scale=1)
        with gr.Column(scale=4):
            page_info_md = gr.Markdown(value="")
        next_btn = gr.Button("Next →", size="sm", scale=1)

    gr.Markdown("---")

    # Export
    with gr.Row():
        export_btn = gr.Button("⬇ Export current page as CSV", size="sm", scale=1)
        csv_file = gr.File(label="Download", visible=False, scale=1)

    # Event handler logic
    def _run_query(role, action, anomaly_filter, from_date, to_date, page):
        df_raw, total = _query(
            facility_id=facility_id,
            role=role,
            action=action,
            anomaly_filter=anomaly_filter,
            from_date=from_date,
            to_date=to_date,
            page=page,
        )
        df_display = _style_anomalies(df_raw)
        metrics = _metrics_md(df_raw, total, page)
        pg_info = _page_info_md(total, page)
        return df_display, metrics, pg_info, df_raw

    def on_filter_change(role, action, anomaly_filter, from_date, to_date):
        # Any filter change resets to page 1
        df_display, metrics, pg_info, df_raw = _run_query(
            role, action, anomaly_filter, from_date, to_date, page=1
        )
        return df_display, metrics, pg_info, 1, df_raw

    def on_refresh(role, action, anomaly_filter, from_date, to_date, page):
        df_display, metrics, pg_info, df_raw = _run_query(
            role, action, anomaly_filter, from_date, to_date, page
        )
        return df_display, metrics, pg_info, df_raw

    def on_prev(role, action, anomaly_filter, from_date, to_date, page):
        new_page = max(1, page - 1)
        df_display, metrics, pg_info, df_raw = _run_query(
            role, action, anomaly_filter, from_date, to_date, new_page
        )
        return df_display, metrics, pg_info, new_page, df_raw

    def on_next(role, action, anomaly_filter, from_date, to_date, page):
        _, total = _query(
            facility_id=facility_id,
            role=role,
            action=action,
            anomaly_filter=anomaly_filter,
            from_date=from_date,
            to_date=to_date,
            page=1,
        )
        total_pages = max(1, -(-total // _PAGE_SIZE))
        new_page = min(total_pages, page + 1)
        df_display, metrics, pg_info, df_raw = _run_query(
            role, action, anomaly_filter, from_date, to_date, new_page
        )
        return df_display, metrics, pg_info, new_page, df_raw

    def on_clear():
        return (
            "All",                # role_dd
            "All",                # action_dd
            "All",                # anomaly_radio
            _default_from(),      # from_box
            _default_to(),        # to_box
        )

    def on_export(df_raw: pd.DataFrame):
        if df_raw.empty:
            return gr.update(visible=False)
        csv_bytes = _to_csv_bytes(df_raw)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tmp_path = f"/tmp/audit_log_{ts}.csv"
        with open(tmp_path, "wb") as fh:
            fh.write(csv_bytes)
        return gr.update(value=tmp_path, visible=True)

    # Wire up events
    filter_inputs = [role_dd, action_dd, anomaly_radio, from_box, to_box]
    query_outputs = [table, metrics_md, page_info_md, page_state, raw_df_state]

    for component in [role_dd, action_dd, anomaly_radio]:
        component.change(
            fn=on_filter_change,
            inputs=filter_inputs,
            outputs=query_outputs,
        )

    for date_box in [from_box, to_box]:
        date_box.submit(
            fn=on_filter_change,
            inputs=filter_inputs,
            outputs=query_outputs,
        )

    refresh_btn.click(
        fn=on_refresh,
        inputs=filter_inputs + [page_state],
        outputs=[table, metrics_md, page_info_md, raw_df_state],
    )

    clear_btn.click(
        fn=on_clear,
        inputs=None,
        outputs=filter_inputs,
    )

    prev_btn.click(
        fn=on_prev,
        inputs=filter_inputs + [page_state],
        outputs=query_outputs,
    )

    next_btn.click(
        fn=on_next,
        inputs=filter_inputs + [page_state],
        outputs=query_outputs,
    )

    export_btn.click(
        fn=on_export,
        inputs=[raw_df_state],
        outputs=[csv_file],
    )


# Standalone launcher
def launch_standalone(facility_id: str | None = None, **kwargs) -> None:
    """
    Launch the audit log as a standalone Gradio app.

    Useful during development before Eng5's dashboard/app.py is ready.

        python -c "from dashboard.components.audit_log import launch_standalone; launch_standalone()"
    """
    with gr.Blocks(
        title="eStock — Audit Log",
        theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate"),
        css=".gradio-container { max-width: 1400px !important; }",
    ) as demo:
        build_audit_log_tab(facility_id=facility_id)

    demo.launch(**kwargs)
