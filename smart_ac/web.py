#!/usr/bin/env python3
"""
smart_ac web UI -- HTTP dashboard on pi-sf.

Serves at http://pi.example.local:5010/

Pages:
  /              -- Dashboard: current scheduler status + quick links
  /reports       -- List all daily retrospective reports
  /reports/<d>   -- Render one specific report as pre-formatted text
  /decisions     -- Live tail of the last N decision-log JSON records
  /calibrate     -- Show last calibration result + button to launch a new run
  /status.json   -- JSON API for anyone else who wants to consume

POST endpoints:
  /calibrate     -- Spawns calibrate.py in background; redirects back to /calibrate

Read-only for everything else; assumes LAN access is trust boundary
(same posture as HA's own dashboard).

Runs as systemd service smart-ac-web on pi-sf.
"""

from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
DECISIONS_LOG = pathlib.Path("/home/chris/smart_ac/decisions.log")
REPORTS_DIR = pathlib.Path("/home/chris/smart_ac/reports")
CALIBRATE_STDOUT = pathlib.Path("/home/chris/smart_ac/calibrate.log")
SCHEDULER_STATE = pathlib.Path("/home/chris/smart_ac_state.json")

PORT = 5010

# DAB pump entity slug. See create_telegram_pump_command.py for how to find
# yours; override via PUMP_SLUG env at systemd EnvironmentFile.
PUMP_SLUG = os.environ.get("PUMP_SLUG", "esyminiv2_rhjl6")


def _pump_e(kind: str, suffix: str) -> str:
    return f"{kind}.{PUMP_SLUG}_{suffix}"

# ---------------------------------------------------------------------- config


def load_config() -> dict:
    p = pathlib.Path(os.environ.get("SMART_AC_CONFIG", HERE / "smart_ac.json"))
    return json.loads(p.read_text())


CFG = load_config()


