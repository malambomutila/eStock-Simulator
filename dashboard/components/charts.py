"""
Visual components for the eStock dashboard.

Exports
───────
  build_stock_bar_chart(drugs)          → plotly Figure
  build_expiry_heatmap(batches)         → plotly Figure
  build_kpi_html(stats)                 → str (HTML)
  build_activity_ticker_html(items)     → str (HTML)
  build_alert_cards_html(alerts)        → str (HTML)
"""

from __future__ import annotations

from datetime import datetime, timezone

import plotly.graph_objects as go

# ── Palette ────────────────────────────────────────────────────────────────────

C = {
    "critical":  "#ef4444",
    "warning":   "#f59e0b",
    "ok":        "#10b981",
    "info":      "#60a5fa",
    "muted":     "#64748b",
    "purple":    "#a78bfa",
    "bg":        "#0f172a",
    "surface":   "#1e293b",
    "surface2":  "#273549",
    "border":    "#334155",
    "text":      "#e2e8f0",
    "subtext":   "#94a3b8",
}

ROLE_COLOR = {
    "DOCTOR":        "#60a5fa",
    "DISPENSER":     "#34d399",
    "STOCK_MANAGER": "#f59e0b",
    "GOV_OFFICIAL":  "#a78bfa",
    "ORCHESTRATOR":  "#fb923c",
    "SYSTEM":        "#64748b",
}

ACTION_EMOJI = {
    "PRESCRIPTION_CREATED":   "📋",
    "AVAILABILITY_CHECKED":   "🔍",
    "RESTOCK_REQUESTED":      "📩",
    "DISPENSING_STARTED":     "🏃",
    "DISPENSING_COMPLETE":    "✅",
    "DISPENSING_FAILED":      "❌",
    "STOCK_DEDUCTED":         "📉",
    "STOCK_RECEIVED":         "📦",
    "REORDER_ALERT_RAISED":   "🔔",
    "EXPIRY_CHECKED":         "⏳",
    "BATCH_EXPIRED":          "🗑️",
    "ANOMALY_FLAGGED":        "🚨",
    "AUDIT_REPORT_GENERATED": "📊",
    "COMPLIANCE_CHECKED":     "🏛️",
    "CONFLICT_RESOLVED":      "⚖️",
    "SYSTEM_ERROR":           "💥",
}

_LAYOUT_BASE = dict(
    plot_bgcolor=C["bg"],
    paper_bgcolor=C["surface"],
    font=dict(color=C["text"], family="'Inter', 'Segoe UI', sans-serif", size=12),
    margin=dict(l=10, r=20, t=50, b=10),
    xaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
    yaxis=dict(gridcolor=C["border"], zerolinecolor=C["border"]),
)


def _empty_figure(msg: str = "No data") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=msg, x=0.5, y=0.5, xref="paper", yref="paper",
        showarrow=False, font=dict(size=16, color=C["muted"]),
    )
    fig.update_layout(**_LAYOUT_BASE, height=300)
    return fig


# ── Stock bar chart ────────────────────────────────────────────────────────────

