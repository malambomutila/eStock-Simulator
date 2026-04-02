"""
Agent Theater — Gradio tab component.

Renders four agent conversation boxes (Doctor, Dispenser, Stock Manager,
Government Official) that fill in real-time as a simulation scenario runs.

Public API
──────────
    build_simulate_tab()
        Call inside an active gr.Blocks() / gr.Tab() context.
"""

from __future__ import annotations

import httpx
import gradio as gr

from config.settings import settings
from core.simulation_engine import SCENARIOS

# ── Palette (matches charts.py) ────────────────────────────────────────────────

_C = {
    "bg":       "#0f172a",
    "surface":  "#1e293b",
    "surface2": "#273549",
    "border":   "#334155",
    "text":     "#e2e8f0",
    "subtext":  "#94a3b8",
    "muted":    "#64748b",
    "ok":       "#10b981",
    "warning":  "#f59e0b",
    "critical": "#ef4444",
    "info":     "#60a5fa",
    "purple":   "#a78bfa",
}

_AGENT_META = {
    "DOCTOR":        {"icon": "👨‍⚕️", "label": "Dr. Freeman Malambo",   "color": "#60a5fa", "role": "Clinical Physician"},
    "DISPENSER":     {"icon": "💊",   "label": "Ms. Cynthia Dave Njeru", "color": "#34d399", "role": "Pharmacy Dispenser"},
    "STOCK_MANAGER": {"icon": "📦",   "label": "Mr. Ibrahim",            "color": "#f59e0b", "role": "Stock Manager"},
    "GOV_OFFICIAL":  {"icon": "🏛️",  "label": "Inspector Hope Ogbons",  "color": "#a78bfa", "role": "Gov't Auditor"},
}

_STATUS_COLOR = {
    "info":    "#60a5fa",
    "success": "#10b981",
    "warning": "#f59e0b",
    "error":   "#ef4444",
    "anomaly": "#ef4444",
}

_STATUS_DOT = {
    "info":    "◉",
    "success": "●",
    "warning": "◆",
    "error":   "✖",
    "anomaly": "🚨",
}


# ── API helper ─────────────────────────────────────────────────────────────────

def _api(suffix: str) -> str:
    host = "127.0.0.1" if settings.api_host in ("0.0.0.0", "") else settings.api_host
    return f"http://{host}:{settings.api_port}{suffix}"


def _post(url: str, body: dict) -> dict | None:
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(url, json=body)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def _get_feed(run_id: str) -> dict | None:
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(_api(f"/api/simulate/{run_id}/feed"))
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


# ── HTML builders ──────────────────────────────────────────────────────────────

def _status_bar_html(status: str, scenario: str, run_id: str, event_count: int) -> str:
    colour = {"running": _C["warning"], "done": _C["ok"],
              "error": _C["critical"], "starting": _C["info"]}.get(status, _C["muted"])
    label  = {"running": "⚡ Running…", "done": "✅ Done",
               "error": "❌ Error", "starting": "⏳ Starting…"}.get(status, status)
    return f"""
    <div style="
        background:{_C['surface2']};border:1px solid {colour};
        border-radius:8px;padding:10px 18px;margin-bottom:14px;
        display:flex;align-items:center;gap:16px;flex-wrap:wrap;
        font-family:'Inter','Segoe UI',sans-serif;
    ">
        <span style="color:{colour};font-weight:700;font-size:14px">{label}</span>
        <span style="color:{_C['subtext']};font-size:13px">Scenario: <b style='color:{_C['text']}'>{scenario}</b></span>
        <span style="color:{_C['muted']};font-size:12px">Run: {run_id[:8]}</span>
        <span style="color:{_C['muted']};font-size:12px;margin-left:auto">{event_count} events</span>
    </div>"""