def ha_get(path: str) -> object | None:
    try:
        req = urllib.request.Request(
            f"{CFG['ha_url'].rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"# ha_get {path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------- HTML

STYLE = """
<style>
  :root {
    --bg: #0f1116;
    --card: #171a22;
    --line: #23283a;
    --text: #dde3ec;
    --dim: #8592a8;
    --accent: #56a0ff;
    --accent-2: #7ce38b;
    --warn: #ffb454;
    --err: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "SF Pro Text", "Segoe UI", monospace;
  }
  header {
    background: var(--card); padding: 12px 20px;
    border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 20px;
  }
  header a {
    color: var(--accent); text-decoration: none;
    padding: 5px 10px; border-radius: 4px;
  }
  header a:hover { background: var(--line); }
  header .title { font-size: 18px; font-weight: bold; color: var(--text); }
  main { max-width: 1100px; margin: 0 auto; padding: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    padding: 15px 20px; margin-bottom: 15px; border-radius: 6px;
  }
  h1, h2, h3 { color: var(--accent); margin-top: 0; }
  h1 { font-size: 22px; }
  h2 { font-size: 18px; }
  h3 { font-size: 16px; }
  .mode {
    display: inline-block; padding: 4px 12px;
    border-radius: 4px; font-weight: bold;
    background: var(--line);
  }
  .mode-SURPLUS { background: var(--accent-2); color: #000; }
  .mode-ON_TRACK { background: var(--accent); color: #000; }
  .mode-EVENING { background: var(--warn); color: #000; }
  .mode-NIGHT { background: #445; }
  .mode-DEFICIT, .mode-CHARGE_BEHIND { background: var(--err); color: #fff; }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: normal; text-transform: uppercase; font-size: 11px; }
  pre {
    background: #000; padding: 12px; overflow-x: auto;
    font-size: 12px; border-radius: 4px; border: 1px solid var(--line);
  }
  .kv { display: grid; grid-template-columns: 140px 1fr; gap: 4px 20px; }
  .kv div:nth-child(odd) { color: var(--dim); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
  @media (max-width: 700px) { .grid2 { grid-template-columns: 1fr; } }
  button, .btn {
    background: var(--accent); color: #000; border: 0;
    padding: 8px 16px; border-radius: 4px; font-weight: bold;
    cursor: pointer; font-family: inherit; font-size: 14px;
    text-decoration: none; display: inline-block;
  }
  button:hover, .btn:hover { opacity: 0.85; }
  .subtle { color: var(--dim); font-size: 12px; }
  .status-good { color: var(--accent-2); }
  .status-warn { color: var(--warn); }
  .status-err { color: var(--err); }
  a { color: var(--accent); }
</style>
"""


def page(title: str, body: str, refresh_sec: int | None = None) -> bytes:
    refresh = f'<meta http-equiv="refresh" content="{refresh_sec}">' if refresh_sec else ""
    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — smart_ac</title>
{refresh}
{STYLE}
</head>
<body>
<header>
  <span class="title">smart_ac</span>
  <a href="/">Dashboard</a>
  <a href="/reports">Reports</a>
  <a href="/decisions">Decisions</a>
  <a href="/observations">Observations</a>
  <a href="/overrides">Overrides</a>
  <a href="/calibrate">Calibrate</a>
  <a href="/water">Water</a>
  <a href="/status.json">JSON</a>
</header>
<main>
{body}
</main>
</body></html>"""
    return html.encode("utf-8")


# ---------------------------------------------------------------------- views


def view_dashboard() -> bytes:
    s = ha_get(f"/api/states/{CFG['status_sensor_entity']}")
    if not s:
        return page("Dashboard", '<div class="card">No status available. Is smart-ac running?</div>')
    a = s.get("attributes", {})
    mode = a.get("mode", "?")
    target_on = a.get("target_on") or []
    target_off = a.get("target_off") or []
    indoor = a.get("indoor_f") or {}
    reasons = a.get("reasons") or {}
    actions = a.get("actions_this_tick") or []
    last_iso = a.get("last_decision_at", "?")
    try:
        last = dt.datetime.fromisoformat(last_iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        last = last_iso

    rows_reason = "".join(
        f"<tr><td>{r}</td><td>{reasons[r]}</td></tr>"
        for r in sorted(reasons.keys())
    )
    indoor_txt = " · ".join(f"{k} {v}°F" for k, v in indoor.items())
    actions_txt = "<br>".join(actions) if actions else "(none)"

    body = f"""
<div class="card">
  <h1>Now: <span class="mode mode-{mode}">{mode}</span></h1>
  <div class="grid2">
    <div class="kv">
      <div>SoC</div><div>{a.get('soc')}%</div>
      <div>Battery</div><div>{a.get('battery_power_w')} W</div>
      <div>Solar</div><div>{a.get('pv_power_w')} W</div>
      <div>Load</div><div>{a.get('load_w')} W</div>
      <div>Outdoor</div><div>{a.get('outdoor_f')}°F</div>
      <div>Indoor</div><div>{indoor_txt}</div>
    </div>
    <div class="kv">
      <div>Target ON</div><div class="status-good">{', '.join(target_on) or '(none)'}</div>
      <div>Target OFF</div><div class="status-warn">{', '.join(target_off) or '(none)'}</div>
      <div>Enabled</div><div>{a.get('enabled')}</div>
      <div>Notify TG</div><div>{a.get('notify_telegram')}</div>
      <div>Unoccupied</div><div>{a.get('unoccupied')}</div>
      <div>Last eval</div><div class="subtle">{last}</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Per-room reasoning</h2>
  <table><tr><th>Room</th><th>Reason</th></tr>{rows_reason}</table>
</div>

<div class="card">
  <h2>Actions this tick</h2>
  <div>{actions_txt}</div>
</div>
"""
    return page("Dashboard", body, refresh_sec=60)


def view_reports_list() -> bytes:
    files = sorted(REPORTS_DIR.glob("*.md"), reverse=True) if REPORTS_DIR.exists() else []
    if not files:
        rows = "<tr><td>(no reports yet — run <code>python3 retrospective.py</code>)</td></tr>"
    else:
        rows = "".join(
            f'<tr><td><a href="/reports/{f.stem}">{f.stem}</a></td>'
            f'<td class="subtle">{f.stat().st_size} bytes</td></tr>'
            for f in files
        )
    body = f"""
<div class="card">
  <h1>Daily reports</h1>
  <p class="subtle">Nightly retrospective analysis, one per day. Each report includes SoC trajectory, mode timing, per-AC runtime, and per-AC estimated draw.</p>
  <table><tr><th>Date</th><th></th></tr>{rows}</table>
</div>
"""
    return page("Reports", body)


def _md_inline(s: str) -> str:
    """Inline markdown: **bold**, `code`. HTML-escape everything else."""
    s = html.escape(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def render_markdown(md: str) -> str:
    """Minimal markdown->HTML renderer covering the constructs our reports
    use: # / ## / ### headers, - bullet lists, **bold**, `code`, and
    | pipe | tables |. No nested lists, no raw HTML pass-through.
    Everything else becomes a paragraph."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        if not stripped:
            i += 1
            continue

        # Headers
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # Table: line starts with |, plus a separator line beneath
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|[-:| ]+\|$", lines[i + 1].strip()):
            header_cells = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2  # skip header + separator
            rows_html = []
            while i < n and lines[i].strip().startswith("|"):
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows_html.append(
                    "<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in row) + "</tr>"
                )
                i += 1
            head_html = "<tr>" + "".join(
                f"<th>{_md_inline(c)}</th>" for c in header_cells
            ) + "</tr>"
            out.append(f"<table>{head_html}{''.join(rows_html)}</table>")
            continue

        # Bullet list
        if stripped.startswith("- "):
            items = []
            while i < n and lines[i].lstrip().startswith("- "):
                items.append(f"<li>{_md_inline(lines[i].lstrip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        # Paragraph: collect until blank line or a special-block start
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].rstrip()
            if not nxt:
                break
            if re.match(r"^(#{1,6}\s|\||-\s)", nxt.lstrip()):
                break
            para_lines.append(nxt)
            i += 1
        out.append("<p>" + _md_inline(" ".join(para_lines)) + "</p>")

    return "\n".join(out)


def view_report(name: str) -> bytes:
    # Basic sanitisation: only allow YYYY-MM-DD.md-ish names.
    if not all(c.isalnum() or c in "-_" for c in name):
        return page("Report", '<div class="card">Invalid report name.</div>')
    p = REPORTS_DIR / f"{name}.md"
    if not p.is_file():
        return page("Report", f'<div class="card">Report {name} not found.</div>')
    rendered = render_markdown(p.read_text())
    body = f"""
<div class="card">
  <div class="subtle" style="margin-bottom:12px">
    <a href="/reports">← Back to reports</a> ·
    Source: <code>{p}</code>
  </div>
  {rendered}
</div>
"""
    return page(f"Report {name}", body)


def _fmt_local_time(iso: str) -> str:
    """ISO UTC timestamp -> local 'HH:MM:SS' string."""
    try:
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")
    except Exception:
        return iso[11:19] if len(iso) > 19 else iso


def view_decisions() -> bytes:
    n = 30
    rows = ""
    if DECISIONS_LOG.exists():
        lines = DECISIONS_LOG.read_text().strip().splitlines()[-n:]
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts_local = _fmt_local_time(rec.get("ts", ""))
            mode = rec.get("mode", "?")
            targets = ", ".join(rec.get("target_on", []))
            acts = rec.get("actions") or []
            reasons = rec.get("reasons") or {}
            # Add the reason next to each action so the decisions view answers
            # "why" for each turn_on/turn_off in one glance.
            act_lines = []
            for a in acts:
                # a is "room:turn_on" or "room:turn_off"
                if ":" in a:
                    room, svc = a.split(":", 1)
                    act_lines.append(
                        f"{svc.replace('turn_','').upper()} {room} "
                        f"<span class='subtle'>({reasons.get(room, '?')})</span>"
                    )
                else:
                    act_lines.append(a)
            act_txt = "<br>".join(act_lines) if act_lines else '<span class="subtle">–</span>'
            rows += (
                f'<tr>'
                f'<td class="subtle">{ts_local}</td>'
                f'<td><span class="mode mode-{mode}">{mode}</span></td>'
                f'<td>{rec.get("soc","?")}%</td>'
                f'<td>{rec.get("battery_power_w","?")} W</td>'
                f'<td>{rec.get("pv_power_w","?")} W</td>'
                f'<td>{targets}</td>'
                f'<td>{act_txt}</td>'
                f'</tr>'
            )
    if not rows:
        rows = '<tr><td colspan="7" class="subtle">(no decisions logged yet)</td></tr>'
    body = f"""
<div class="card">
  <h1>Recent decisions</h1>
  <p class="subtle">Last 30 evaluation ticks (newest first). Times shown local. Actions include the reason. Auto-refresh every 60s.</p>
  <table>
    <tr><th>Time</th><th>Mode</th><th>SoC</th><th>Battery</th><th>Solar</th><th>Target ON</th><th>Actions (with reason)</th></tr>
    {rows}
  </table>
</div>
"""
    return page("Decisions", body, refresh_sec=60)


def view_calibrate() -> bytes:
    s = ha_get("/api/states/sensor.smart_ac_calibration")
    running = _is_calibration_running()
    if not s or s.get("state") == "unknown":
        result_body = '<p class="subtle">No calibration has been run yet.</p>'
    else:
        a = s.get("attributes", {})
        results = a.get("results", {})
        rows = ""
        for r, info in results.items():
            if "error" in info:
                rows += f'<tr><td>{r}</td><td colspan="3" class="status-err">{info["error"]}</td></tr>'
            else:
                rows += (
                    f'<tr><td>{r}</td>'
                    f'<td>{info.get("baseline_w")} W</td>'
                    f'<td>{info.get("running_w")} W</td>'
                    f'<td class="status-good">{info.get("delta_w"):+} W</td></tr>'
                )
        result_body = f"""
<p class="subtle">Last run: {a.get('run_at','?')} (settle {a.get('settle_before_sec','?')}s / on {a.get('on_duration_sec','?')}s / recover {a.get('settle_after_sec','?')}s)</p>
<table>
  <tr><th>Room</th><th>Baseline</th><th>Running</th><th>Delta (est. draw)</th></tr>
  {rows}
</table>
"""

    if running:
        running_body = f"""
<div class="card">
  <h2>Calibration running…</h2>
  <p class="subtle">Started; check back in ~20 min. Progress log: <code>tail -F {CALIBRATE_STDOUT}</code></p>
  <pre id="log">{_tail(CALIBRATE_STDOUT, 20)}</pre>
</div>
"""
        refresh = 15
    else:
        running_body = f"""
<div class="card">
  <h2>Run a new calibration</h2>
  <p>Takes ~20 minutes. Turns each AC OFF-then-ON in sequence, samples mean load. Best run when the house is quiet.</p>
  <form method="POST" action="/calibrate">
    <button type="submit">Start calibration</button>
  </form>
</div>
"""
        refresh = None

    body = f"""
<div class="card">
  <h1>Per-AC calibration</h1>
  {result_body}
</div>
{running_body}
"""
    return page("Calibrate", body, refresh_sec=refresh)


def view_overrides() -> bytes:
    # Read from HA's input_datetime helpers (single source of truth).
    # Any value in the future = active override; anything past (including
    # the "cleared" 1970-01-01 or a small offset from now) = inactive.
    rooms = ["master", "guest", "dining", "living", "office", "kyle"]
    now = dt.datetime.now().astimezone()
    active_rows = ""
    for room in rooms:
        st = ha_get(f"/api/states/input_datetime.ac_{room}_override_until")
        if not st:
            continue
        raw = st.get("state", "")
        if raw in ("unknown", "unavailable", None, ""):
            continue
        try:
            # input_datetime state is naive local ("YYYY-MM-DD HH:MM:SS")
            until = dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").astimezone()
        except Exception:
            continue
        if until <= now:
            continue  # expired / cleared
        remaining = int((until - now).total_seconds() // 60)
        active_rows += (
            f'<tr><td>{room}</td>'
            f'<td>{until.strftime("%Y-%m-%d %H:%M")}</td>'
            f'<td>{remaining} min</td>'
            f'<td><button type="button" onclick="clearOverride(\'{room}\')">Clear</button></td></tr>'
        )
    if not active_rows:
        active_rows = '<tr><td colspan="4" class="subtle">(no active overrides)</td></tr>'

    body = f"""
<div class="card">
  <h1>Overrides</h1>
  <p class="subtle">Explicit pins on an AC's state until a specific time. Set via Telegram (<code>/override &lt;room&gt; until 22:00</code>) or the JSON endpoint.</p>
  <table>
    <tr><th>Room</th><th>Until (local)</th><th>Remaining</th><th></th></tr>
    {active_rows}
  </table>
</div>
<div class="card">
  <h2>Set a new override</h2>
  <form method="POST" action="/override" onsubmit="return submitOverride(this)">
    <label>Room:
      <select name="room">
        <option>master</option><option>guest</option><option>dining</option>
        <option>living</option><option>office</option><option>kyle</option>
      </select>
    </label>
    <label style="margin:0 12px">State:
      <select name="state">
        <option value="">(keep current)</option>
        <option value="on">ON</option>
        <option value="off">OFF</option>
      </select>
    </label>
    <label style="margin:0 12px">Until:
      <input type="text" name="until" placeholder="22:00 or +2h">
    </label>
    <button type="submit">Set</button>
  </form>
  <p class="subtle">If State is set, the AC is flipped to that state immediately (via Alexa routine) AND pinned until the "Until" time. If State is "(keep current)", scheduler is just prevented from touching that AC until the time.</p>
</div>
<script>
function submitOverride(form) {{
  const room = form.room.value;
  const state = form.state.value;
  const until = form.until.value;
  const body = {{room, until}};
  if (state) body.state = state;
  fetch('/override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }}).then(r => r.json()).then(j => {{
    if (j.ok) location.reload();
    else alert('Error: ' + (j.error || 'unknown'));
  }});
  return false;
}}
function clearOverride(room) {{
  fetch('/override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{room, clear: true}}),
  }}).then(() => location.reload());
  return false;
}}
</script>
"""
    return page("Overrides", body, refresh_sec=60)


def _ha_history(entity_id: str, start_iso: str, end_iso: str) -> list[dict]:
    """Fetch HA history for one entity across [start, end).
    Returns a list of {last_changed, state} dicts, or [] on error."""
    path = (
        f"/api/history/period/{urllib.parse.quote(start_iso)}"
        f"?filter_entity_id={entity_id}"
        f"&end_time={urllib.parse.quote(end_iso)}"
    )
    data = ha_get(path)
    if not data or not isinstance(data, list) or not data:
        return []
    # HA history returns a list per entity id; we asked for one.
    series = data[0] if data else []
    out: list[dict] = []
    for pt in series:
        try:
            val = float(pt["state"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"t": pt["last_changed"], "v": val})
    return out


def _svg_line_chart(series: list[tuple[str, list[dict], str]], title: str,
                    width: int = 800, height: int = 240) -> str:
    """Render one or more time-series as an SVG line chart.
    series: list of (label, points, color) where points is list of
    {"t": iso, "v": float}.

    All series share the same X axis (time span). Each has its own Y
    autoscale, drawn as a light line + label."""
    pad_l, pad_r, pad_t, pad_b = 50, 20, 30, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    # Compute time span across all series
    all_times: list[dt.datetime] = []
    for _, pts, _ in series:
        for p in pts:
            try:
                all_times.append(dt.datetime.fromisoformat(p["t"].replace("Z", "+00:00")))
            except Exception:
                pass
    if not all_times:
        return f'<div class="card"><h3>{html.escape(title)}</h3><p class="subtle">(no data)</p></div>'
    t_min, t_max = min(all_times), max(all_times)
    span_s = max((t_max - t_min).total_seconds(), 1)

    def x_for(ts: dt.datetime) -> float:
        return pad_l + plot_w * ((ts - t_min).total_seconds() / span_s)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{width}px">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#171a22"/>',
    ]

    # Draw each series
    legend_x = pad_l
    for idx, (label, pts, color) in enumerate(series):
        if not pts:
            continue
        vals = [p["v"] for p in pts]
        v_min, v_max = min(vals), max(vals)
        v_min = min(v_min, 0.0)
        if v_max - v_min < 1e-6:
            v_max = v_min + 1.0

        def y_for(v: float, vmin=v_min, vmax=v_max) -> float:
            return pad_t + plot_h - plot_h * ((v - vmin) / (vmax - vmin))

        path_pts: list[str] = []
        for p in pts:
            try:
                ts = dt.datetime.fromisoformat(p["t"].replace("Z", "+00:00"))
            except Exception:
                continue
            path_pts.append(f"{x_for(ts):.1f},{y_for(p['v']):.1f}")
        if path_pts:
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
                f'points="{" ".join(path_pts)}"/>'
            )
        # Legend entry (top-left)
        parts.append(
            f'<circle cx="{legend_x + 6}" cy="16" r="4" fill="{color}"/>'
            f'<text x="{legend_x + 14}" y="20" fill="#dde3ec" font-size="12">'
            f'{html.escape(label)} (max {v_max:.1f})</text>'
        )
        legend_x += 180

    # X axis: 4 hourly labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = pad_l + plot_w * frac
        t = t_min + dt.timedelta(seconds=span_s * frac)
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t + plot_h}" x2="{x:.1f}" '
            f'y2="{pad_t + plot_h + 4}" stroke="#8592a8"/>'
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 18}" fill="#8592a8" '
            f'font-size="11" text-anchor="middle">{t.strftime("%H:%M")}</text>'
        )
    # Chart baseline
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" '
        f'y2="{pad_t + plot_h}" stroke="#23283a"/>'
    )
    parts.append("</svg>")
    return (
        f'<div class="card"><h3>{html.escape(title)}</h3>'
        + "".join(parts) + "</div>"
    )