def build_stock_bar_chart(drugs: list[dict]) -> go.Figure:
    """
    Horizontal bar chart showing available stock vs reorder threshold.
    Bars are coloured red (critical), amber (low), or green (OK).
    A diamond marker shows the reorder threshold on each bar.
    Sorted worst-first so critical drugs float to the top.
    """
    if not drugs:
        return _empty_figure("No stock data — start the API and refresh")

    low_threshold_pct = 0.20
    drugs_sorted = sorted(
        drugs,
        key=lambda d: d["total_quantity"] / max(d["reorder_threshold"], 1),
    )[:30]

    names, quantities, thresholds, colors, hover = [], [], [], [], []
    for d in drugs_sorted:
        qty = d["total_quantity"]
        thr = max(d["reorder_threshold"], 1)
        ratio = qty / thr
        names.append(d["drug_name"][:38])
        quantities.append(qty)
        thresholds.append(thr)
        hover.append(
            f"<b>{d['drug_name']}</b><br>"
            f"Available: {qty:,} {d.get('unit','units')}<br>"
            f"Reorder at: {thr:,}<br>"
            f"Ratio: {ratio:.1%}"
        )
        if ratio < low_threshold_pct:
            colors.append(C["critical"])
        elif ratio < 1.0:
            colors.append(C["warning"])
        else:
            colors.append(C["ok"])

    fig = go.Figure()

    fig.add_trace(go.Bar(
        y=names,
        x=quantities,
        orientation="h",
        name="Stock available",
        marker=dict(color=colors, line=dict(width=0)),
        text=[f"  {q:,}" for q in quantities],
        textposition="inside",
        insidetextanchor="start",
        textfont=dict(color=C["bg"], size=11),
        hovertext=hover,
        hoverinfo="text",
    ))

    fig.add_trace(go.Scatter(
        y=names,
        x=thresholds,
        mode="markers",
        name="Reorder threshold",
        marker=dict(
            symbol="line-ew-open",
            size=14,
            color=C["warning"],
            line=dict(width=3, color=C["warning"]),
        ),
        hovertemplate="Threshold: %{x:,}<extra></extra>",
    ))

    height = max(420, len(names) * 30 + 80)
    fig.update_layout(
        **_LAYOUT_BASE,
        title=dict(text="Stock Levels vs Reorder Threshold  (sorted worst-first)", font=dict(size=14)),
        xaxis_title="Units available",
        height=height,
        bargap=0.25,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1,
            bgcolor="rgba(0,0,0,0)",
        ),
        hoverlabel=dict(bgcolor=C["surface2"], font_color=C["text"]),
    )
    return fig


# ── Expiry heatmap ─────────────────────────────────────────────────────────────