def _agent_box_html(agent_key: str, events: list[dict]) -> str:
    meta   = _AGENT_META[agent_key]
    color  = meta["color"]
    icon   = meta["icon"]
    label  = meta["label"]
    role   = meta["role"]

    agent_events = [e for e in events if e.get("agent") == agent_key]

    rows = []
    for ev in agent_events[-30:]:   # show last 30 per box
        ts     = ev.get("ts", "")
        speech = ev.get("speech", "")
        detail = ev.get("detail") or ""
        status = ev.get("status", "info")
        phase  = ev.get("phase", "")
        dot    = _STATUS_DOT.get(status, "◉")
        sc     = _STATUS_COLOR.get(status, color)
        bg     = "rgba(239,68,68,0.09)" if status == "anomaly" else "transparent"

        detail_html = (
            f'<div style="color:{_C["subtext"]};font-size:11px;margin-top:2px'
            f';padding-left:16px">{detail}</div>'
        ) if detail else ""

        phase_html = (
            f'<span style="color:{_C["muted"]};font-size:10px;'
            f'margin-left:6px;font-style:italic">[{phase}]</span>'
        ) if phase else ""

        rows.append(f"""
        <div style="
            border-left:3px solid {sc};
            background:{bg};
            padding:6px 10px;
            margin-bottom:5px;
            border-radius:0 5px 5px 0;
        ">
            <div style="display:flex;align-items:flex-start;gap:6px">
                <span style="color:{sc};font-size:11px;min-width:20px;padding-top:1px">{dot}</span>
                <div style="flex:1">
                    <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
                        <span style="color:{_C['subtext']};font-size:10px;font-family:monospace">{ts}</span>
                        {phase_html}
                    </div>
                    <div style="color:{_C['text']};font-size:13px;line-height:1.4;margin-top:2px">{speech}</div>
                    {detail_html}
                </div>
            </div>
        </div>""")

    empty_html = (
        f'<div style="color:{_C["muted"]};font-size:12px;text-align:center;padding:24px 0">'
        f'Waiting for {label} to act…</div>'
    ) if not rows else ""

    return f"""
    <div style="
        font-family:'Inter','Segoe UI',sans-serif;
        background:{_C['surface']};
        border:1px solid {_C['border']};
        border-top:3px solid {color};
        border-radius:8px;
        padding:14px;
        height:340px;
        display:flex;
        flex-direction:column;
    ">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;
                    border-bottom:1px solid {_C['border']};padding-bottom:10px;flex-shrink:0">
            <span style="font-size:22px">{icon}</span>
            <div>
                <div style="color:{color};font-weight:700;font-size:14px">{label}</div>
                <div style="color:{_C['muted']};font-size:11px">{role}</div>
            </div>
            <div style="margin-left:auto;background:{color}22;border:1px solid {color}44;
                        border-radius:12px;padding:2px 10px">
                <span style="color:{color};font-size:11px;font-weight:600">{len(agent_events)} msg(s)</span>
            </div>
        </div>
        <div style="flex:1;overflow-y:auto;scrollbar-width:thin">
            {empty_html}{"".join(rows)}
        </div>
    </div>"""


def _theater_html(events: list[dict]) -> str:
    """Render all four agent boxes in a 2×2 grid."""
    doctor_box  = _agent_box_html("DOCTOR",        events)
    disp_box    = _agent_box_html("DISPENSER",     events)
    stock_box   = _agent_box_html("STOCK_MANAGER", events)
    gov_box     = _agent_box_html("GOV_OFFICIAL",  events)

    return f"""
    <div style="font-family:'Inter','Segoe UI',sans-serif">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
            {doctor_box}
            {disp_box}
            {stock_box}
        </div>
        <div>
            {gov_box}
        </div>
    </div>"""


def _system_log_html(events: list[dict]) -> str:
    """Slim system event ticker (phase transitions + SYSTEM messages)."""
    sys_events = [e for e in events if e.get("agent") == "SYSTEM"][-15:]
    if not sys_events:
        return f'<div style="color:{_C["muted"]};font-size:12px;text-align:center;padding:8px">No system events yet.</div>'

    rows = []
    for ev in reversed(sys_events):
        sc  = _STATUS_COLOR.get(ev.get("status", "info"), _C["info"])
        rows.append(
            f'<span style="color:{_C["muted"]};font-size:10px;font-family:monospace">{ev.get("ts","")}</span>'
            f'&nbsp;<span style="color:{sc};font-size:12px">{ev.get("speech","")}</span>'
        )

    items = "".join(f'<div style="padding:2px 0">{r}</div>' for r in rows)
    return f"""
    <div style="
        background:{_C['surface2']};border:1px solid {_C['border']};border-radius:6px;
        padding:10px 14px;font-family:monospace;max-height:120px;overflow-y:auto;
    ">{items}</div>"""


# ── Gradio tab builder ─────────────────────────────────────────────────────────