def _svg_bar_chart(labels: list[str], values: list[float], title: str,
                   width: int = 800, height: int = 220, unit: str = "") -> str:
    """Render a vertical bar chart for multi-day totals."""
    if not values:
        return f'<div class="card"><h3>{html.escape(title)}</h3><p class="subtle">(no data)</p></div>'
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    v_max = max(values) or 1.0
    bar_w = plot_w / max(len(values), 1)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{width}px">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#171a22"/>',
    ]
    for i, (lab, v) in enumerate(zip(labels, values)):
        h = plot_h * (v / v_max)
        x = pad_l + i * bar_w + 4
        y = pad_t + plot_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 8:.1f}" '
            f'height="{h:.1f}" fill="#56a0ff"/>'
        )
        # value label
        parts.append(
            f'<text x="{x + (bar_w - 8) / 2:.1f}" y="{y - 4:.1f}" fill="#dde3ec" '
            f'font-size="11" text-anchor="middle">{v:.0f}</text>'
        )
        # x label
        parts.append(
            f'<text x="{x + (bar_w - 8) / 2:.1f}" y="{pad_t + plot_h + 16:.1f}" '
            f'fill="#8592a8" font-size="11" text-anchor="middle">'
            f'{html.escape(lab)}</text>'
        )
    parts.append(
        f'<text x="{pad_l - 8}" y="{pad_t + 12}" fill="#8592a8" '
        f'font-size="11" text-anchor="end">{v_max:.0f}{unit}</text>'
    )
    parts.append("</svg>")
    return (
        f'<div class="card"><h3>{html.escape(title)}</h3>'
        + "".join(parts) + "</div>"
    )