def build_expiry_heatmap(batches: list[dict]) -> go.Figure:
    """
    Scatter plot where each point is one stock batch.
      X  = days until expiry  (negative = already expired)
      Y  = drug name
      size = quantity in batch
      color = urgency band
    """
    if not batches:
        return _empty_figure("No batch data — start the API and refresh")

    now = datetime.now(timezone.utc)
    points = []
    for b in batches:
        raw = b.get("expiry_date")
        if not raw:
            continue
        try:
            exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        days = (exp - now).days
        points.append({
            "drug": b.get("drug_name", "?")[:35],
            "batch": b.get("batch_number", "?"),
            "days": days,
            "qty": max(b.get("quantity", 0), 1),
            "quarantined": b.get("is_quarantined", False),
        })

    if not points:
        return _empty_figure("No expiry data available")

    points.sort(key=lambda p: p["days"])

    def _color(days: int, quarantined: bool) -> str:
        if quarantined:
            return C["muted"]
        if days < 0:
            return C["critical"]
        if days < 30:
            return "#ff6b6b"
        if days < 90:
            return C["warning"]
        if days < 180:
            return "#a3e635"
        return C["ok"]

    def _label(days: int, quarantined: bool) -> str:
        if quarantined:
            return "quarantined"
        if days < 0:
            return f"expired {abs(days)}d ago"
        if days < 30:
            return f"expires in {days}d ⚠️"
        if days < 90:
            return f"expires in {days}d"
        return f"expires in {days}d ✓"

    drug_names = sorted({p["drug"] for p in points})
    drug_index = {d: i for i, d in enumerate(drug_names)}

    fig = go.Figure()

    for p in points:
        fig.add_trace(go.Scatter(
            x=[p["days"]],
            y=[drug_index[p["drug"]]],
            mode="markers",
            marker=dict(
                size=min(max(8, p["qty"] // 40), 32),
                color=_color(p["days"], p["quarantined"]),
                line=dict(width=1, color=C["border"]),
                opacity=0.5 if p["quarantined"] else 0.85,
            ),
            hovertemplate=(
                f"<b>{p['drug']}</b><br>"
                f"Batch: {p['batch']}<br>"
                f"Qty: {p['qty']:,}<br>"
                f"Status: {_label(p['days'], p['quarantined'])}"
                "<extra></extra>"
            ),
            showlegend=False,
        ))

    fig.add_vline(x=0,   line=dict(color=C["critical"], width=1, dash="dot"))
    fig.add_vline(x=30,  line=dict(color="#ff6b6b",     width=1, dash="dot"))
    fig.add_vline(x=90,  line=dict(color=C["warning"],  width=1, dash="dot"))
    fig.add_vline(x=180, line=dict(color="#a3e635",     width=1, dash="dot"))

    for x, label in [(0, "Expired"), (30, "30d"), (90, "90d"), (180, "180d")]:
        fig.add_annotation(
            x=x, y=len(drug_names) - 0.5,
            text=label, showarrow=False,
            font=dict(size=9, color=C["subtext"]),
            xanchor="left",
        )

    height = max(400, len(drug_names) * 24 + 80)
    layout = dict(**_LAYOUT_BASE)
    layout["yaxis"] = dict(
        tickvals=list(range(len(drug_names))),
        ticktext=drug_names,
        gridcolor=C["border"],
        zerolinecolor=C["border"],
    )
    fig.update_layout(
        **layout,
        title=dict(text="Batch Expiry Heatmap  (bubble size = quantity)", font=dict(size=14)),
        xaxis_title="Days until expiry",
        height=height,
        hoverlabel=dict(bgcolor=C["surface2"], font_color=C["text"]),
        showlegend=False,
    )
    return fig


# ── KPI badge row ──────────────────────────────────────────────────────────────

def build_kpi_html(
    total_drugs: int,
    low_stock: int,
    near_expiry: int,
    anomalies: int,
    total_prescriptions: int,
) -> str:
    def _card(label: str, value: int | str, color: str, icon: str) -> str:
        return f"""
        <div style="
            background:{C['surface2']};
            border:1px solid {C['border']};
            border-top:3px solid {color};
            border-radius:8px;
            padding:16px 20px;
            min-width:140px;
            flex:1;
        ">
            <div style="font-size:24px;margin-bottom:4px">{icon}</div>
            <div style="font-size:28px;font-weight:700;color:{color};line-height:1">{value}</div>
            <div style="font-size:12px;color:{C['subtext']};margin-top:4px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>
        </div>"""

    cards = "".join([
        _card("Drugs tracked",    total_drugs,         C["info"],     "💊"),
        _card("Low stock",        low_stock,            C["critical"] if low_stock else C["ok"], "📉"),
        _card("Expiry alerts",    near_expiry,          C["warning"]  if near_expiry else C["ok"], "⏳"),
        _card("Anomalies",        anomalies,            C["critical"] if anomalies else C["ok"], "🚨"),
        _card("Prescriptions",    total_prescriptions,  C["info"],     "📋"),
    ])

    return f"""
    <div style="
        font-family:'Inter','Segoe UI',sans-serif;
        display:flex;
        gap:12px;
        flex-wrap:wrap;
        padding:4px 0 16px 0;
    ">{cards}</div>
    """


# ── Activity ticker ────────────────────────────────────────────────────────────

def build_activity_ticker_html(items: list[dict], limit: int = 40) -> str:
    """
    Vertically scrollable activity feed.
    Each entry has a left border coloured by agent role.
    Anomalies pulse with a red background tint.
    """
    if not items:
        return (
            f'<div style="background:{C["surface"]};border-radius:8px;padding:24px;'
            f'text-align:center;color:{C["muted"]};font-family:monospace">'
            f"No recent activity — start the API and refresh</div>"
        )

    rows_html = []
    for item in items[:limit]:
        role = item.get("agent_role", "SYSTEM")
        action = item.get("action_type", "")
        is_anomaly = item.get("is_anomaly", False)
        ts = (item.get("timestamp") or "")[:19].replace("T", " ")
        entity_type = item.get("entity_type") or ""
        entity_id = (item.get("entity_id") or "")[:8]
        reason = item.get("anomaly_reason") or ""
        emoji = ACTION_EMOJI.get(action, "·")
        role_color = ROLE_COLOR.get(role, C["muted"])
        bg = "rgba(239,68,68,0.07)" if is_anomaly else "transparent"
        anomaly_badge = (
            f'<span style="background:{C["critical"]};color:white;font-size:9px;'
            f'padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:middle">ANOMALY</span>'
            if is_anomaly else ""
        )
        reason_html = (
            f'<div style="color:{C["warning"]};font-size:11px;margin-top:3px;'
            f'padding-left:2px">↳ {reason[:120]}</div>'
            if reason else ""
        )

        rows_html.append(f"""
        <div style="
            border-left:3px solid {role_color};
            background:{bg};
            padding:7px 12px;
            margin-bottom:4px;
            border-radius:0 6px 6px 0;
        ">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <span style="color:{C['subtext']};font-size:11px;font-family:monospace">{ts}</span>
                <span style="color:{role_color};font-weight:600;font-size:12px">{role}</span>
                <span style="color:{C['text']};font-size:13px">{emoji} {action}</span>
                {"<span style='color:" + C['subtext'] + ";font-size:11px'>— " + entity_type + " " + entity_id + "</span>" if entity_type else ""}
                {anomaly_badge}
            </div>
            {reason_html}
        </div>""")

    return f"""
    <div style="
        font-family:'Inter','Segoe UI',sans-serif;
        background:{C['surface']};
        border:1px solid {C['border']};
        border-radius:8px;
        padding:12px;
        max-height:520px;
        overflow-y:auto;
    ">
        <div style="color:{C['subtext']};font-size:11px;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.08em">
            Live Agent Activity Feed — {len(items)} events
        </div>
        {"".join(rows_html)}
    </div>
    """


# ── Alert cards ────────────────────────────────────────────────────────────────

def build_alert_cards_html(alerts: list[dict]) -> str:
    """
    Card-based alert display.
    Low stock = red border, expiry soon = amber border.
    """
    if not alerts:
        return (
            f'<div style="background:{C["surface"]};border-radius:8px;padding:24px;'
            f'text-align:center;color:{C["ok"]};font-family:sans-serif;font-size:16px">'
            f"✓ No active alerts</div>"
        )

    low_stock = [a for a in alerts if a.get("alert_type") == "low_stock"]
    expiry    = [a for a in alerts if a.get("alert_type") != "low_stock"]

    def _section(title: str, items: list[dict], border: str, icon: str) -> str:
        if not items:
            return ""
        cards = []
        for a in items:
            qty = a.get("current_quantity", "?")
            exp = (a.get("expiry_date") or "")[:10]
            detail = a.get("detail", "")
            cards.append(f"""
            <div style="
                background:{C['surface2']};
                border:1px solid {C['border']};
                border-left:4px solid {border};
                border-radius:6px;
                padding:12px 16px;
                margin-bottom:8px;
            ">
                <div style="display:flex;justify-content:space-between;align-items:flex-start">
                    <div>
                        <span style="font-size:16px">{icon}</span>
                        <span style="color:{C['text']};font-weight:600;margin-left:6px">{a.get('drug_name','?')}</span>
                    </div>
                    <span style="
                        background:{border};
                        color:{C['bg']};
                        font-size:11px;font-weight:700;
                        padding:2px 8px;border-radius:12px;
                    ">{qty} units</span>
                </div>
                <div style="color:{C['subtext']};font-size:12px;margin-top:6px">{detail}</div>
                {"<div style='color:" + C['muted'] + ";font-size:11px;margin-top:4px'>Expiry: " + exp + "</div>" if exp else ""}
            </div>""")
        return f"""
        <div style="margin-bottom:20px">
            <div style="color:{C['subtext']};font-size:11px;text-transform:uppercase;
                        letter-spacing:0.08em;margin-bottom:10px;font-weight:600">
                {icon} {title} &nbsp;<span style="background:{border};color:{C['bg']};
                    font-size:10px;padding:1px 7px;border-radius:10px">{len(items)}</span>
            </div>
            {"".join(cards)}
        </div>"""

    return f"""
    <div style="font-family:'Inter','Segoe UI',sans-serif;padding:4px 0">
        {_section("Low Stock", low_stock, C["critical"], "📉")}
        {_section("Expiring Soon", expiry, C["warning"], "⏳")}
    </div>
    """
