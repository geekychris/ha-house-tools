#!/usr/bin/env python3
"""
Retrospective analysis of smart_ac decisions and their effect on the
battery and per-AC load.

Reads:
  - /home/chris/smart_ac/decisions.log    (rotated, one JSON record per tick)
  - HA /api/history/period for SoC, load, and each input_boolean.ac_*

Produces:
  - A Markdown report at /home/chris/smart_ac/reports/YYYY-MM-DD.md
  - HA sensor.smart_ac_retrospective with summary attributes (consumed by
    /smart_ac_report Telegram command + dashboard markdown card)

Stats produced:
  - SoC at start, peak, end of the window
  - Time in each mode (minutes)
  - Per-AC runtime (minutes ON)
  - Per-AC estimated draw (W) derived from load deltas around single-AC
    transitions: when exactly one AC transitioned and no other was nearby
    (within ±4 min), measure mean load in (-90s, -30s) vs (+30s, +90s).
    Median of all such samples is the estimate.
  - Action timeline + recent notable events

Run modes:
  - On-demand:     python3 retrospective.py                 (analyzes last 24h)
  - Specific day:  python3 retrospective.py 2026-06-30
  - Specific span: python3 retrospective.py 2026-06-29 2026-06-30
  - Systemd timer: smart-ac-retrospective.timer fires the .service nightly

Designed to be cheap: ~2 HA REST calls + read decisions.log file. Safe to
run any number of times.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import os
import pathlib
import statistics
import sys
import urllib.parse
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
DECISIONS_LOG = pathlib.Path("/home/chris/smart_ac/decisions.log")
REPORTS_DIR = pathlib.Path("/home/chris/smart_ac/reports")

# Off-grid rate used for the cost estimate. Matches the value the main
# dashboard uses for the "Off-Grid Savings" card (GRID_RATE_USD_PER_KWH in
# create_energy_dashboard.py). Override via env if you want a different figure.
GRID_RATE_USD_PER_KWH = float(os.environ.get("GRID_RATE_USD_PER_KWH", "0.30"))


# ---------------------------------------------------------------------- config


def load_config() -> dict:
    p = pathlib.Path(os.environ.get("SMART_AC_CONFIG", HERE / "smart_ac.json"))
    return json.loads(p.read_text())


# ---------------------------------------------------------------------- HA helpers


def ha_get(cfg: dict, path: str) -> object:
    req = urllib.request.Request(
        f"{cfg['ha_url'].rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ha_set_state(cfg: dict, entity_id: str, state: str, attrs: dict) -> None:
    url = f"{cfg['ha_url'].rstrip('/')}/api/states/{entity_id}"
    body = {"state": state, "attributes": attrs}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


# ---------------------------------------------------------------------- decisions.log


def iter_decision_records(start: dt.datetime, end: dt.datetime):
    """Yields decision dicts whose ts is within [start, end]. Reads the current
    log + any rotated backups (decisions.log.1, decisions.log.2, ...)."""
    files: list[pathlib.Path] = []
    if DECISIONS_LOG.exists():
        files.append(DECISIONS_LOG)
    parent = DECISIONS_LOG.parent
    for p in sorted(parent.glob(f"{DECISIONS_LOG.name}.*")):
        files.append(p)
    for f in files:
        opener = gzip.open if f.suffix == ".gz" else open
        try:
            with opener(f, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = parse_ts(rec["ts"])
                    except Exception:
                        continue
                    if start <= ts <= end:
                        yield rec
        except Exception as e:
            print(f"# warn: could not read {f}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------- HA history


def fetch_history(cfg: dict, start: dt.datetime, end: dt.datetime,
                  entity_ids: list[str]) -> dict[str, list[dict]]:
    """One /api/history/period call per entity. Returns map entity_id -> list
    of {state, last_changed}."""
    out: dict[str, list[dict]] = {}
    base = cfg["ha_url"].rstrip("/")
    for eid in entity_ids:
        # Use minimal_response to keep payload small.
        start_iso = start.isoformat(timespec="seconds")
        end_iso = urllib.parse.quote(end.isoformat(timespec="seconds"))
        url = (f"{base}/api/history/period/{start_iso}"
               f"?end_time={end_iso}&filter_entity_id={eid}&minimal_response")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            series = data[0] if data else []
            out[eid] = [
                {"state": s.get("state"),
                 "last_changed": parse_ts(s.get("last_changed", s.get("last_updated")))}
                for s in series
                if s.get("state") not in (None, "unknown", "unavailable")
            ]
        except Exception as e:
            print(f"# warn: history fetch failed for {eid}: {e}", file=sys.stderr)
            out[eid] = []
    return out


# ---------------------------------------------------------------------- analysis


def soc_summary(soc_series: list[dict]) -> dict:
    if not soc_series:
        return {"start": None, "peak": None, "peak_at": None, "end": None}
    vals = []
    for s in soc_series:
        try:
            vals.append((s["last_changed"], float(s["state"])))
        except Exception:
            continue
    if not vals:
        return {"start": None, "peak": None, "peak_at": None, "end": None}
    peak = max(vals, key=lambda x: x[1])
    return {
        "start": round(vals[0][1], 1),
        "peak": round(peak[1], 1),
        "peak_at": peak[0].isoformat(),
        "end": round(vals[-1][1], 1),
    }


def time_in_modes(decisions: list[dict], interval_min: float) -> dict[str, float]:
    """Each tick represents ~interval_min minutes in a particular mode."""
    counter = collections.Counter()
    for r in decisions:
        counter[r.get("mode", "?")] += 1
    return {m: round(c * interval_min, 1) for m, c in counter.items()}


def per_ac_runtime(history: dict[str, list[dict]],
                   start: dt.datetime, end: dt.datetime,
                   rooms: list[str]) -> dict[str, float]:
    """Minutes each input_boolean.ac_<room> was ON during the window."""
    out: dict[str, float] = {}
    for r in rooms:
        eid = f"input_boolean.ac_{r}"
        series = history.get(eid, [])
        if not series:
            out[r] = 0.0
            continue
        # Boundary state at `start`: assume same as first entry (or "off")
        prev_state = series[0]["state"]
        prev_ts = max(start, series[0]["last_changed"])
        minutes_on = 0.0
        for entry in series[1:]:
            ts = entry["last_changed"]
            if ts < start:
                prev_state = entry["state"]
                prev_ts = ts
                continue
            if ts > end:
                ts = end
            if prev_state == "on":
                minutes_on += (ts - prev_ts).total_seconds() / 60
            prev_state = entry["state"]
            prev_ts = ts
        if prev_state == "on" and prev_ts < end:
            minutes_on += (end - prev_ts).total_seconds() / 60
        out[r] = round(minutes_on, 1)
    return out


def estimate_per_ac_watts(history: dict[str, list[dict]],
                          load_eid: str, rooms: list[str]) -> dict[str, dict]:
    """Look at moments when EXACTLY ONE AC transitioned and no other AC
    transitioned within ±4 min. Measure mean load delta over (-90s, -30s)
    vs (+30s, +90s). Median of samples per room is the estimate."""
    transitions = []
    for r in rooms:
        for s in history.get(f"input_boolean.ac_{r}", []):
            transitions.append({"ts": s["last_changed"], "room": r, "state": s["state"]})
    transitions.sort(key=lambda t: t["ts"])

    load_series = sorted(
        [(s["last_changed"], _to_float(s["state"]))
         for s in history.get(load_eid, [])
         if _to_float(s["state"]) is not None],
        key=lambda x: x[0],
    )
    if not load_series:
        return {}

    def mean_load(t0: dt.datetime, t1: dt.datetime) -> float | None:
        vals = [v for ts, v in load_series if t0 <= ts <= t1]
        return statistics.mean(vals) if vals else None

    samples: dict[str, list[float]] = collections.defaultdict(list)
    n = len(transitions)
    for i, t in enumerate(transitions):
        # Skip if any other AC transitioned within ±4 min
        cutoff = dt.timedelta(minutes=4)
        nearby = [
            o for o in transitions
            if o is not t and o["room"] != t["room"]
            and abs((o["ts"] - t["ts"]).total_seconds()) < cutoff.total_seconds()
        ]
        if nearby:
            continue
        before = mean_load(t["ts"] - dt.timedelta(seconds=90),
                           t["ts"] - dt.timedelta(seconds=30))
        after = mean_load(t["ts"] + dt.timedelta(seconds=30),
                          t["ts"] + dt.timedelta(seconds=90))
        if before is None or after is None:
            continue
        delta = after - before
        if t["state"] == "off":
            delta = -delta
        if 200 < abs(delta) < 3500:
            samples[t["room"]].append(abs(delta))

    out: dict[str, dict] = {}
    for r in rooms:
        s = samples.get(r, [])
        if s:
            out[r] = {
                "estimate_w": round(statistics.median(s)),
                "samples": len(s),
                "min_w": round(min(s)),
                "max_w": round(max(s)),
            }
    return out


def _to_float(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def estimate_costs(runtime_min: dict[str, float],
                   draws: dict[str, dict],
                   cfg: dict) -> dict[str, dict]:
    """Multiply per-AC runtime by per-AC estimated draw to get an approximate
    energy + dollar cost per room. Uses the measured draw from the retrospective
    if there were enough single-AC transitions in the window; otherwise falls
    back to `ac_power_estimate_w` (default 1000 W) from smart_ac.json so every
    AC gets some number.

    All numbers are approximations -- ACs have variable-speed compressors and
    duty cycles that a spot-measurement doesn't capture. Wide error bars.
    """
    default_w = float(cfg.get("ac_power_estimate_w", 1000))
    out: dict[str, dict] = {}
    total_kwh = 0.0
    for room, mins in runtime_min.items():
        if mins <= 0:
            continue
        # Prefer measured draw for this room; fall back to the config default
        watts = float(draws.get(room, {}).get("estimate_w") or default_w)
        source = "measured" if draws.get(room, {}).get("estimate_w") else "default"
        kwh = mins / 60.0 * watts / 1000.0
        usd = kwh * GRID_RATE_USD_PER_KWH
        total_kwh += kwh
        out[room] = {
            "watts_used": round(watts),
            "watts_source": source,
            "kwh": round(kwh, 2),
            "usd": round(usd, 2),
        }
    out["_total"] = {
        "kwh": round(total_kwh, 2),
        "usd": round(total_kwh * GRID_RATE_USD_PER_KWH, 2),
        "rate_usd_per_kwh": GRID_RATE_USD_PER_KWH,
    }
    return out


def collect_actions(decisions: list[dict]) -> list[dict]:
    """Pulls every action_taken record across the decisions window."""
    out = []
    for r in decisions:
        for a in r.get("actions", []):
            # actions in decisions.log are strings like "living:turn_on"
            parts = a.split(":")
            if len(parts) == 2:
                room, svc = parts
            else:
                room, svc = a, "?"
            out.append({
                "ts": r["ts"],
                "mode": r.get("mode"),
                "room": room,
                "action": svc.replace("turn_", "").upper(),
                "reason": (r.get("reasons") or {}).get(room, "?"),
            })
    return out


# ---------------------------------------------------------------------- report


def _local(iso: str | None) -> str:
    """Render an ISO timestamp as local 'YYYY-MM-DD HH:MM' (no seconds)."""
    if not iso:
        return "?"
    try:
        return parse_ts(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _local_hm(iso: str | None) -> str:
    """Render an ISO timestamp as local 'HH:MM'."""
    if not iso:
        return "?"
    try:
        return parse_ts(iso).astimezone().strftime("%H:%M")
    except Exception:
        return "?"


def make_report(summary: dict) -> str:
    s = summary
    lines = []
    lines.append(f"# Smart AC retrospective")
    lines.append("")
    lines.append(f"**Window (local):** {_local(s['start'])} → {_local(s['end'])}")
    lines.append("")
    lines.append("## Battery / SoC")
    soc = s["soc"]
    if soc["start"] is not None:
        peak_at = _local_hm(soc.get("peak_at"))
        lines.append(f"- Start:   **{soc['start']}%**")
        lines.append(f"- Peak:    **{soc['peak']}%** at {peak_at}")
        lines.append(f"- End:     **{soc['end']}%**")
        delta = soc["end"] - soc["start"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"- Net:     {sign}{round(delta, 1)} %")
    else:
        lines.append("- (no SoC history in window)")
    lines.append("")
    lines.append("## Time in each mode (minutes)")
    modes = s["modes_min"]
    if modes:
        for m in sorted(modes.keys(), key=lambda k: -modes[k]):
            lines.append(f"- {m}: {modes[m]}")
    else:
        lines.append("- (no decision records in window)")
    lines.append("")
    lines.append(f"## Per-AC runtime (minutes ON)")
    runtime = s["runtime_min"]
    for r in sorted(runtime.keys(), key=lambda k: -runtime[k]):
        lines.append(f"- {r}: {runtime[r]}")
    lines.append("")
    lines.append("## Per-AC estimated draw")
    draws = s["draw_w"]
    if draws:
        lines.append("(median load-delta around single-AC transitions; "
                     "wide error bars expected, especially with <5 samples)")
        for r, info in sorted(draws.items(), key=lambda kv: -kv[1]["estimate_w"]):
            lines.append(f"- {r}: ~**{info['estimate_w']} W** "
                         f"(n={info['samples']}, range {info['min_w']}-{info['max_w']} W)")
    else:
        lines.append("- (no isolated transitions in window — try with a longer window or run calibrate.py)")
    lines.append("")

    # Cost estimate: runtime * (measured or default) draw * rate.
    lines.append("## Estimated energy + cost")
    costs = s["costs"]
    total = costs.get("_total", {})
    if total:
        lines.append(
            f"_Approximation._ Rate used: **${total.get('rate_usd_per_kwh')}/kWh**. "
            "Watts per AC = measured from single-AC transitions if available "
            "(marked 'measured'), otherwise the config default (marked 'default'). "
            "ACs have variable compressors + duty cycles, so real cost may differ "
            "±30%."
        )
        lines.append("")
        lines.append("| Room | Runtime (min) | W used | Source | kWh | Cost |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(costs.keys()):
            if r == "_total":
                continue
            info = costs[r]
            lines.append(
                f"| {r} | {s['runtime_min'].get(r, 0)} | "
                f"{info['watts_used']} | {info['watts_source']} | "
                f"{info['kwh']} | ${info['usd']} |"
            )
        lines.append(f"| **Total** | | | | **{total['kwh']}** | **${total['usd']}** |")
    lines.append("")
    actions = s["actions"]
    if actions:
        lines.append(f"## Actions in window ({len(actions)})")
        lines.append("")
        lines.append("Each action lists the reason the scheduler decided to flip that AC.")
        lines.append("")
        lines.append("| Time (local) | Action | Room | Reason | Mode |")
        lines.append("|---|---|---|---|---|")
        for a in actions:
            ts = _local_hm(a["ts"])
            lines.append(f"| {ts} | {a['action']} | {a['room']} | "
                         f"{a['reason']} | {a['mode']} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- driver


def parse_args(argv: list[str]) -> tuple[dt.datetime, dt.datetime]:
    """Parse 0/1/2 date args, return UTC start/end."""
    tz_local = dt.datetime.now().astimezone().tzinfo
    if len(argv) == 0:
        # last 24h ending now
        end = dt.datetime.now(tz=dt.timezone.utc)
        start = end - dt.timedelta(hours=24)
    elif len(argv) == 1:
        # specific day (local), midnight-to-midnight
        day = dt.date.fromisoformat(argv[0])
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz_local)
        end = start + dt.timedelta(days=1)
    elif len(argv) == 2:
        start = dt.datetime.combine(dt.date.fromisoformat(argv[0]),
                                    dt.time(0, 0), tzinfo=tz_local)
        end = dt.datetime.combine(dt.date.fromisoformat(argv[1]),
                                  dt.time(23, 59, 59), tzinfo=tz_local)
    else:
        sys.exit("Usage: retrospective.py [date] | [start_date end_date]")
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def main() -> int:
    cfg = load_config()
    if "HA_TOKEN" not in os.environ:
        sys.exit("HA_TOKEN env var required")

    start, end = parse_args(sys.argv[1:])
    print(f"# window {start.isoformat()} -> {end.isoformat()}", file=sys.stderr)

    rooms = sorted(set(cfg["night_min_acs"]) | set(cfg["day_priority"]))
    interval = float(cfg.get("evaluation_interval_minutes", 5))

    decisions = list(iter_decision_records(start, end))
    history = fetch_history(
        cfg, start, end,
        [cfg["soc_sensor"], cfg["load_sensor"]] +
        [f"input_boolean.ac_{r}" for r in rooms],
    )

    soc = soc_summary(history.get(cfg["soc_sensor"], []))
    modes = time_in_modes(decisions, interval)
    runtime = per_ac_runtime(history, start, end, rooms)
    draws = estimate_per_ac_watts(history, cfg["load_sensor"], rooms)
    costs = estimate_costs(runtime, draws, cfg)
    actions = collect_actions(decisions)

    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "soc": soc,
        "modes_min": modes,
        "runtime_min": runtime,
        "draw_w": draws,
        "costs": costs,
        "actions": actions,
        "decision_count": len(decisions),
    }

    md = make_report(summary)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    day_stem = end.astimezone().strftime("%Y-%m-%d")
    out_path = REPORTS_DIR / (day_stem + ".md")
    out_path.write_text(md)
    print(f"# wrote {out_path}", file=sys.stderr)

    # JSON sidecar: same summary, machine-readable. weekly.py aggregates
    # by scanning these instead of parsing the markdown. Keep the shape
    # stable -- other tools depend on the keys here.
    json_path = REPORTS_DIR / (day_stem + ".json")
    json_path.write_text(json.dumps({
        "day": day_stem,
        **summary,
    }, default=str, indent=2))
    print(f"# wrote {json_path}", file=sys.stderr)

    print(md)

    # Latest N actions with reason -- surfaced in the /smart_ac_report
    # Telegram reply and the web UI. Cap so the sensor payload doesn't
    # get huge.
    actions_recent = [
        {
            "time": _local_hm(a["ts"]),
            "action": a["action"],
            "room": a["room"],
            "reason": a["reason"],
            "mode": a["mode"],
        }
        for a in actions[-12:]  # most recent 12
    ]

    # Push summary to HA so it's reachable via /smart_ac_report.
    ha_set_state(
        cfg,
        "sensor.smart_ac_retrospective",
        f"{soc.get('start','?')}->{soc.get('end','?')}%",
        {
            "friendly_name": "Smart AC retrospective",
            "icon": "mdi:chart-timeline-variant",
            "start_local": _local(summary["start"]),
            "end_local": _local(summary["end"]),
            "start": summary["start"],
            "end": summary["end"],
            "soc_start": soc.get("start"),
            "soc_peak": soc.get("peak"),
            "soc_peak_at": soc.get("peak_at"),
            "soc_peak_local": _local_hm(soc.get("peak_at")),
            "soc_end": soc.get("end"),
            "modes_min": modes,
            "runtime_min": runtime,
            "draw_w": draws,
            "costs": costs,
            "cost_total_kwh": costs.get("_total", {}).get("kwh"),
            "cost_total_usd": costs.get("_total", {}).get("usd"),
            "cost_rate_usd_per_kwh": costs.get("_total", {}).get("rate_usd_per_kwh"),
            "action_count": len(actions),
            "actions_recent": actions_recent,
            "decision_count": summary["decision_count"],
            "report_path": str(out_path),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