def view_water(query_params: dict) -> bytes:
    """Per-day water pump view with prev/next pagination."""
    today = dt.date.today()
    try:
        date_str = query_params.get("date", [today.isoformat()])[0]
        day = dt.date.fromisoformat(date_str)
    except Exception:
        day = today
    prev_day = day - dt.timedelta(days=1)
    next_day = day + dt.timedelta(days=1)
    is_today = (day == today)

    # Local-day boundaries
    start = dt.datetime.combine(day, dt.time.min).astimezone()
    end = dt.datetime.combine(day, dt.time.max).astimezone()

    pressure = _ha_history(_pump_e("sensor", "vp_pressurepsi"),
                           start.isoformat(), end.isoformat())
    flow = _ha_history(_pump_e("sensor", "vf_flowgall"),
                       start.isoformat(), end.isoformat())
    power = _ha_history(_pump_e("sensor", "po_outputpower"),
                        start.isoformat(), end.isoformat())

    # Live current stats
    stat_map = {
        "Status": ha_get(f"/api/states/{_pump_e('sensor', 'pumpstatus')}"),
        "System": ha_get(f"/api/states/{_pump_e('sensor', 'systemstatus')}"),
        "Errors": ha_get(f"/api/states/{_pump_e('sensor', 'faultpumpsnumber')}"),
    }
    stat_rows = "".join(
        f'<tr><td>{k}</td><td>{s["state"] if s else "?"}</td></tr>'
        for k, s in stat_map.items()
    )

    # 7-day usage bar chart. Compute daily totals from
    # actual_period_flow_counter_gall's history: since it's monthly-reset,
    # simpler to sum flow rate. But rate is instantaneous, not integrated.
    # As a shortcut: fetch fct_total_delivered_flow_gall (cumulative) daily.
    labels: list[str] = []
    daily_flow: list[float] = []
    daily_kwh: list[float] = []
    for offset in range(6, -1, -1):
        d = today - dt.timedelta(days=offset)
        d_start = dt.datetime.combine(d, dt.time.min).astimezone()
        d_end = dt.datetime.combine(d, dt.time.max).astimezone()
        flow_hist = _ha_history(_pump_e("sensor", "fct_total_delivered_flow_gall"),
                                d_start.isoformat(), d_end.isoformat())
        kwh_hist = _ha_history(_pump_e("sensor", "totalenergy"),
                               d_start.isoformat(), d_end.isoformat())
        if flow_hist:
            daily_flow.append(max(0.0, flow_hist[-1]["v"] - flow_hist[0]["v"]))
        else:
            daily_flow.append(0.0)
        if kwh_hist:
            daily_kwh.append(max(0.0, kwh_hist[-1]["v"] - kwh_hist[0]["v"]))
        else:
            daily_kwh.append(0.0)
        labels.append(d.strftime("%a"))

    charts = ""
    charts += _svg_line_chart(
        [("Pressure (psi)", pressure, "#7ce38b")],
        f"Pressure — {day.isoformat()}",
    )
    charts += _svg_line_chart(
        [("Flow (gal/min)", flow, "#56a0ff"),
         ("Power (W)", power, "#ffb454")],
        f"Flow + Power — {day.isoformat()}",
    )

    nav = (
        f'<a href="/water?date={prev_day.isoformat()}">◀ {prev_day.isoformat()}</a>'
        f'&nbsp;&nbsp; <strong>{day.isoformat()}{" (today)" if is_today else ""}</strong>'
        f'&nbsp;&nbsp; '
        + (f'<a href="/water?date={next_day.isoformat()}">{next_day.isoformat()} ▶</a>'
           if not is_today else '<span class="subtle">tomorrow</span>')
        + f'&nbsp;&nbsp; <a href="/water">jump to today</a>'
    )

    body = f"""
<div class="card">
  <h1>Water pump — {day.isoformat()}</h1>
  <p>{nav}</p>
  <table><tr><th>Live</th><th></th></tr>{stat_rows}</table>
</div>
{charts}
{_svg_bar_chart(labels, daily_flow, "Daily flow (gal) — last 7 days", unit=" gal")}
{_svg_bar_chart(labels, daily_kwh, "Daily energy (kWh) — last 7 days", unit=" kWh")}
"""
    return page(f"Water {day.isoformat()}", body,
                refresh_sec=60 if is_today else None)