def build_simulate_tab() -> None:
    """Declare all Gradio components. Call inside an active gr.Blocks() / gr.Tab() context."""

    scenario_names = list(SCENARIOS.keys())

    gr.HTML(f"""
    <div style="
        font-family:'Inter','Segoe UI',sans-serif;
        padding:8px 0 16px 0;
    ">
        <h2 style="margin:0 0 6px 0;font-size:20px;color:{_C['text']}">🎬 Live Agent Theater</h2>
        <p style="margin:0;color:{_C['subtext']};font-size:13px">
            Select a scenario, click <b>Run Simulation</b>, then watch the agents converse in real time.
            All dialogue is generated live by GPT-4o-mini. The simulation writes real audit records —
            refresh any other tab to see them.
        </p>
    </div>""")

    # ── Controls ──────────────────────────────────────────────────────────────
    with gr.Row():
        scenario_dd = gr.Dropdown(
            choices=scenario_names,
            value="Anomaly",
            label="Scenario",
            scale=3,
        )
        run_btn   = gr.Button("▶ Run Simulation", variant="primary",   scale=1)
        stop_btn  = gr.Button("⏹ Clear / Reset",  variant="secondary", scale=1)
        close_btn = gr.Button("⏻ Close Application", variant="stop",   scale=1)

    close_status = gr.HTML(value="")

    # ── Scenario descriptions ─────────────────────────────────────────────────
    scenario_desc = gr.Markdown(value=_scenario_description("Anomaly"))

    # ── Status bar ────────────────────────────────────────────────────────────
    status_html = gr.HTML(value="")

    # ── Theater ───────────────────────────────────────────────────────────────
    theater_html = gr.HTML(value=_theater_html([]))

    # ── System log ───────────────────────────────────────────────────────────
    gr.Markdown("**System events**", elem_id="sim-syslog-label")
    syslog_html = gr.HTML(value=_system_log_html([]))

    # ── State ─────────────────────────────────────────────────────────────────
    run_id_state   = gr.State(value="")
    polling_state  = gr.State(value=False)   # True while active run
    sim_timer      = gr.Timer(value=1.5)     # polls during active run

    # ── Event handlers ────────────────────────────────────────────────────────

    def on_scenario_change(scenario: str):
        return _scenario_description(scenario)

    def on_run(scenario: str):
        data = _post(_api("/api/simulate"), {"scenario": scenario})
        if not data:
            return (
                "<p style='color:#ef4444'>Could not reach API. Is the backend running?</p>",
                _theater_html([]),
                _system_log_html([]),
                "", True,
            )
        run_id = data.get("run_id", "")
        status = _status_bar_html("starting", scenario, run_id, 0)
        return status, _theater_html([]), _system_log_html([]), run_id, True

    def on_poll(run_id: str, polling: bool):
        if not run_id or not polling:
            return gr.update(), gr.update(), gr.update(), run_id, polling

        feed = _get_feed(run_id)
        if feed is None:
            return gr.update(), gr.update(), gr.update(), run_id, polling

        events   = feed.get("events", [])
        sim_stat = feed.get("status", "running")
        scenario = feed.get("scenario", "")

        # Filter out SYSTEM messages from the theater (they go in syslog only)
        theater_events = [e for e in events if e.get("agent") != "SYSTEM"]

        still_polling = sim_stat not in ("done", "error")

        return (
            _status_bar_html(sim_stat, scenario, run_id, len(events)),
            _theater_html(theater_events),
            _system_log_html(events),
            run_id,
            still_polling,
        )

    def on_stop():
        return "", _theater_html([]), _system_log_html([]), "", False

    def on_close():
        try:
            httpx.post(_api("/api/shutdown"), timeout=3)
        except Exception:
            pass
        return (
            "<div style=\""
            "background:#7f1d1d;color:#fca5a5;padding:10px 16px;border-radius:6px;"
            "font-weight:600;font-size:14px;text-align:center;margin-top:6px"
            "\">⏻ Shutdown signal sent — the application will close shortly. "
            "You can safely close this browser tab.</div>"
        )

    # Wire up
    scenario_dd.change(on_scenario_change, inputs=[scenario_dd], outputs=[scenario_desc])

    run_btn.click(
        fn=on_run,
        inputs=[scenario_dd],
        outputs=[status_html, theater_html, syslog_html, run_id_state, polling_state],
    )

    stop_btn.click(
        fn=on_stop,
        outputs=[status_html, theater_html, syslog_html, run_id_state, polling_state],
    )

    close_btn.click(
        fn=on_close,
        outputs=[close_status],
    )

    sim_timer.tick(
        fn=on_poll,
        inputs=[run_id_state, polling_state],
        outputs=[status_html, theater_html, syslog_html, run_id_state, polling_state],
    )


# ── Scenario descriptions ──────────────────────────────────────────────────────

_DESCRIPTIONS: dict[str, str] = {
    "Full Day": (
        "**Full Day simulation** — mirrors the complete scripted day:\n"
        "3 stock receipts → 15 LLM-generated prescriptions → expiry scans → reorder sweep → "
        "Government audit. Includes the Tramadol anomaly by design."
    ),
    "Anomaly": (
        "**Anomaly scenario** — designed to trigger the controlled-drug anomaly detector:\n"
        "Routine prescriptions build a baseline, then Tramadol stock is drained by a large Rx. "
        "A second Tramadol Rx fails (out of stock) → orphan prescription → Inspector Hope Ogbons flags it."
    ),
    "Limited Stock": (
        "**Limited Stock scenario** — high-demand day stress-test:\n"
        "5 high-volume prescriptions targeting drugs near their reorder thresholds. "
        "Stock Manager raises reorder alerts. Gov Official runs compliance check."
    ),
    "Expiry Alert": (
        "**Expiry Alert scenario** — near-expiry batch management:\n"
        "Near-expiry stock receipts are processed, then the Stock Manager scans all batches, "
        "quarantines expired ones, and flags reorder needs."
    ),
    "Random": (
        "**Random scenario** — fully LLM-driven:\n"
        "GPT-4o-mini generates 4 realistic patient cases from the drug catalog. "
        "Dr. Freeman Malambo prescribes each one via the real DoctorAgent. Dispenser fulfils. "
        "Stock Manager checks levels. Gov Official audits."
    ),
}


def _scenario_description(scenario: str) -> str:
    return _DESCRIPTIONS.get(scenario, f"**{scenario}**")