def view_observations() -> bytes:
    """Recent observations + calibration drift + room dynamics.
    All backed by the smart_ac SQLite stats DB. If the DB is missing or
    empty (fresh install), renders a hint instead."""
    try:
        from . import stats as _stats  # type: ignore
    except (ImportError, ValueError):
        try:
            import stats as _stats  # type: ignore
        except ImportError:
            _stats = None  # type: ignore
    if _stats is None:
        return page(
            "Observations",
            '<div class="card"><h1>Observations</h1>'
            '<p class="subtle">stats.py module not available in this deploy.</p>'
            '</div>',
        )
    try:
        with _stats.opened() as conn:
            today_summary = _stats.daily_action_summary(conn)
            drift = _stats.drift_by_room(conn)
            dynamics = _stats.warm_up_rates(conn)
            rolling = _stats.rolling_stats_per_room_action(conn, n_samples=20)
            rolling_summary = _stats.rolling_stats_summary(conn, n_samples=100)
            rows = _stats.recent_observations(conn, 100)
            total_obs = conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
    except Exception as e:
        return page(
            "Observations",
            f'<div class="card"><h1>Observations</h1>'
            f'<p class="status-err">DB query failed: {html.escape(str(e))}</p></div>',
        )

    def _cls(x: float, warn: float, err: float) -> str:
        if abs(x) >= err:
            return "status-err"
        if abs(x) >= warn:
            return "status-warn"
        return "status-good"

    # Today summary card
    off = today_summary.get("turn_off", {})
    on = today_summary.get("turn_on", {})
    summary_body = (
        f'<p>Today ({today_summary.get("day","?")}): '
        f'<strong>{off.get("n",0)}</strong> turn-offs saved '
        f'<strong>{off.get("total_saved_w",0)} W</strong> total '
        f'(avg {off.get("avg_saved_w",0)} W). '
        f'<strong>{on.get("n",0)}</strong> turn-ons added '
        f'<strong>{on.get("total_added_w",0)} W</strong> total '
        f'(avg {on.get("avg_added_w",0)} W). '
        f'Net load delta: <strong>{today_summary.get("net_load_delta_w",0)} W</strong>.</p>'
    )

    drift_rows = ""
    for d in drift:
        flag = "⚠" if d["drift_flag"] else ""
        css = _cls(d["drift_pct"], 15, 30)
        drift_rows += (
            f"<tr><td>{d['room']}</td><td>{d['action']}</td><td>{d['n']}</td>"
            f"<td>{d['avg_actual_w']:+d} W</td><td>{d['avg_expected_w']:+d} W</td>"
            f"<td>{d['avg_diff_w']:+d} W</td>"
            f"<td class='{css}'>{d['drift_pct']:.1f}%</td><td>{flag}</td></tr>"
        )
    if not drift_rows:
        drift_rows = "<tr><td colspan='8' class='subtle'>Not enough data yet (need ≥3 observations per room/action).</td></tr>"

    dynamics_rows = ""
    for d in dynamics:
        dynamics_rows += (
            f"<tr><td>{d['room']}</td><td>{d['samples']}</td>"
            f"<td>{d['f_per_min']:.3f} °F/min</td>"
            f"<td>{d['avg_outdoor_f']:.1f} °F</td></tr>"
        )
    if not dynamics_rows:
        dynamics_rows = "<tr><td colspan='4' class='subtle'>Not enough post-turn-off samples yet.</td></tr>"

    # Rolling stats per (room, action). Median filters fridge/pump noise.
    rolling_rows = ""
    for x in rolling:
        act = x["actual"]
        dif = x["diff"]
        css = _cls(dif.get("median", 0), 200, 500)
        rolling_rows += (
            f"<tr><td>{html.escape(x['room'])}</td>"
            f"<td>{html.escape(x['action'])}</td>"
            f"<td>{x['n_samples']}</td>"
            f"<td>{act.get('median', 0):+d}</td>"
            f"<td>{act.get('mean', 0):+d}</td>"
            f"<td>{act.get('stddev', 0)}</td>"
            f"<td>{act.get('min', 0):+d}</td>"
            f"<td>{act.get('max', 0):+d}</td>"
            f"<td class='{css}'>{dif.get('median', 0):+d}</td>"
            f"<td>{dif.get('stddev', 0)}</td></tr>"
        )
    if not rolling_rows:
        rolling_rows = "<tr><td colspan='10' class='subtle'>Not enough data yet.</td></tr>"

    # Cross-room summary: overall noise floor across last 100 obs.
    r_all = rolling_summary
    if r_all.get("n", 0) > 0:
        act_all = r_all["actual"]
        dif_all = r_all["diff"]
        rolling_summary_html = (
            f"<p>Across last {r_all['n']} isolated observations "
            f"(<code>n_actions=1</code>): "
            f"actual median <strong>{act_all['median']:+d} W</strong> "
            f"(stddev {act_all['stddev']}, range {act_all['min']:+d} to {act_all['max']:+d}). "
            f"Diff vs expected: median <strong>{dif_all['median']:+d} W</strong> "
            f"(stddev {dif_all['stddev']}, range {dif_all['min']:+d} to {dif_all['max']:+d}). "
            f"A low |median diff| and stddev under ~300 W means calibration is holding "
            f"against fridge/pump noise; wide stddev = high measurement uncertainty.</p>"
        )
    else:
        rolling_summary_html = (
            '<p class="subtle">Not enough isolated observations yet.</p>'
        )

    obs_rows = ""
    for r in rows:
        try:
            ts = dt.datetime.fromisoformat(
                str(r["ts_observed"]).replace("Z", "+00:00")
            ).astimezone().strftime("%m-%d %H:%M")
        except Exception:
            ts = str(r["ts_observed"])
        diff_w = int(r["delta_vs_expected_w"] or 0)
        css = _cls(diff_w, 300, 700)
        obs_rows += (
            f"<tr><td>{ts}</td>"
            f"<td>{html.escape(r['primary_action'])}</td>"
            f"<td>{html.escape(r['primary_room'])}</td>"
            f"<td>{html.escape(r['mode_at_action'] or '?')}</td>"
            f"<td>{r['before_load_w']} W</td>"
            f"<td>{r['after_load_w']} W</td>"
            f"<td>{r['delta_load_w']:+d} W</td>"
            f"<td>{r['expected_delta_w']:+d} W</td>"
            f"<td class='{css}'>{diff_w:+d} W</td>"
            f"<td>{html.escape(r['primary_reason'] or '')[:80]}</td></tr>"
        )
    if not obs_rows:
        obs_rows = "<tr><td colspan='10' class='subtle'>No observations yet. Wait for the next tick that fires actions.</td></tr>"

    body = f"""
<div class="card">
  <h1>Observations</h1>
  <p class="subtle">{total_obs} observations in DB. Each row is the actual vs
  calibration-expected effect of one AC action, measured ~5 min after
  the scheduler flipped it.</p>
  {summary_body}
</div>

<div class="card">
  <h2>Rolling stats (last 20 obs per room+action)</h2>
  <p class="subtle">Robust to fridge / water-pump noise: the median column
  is much less affected by one-off spikes than the mean. High stddev
  means "we can't pin this room's calibration confidently yet"; it does
  not necessarily mean drift. Only isolated (single-AC transition)
  observations are included here.</p>
  {rolling_summary_html}
  <table>
    <tr><th>Room</th><th>Action</th><th>n</th>
      <th>Actual median</th><th>Actual mean</th><th>Actual stddev</th>
      <th>Actual min</th><th>Actual max</th>
      <th>Diff median</th><th>Diff stddev</th></tr>
    {rolling_rows}
  </table>
</div>

<div class="card">
  <h2>Calibration drift (last 14 days)</h2>
  <p class="subtle">Rooms whose actual load delta materially differs from
  calibration. If a row is flagged (⚠), consider re-running calibrate.py.</p>
  <table>
    <tr><th>Room</th><th>Action</th><th>n</th><th>Actual</th><th>Expected</th>
      <th>Diff</th><th>Drift %</th><th></th></tr>
    {drift_rows}
  </table>
</div>

<div class="card">
  <h2>Room warm-up dynamics</h2>
  <p class="subtle">°F per minute a room warms up after the AC is turned off.
  Useful for tuning precool windows (fast-warming rooms need earlier precool).</p>
  <table>
    <tr><th>Room</th><th>Samples</th><th>Warm-up rate</th><th>Avg outdoor</th></tr>
    {dynamics_rows}
  </table>
</div>

<div class="card">
  <h2>Recent observations (last 100)</h2>
  <table>
    <tr><th>Time</th><th>Action</th><th>Room</th><th>Mode</th>
      <th>Before</th><th>After</th><th>Δ</th><th>Expected</th>
      <th>vs Expected</th><th>Reason</th></tr>
    {obs_rows}
  </table>
</div>
"""
    return page("Observations", body, refresh_sec=90)


def view_status_json() -> bytes:
    s = ha_get(f"/api/states/{CFG['status_sensor_entity']}")
    retro = ha_get("/api/states/sensor.smart_ac_retrospective")
    calib = ha_get("/api/states/sensor.smart_ac_calibration")
    return json.dumps({
        "status": s,
        "retrospective": retro,
        "calibration": calib,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }, default=str).encode()


# ---------------------------------------------------------------------- helpers


def _is_calibration_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", "calibrate.py"],
                                capture_output=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False


def _tail(path: pathlib.Path, n: int) -> str:
    if not path.exists():
        return "(no log yet)"
    try:
        lines = path.read_text().strip().splitlines()[-n:]
        return "\n".join(lines).replace("<", "&lt;")
    except Exception as e:
        return f"(error reading log: {e})"


def start_calibration_in_background() -> None:
    """Launch calibrate.py as a detached subprocess so it survives past
    this HTTP request. Logs stdout+stderr to /home/chris/smart_ac/calibrate.log.

    IMPORTANT: forces PYTHONUNBUFFERED=1 so Python's print() line-flushes
    into the log file instead of block-buffering. Without this, the log
    stays empty for many minutes (until the process exits or fills ~4KB)
    and the /calibrate page's tail shows nothing while the run is
    actually progressing."""
    if _is_calibration_running():
        return
    logf = open(CALIBRATE_STDOUT, "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    subprocess.Popen(
        [sys.executable, "-u", str(HERE / "calibrate.py")],
        cwd=str(HERE),
        env=env,
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------- overrides


def _load_scheduler_state() -> dict:
    if SCHEDULER_STATE.is_file():
        try:
            return json.loads(SCHEDULER_STATE.read_text())
        except Exception:
            return {}
    return {}


def _save_scheduler_state(state: dict) -> None:
    SCHEDULER_STATE.write_text(json.dumps(state, indent=2))


def _ha_call(domain: str, service: str, body: dict) -> None:
    """Fire-and-forget HA REST service call. Used to immediately flip an
    input_boolean when the override request explicitly asks for a state."""
    url = f"{CFG['ha_url'].rstrip('/')}/api/services/{domain}/{service}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def parse_override_until(spec: str) -> dt.datetime | None:
    """Turn a user-supplied time spec into an aware local datetime, or None
    if unparseable. Supported forms:
        HH:MM        -> today at HH:MM local (tomorrow if already past)
        +Nh          -> now + N hours
        +Nm          -> now + N minutes
        Nh           -> now + N hours (shorthand)
        Nm           -> now + N minutes (shorthand)
    """
    spec = spec.strip().lower()
    now_local = dt.datetime.now().astimezone()

    m = re.match(r"^([+])?(\d+)([hm])$", spec)
    if m:
        n = int(m.group(2))
        unit = m.group(3)
        delta = dt.timedelta(hours=n) if unit == "h" else dt.timedelta(minutes=n)
        return now_local + delta

    m = re.match(r"^(\d{1,2}):(\d{2})$", spec)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh < 24 and 0 <= mm < 60:
            target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now_local:
                target = target + dt.timedelta(days=1)
            return target

    return None


def apply_override(room: str, until: dt.datetime,
                   state_to_set: str | None = None) -> None:
    """Set input_datetime.ac_<room>_override_until via HA REST. HA is the
    single source of truth for overrides; smart_ac.py reads the same
    input_datetime helpers on each tick.

    If state_to_set is 'on' or 'off', ALSO immediately flip the matching
    input_boolean via HA REST so the AC responds now (rather than at the
    next tick)."""
    if state_to_set in ("on", "off"):
        try:
            _ha_call("input_boolean",
                     "turn_on" if state_to_set == "on" else "turn_off",
                     {"entity_id": f"input_boolean.ac_{room}"})
        except Exception as e:
            print(f"# ha turn_{state_to_set} failed for {room}: {e}",
                  file=sys.stderr)
    # Convert 'until' (already local tz-aware) to the "YYYY-MM-DD HH:MM:SS"
    # naive-local format input_datetime.set_datetime expects.
    dt_str = until.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _ha_call("input_datetime", "set_datetime", {
            "entity_id": f"input_datetime.ac_{room}_override_until",
            "datetime": dt_str,
        })
    except Exception as e:
        print(f"# ha set_datetime failed for {room}: {e}", file=sys.stderr)


def clear_override(room: str) -> None:
    # "Clear" = set to a past date (1970). smart_ac treats any past value
    # as expired and no-longer-in-effect.
    try:
        _ha_call("input_datetime", "set_datetime", {
            "entity_id": f"input_datetime.ac_{room}_override_until",
            "datetime": "1970-01-01 00:00:00",
        })
    except Exception as e:
        print(f"# ha clear failed for {room}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------- handler


class Handler(http.server.BaseHTTPRequestHandler):
    def _respond(self, code: int, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", ""):
                self._respond(200, view_dashboard())
            elif path == "/reports":
                self._respond(200, view_reports_list())
            elif path.startswith("/reports/"):
                self._respond(200, view_report(path[len("/reports/"):]))
            elif path == "/decisions":
                self._respond(200, view_decisions())
            elif path == "/calibrate":
                self._respond(200, view_calibrate())
            elif path == "/overrides":
                self._respond(200, view_overrides())
            elif path == "/water":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._respond(200, view_water(query))
            elif path == "/observations":
                self._respond(200, view_observations())
            elif path == "/status.json":
                self._respond(200, view_status_json(), content_type="application/json")
            elif path == "/healthz":
                self._respond(200, b"ok\n", content_type="text/plain")
            else:
                self.send_error(404)
        except Exception as e:
            self.send_error(500, str(e))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/calibrate":
                # Read + discard body if present.
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                start_calibration_in_background()
                self.send_response(303)
                self.send_header("Location", "/calibrate")
                self.end_headers()
                return
            if path == "/override":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(body)
                room = payload.get("room", "").strip().lower()
                if not room:
                    self._respond(400, b'{"error":"room required"}', "application/json")
                    return
                if payload.get("clear"):
                    clear_override(room)
                    self._respond(200,
                                  json.dumps({"ok": True, "room": room, "cleared": True}).encode(),
                                  "application/json")
                    return
                until_spec = payload.get("until", "").strip()
                until = parse_override_until(until_spec)
                if not until:
                    self._respond(400,
                                  json.dumps({"error": f"unparseable 'until': {until_spec!r}"}).encode(),
                                  "application/json")
                    return
                state_to_set = payload.get("state")
                if state_to_set is not None:
                    state_to_set = str(state_to_set).strip().lower()
                    if state_to_set not in ("on", "off", ""):
                        self._respond(
                            400,
                            json.dumps({"error": f"bad state: {state_to_set!r}, use 'on' or 'off'"}).encode(),
                            "application/json",
                        )
                        return
                    if state_to_set == "":
                        state_to_set = None
                apply_override(room, until, state_to_set=state_to_set)
                self._respond(200,
                              json.dumps({"ok": True, "room": room,
                                          "until": until.isoformat(),
                                          "state_set": state_to_set}).encode(),
                              "application/json")
                return
            self.send_error(404)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[web] {fmt % args}\n")


if __name__ == "__main__":
    print(f"smart_ac web listening on 0.0.0.0:{PORT}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
