#!/usr/bin/env python3
"""
Weekly rollup of the daily retrospectives.

Aggregates the last N days (default 7) of JSON sidecars written by
retrospective.py at /home/chris/smart_ac/reports/YYYY-MM-DD.json,
producing:

  - A weekly Markdown report at /home/chris/smart_ac/reports/weekly-YYYY-WW.md
  - sensor.smart_ac_weekly in HA with summary attributes (consumed by
    /smart_ac_weekly Telegram command)

Aggregated stats:
  - Total mode-minutes per mode across the week
  - Total AC runtime per room (minutes)
  - Total AC cost per room ($) + weekly total
  - SoC min / max / average across the week
  - Day-by-day thumbnail: SoC start/end + total AC minutes each day

Run on demand:
    python3 weekly.py                (last 7 days ending today)
    python3 weekly.py 14             (last 14 days)

Systemd:
    smart-ac-weekly.timer fires the .service every Monday 00:45.

Missing days (no JSON sidecar) are skipped with a note; the report still
shows partial-week aggregates.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from collections import defaultdict


HERE = pathlib.Path(__file__).resolve().parent
REPORTS_DIR = pathlib.Path("/home/chris/smart_ac/reports")

# Rate for local cost math (mirrors retrospective.py's GRID_RATE_USD_PER_KWH).
GRID_RATE_USD_PER_KWH = float(os.environ.get("GRID_RATE_USD_PER_KWH", "0.30"))


def load_config() -> dict:
    p = pathlib.Path(os.environ.get("SMART_AC_CONFIG", HERE / "smart_ac.json"))
    return json.loads(p.read_text())


def ha_set_state(cfg: dict, entity_id: str, state, attrs: dict) -> None:
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


def load_days(n_days: int) -> tuple[list[dict], list[str]]:
    """Return (list of day summaries, list of missing day stems)."""
    today = dt.date.today()
    days: list[dict] = []
    missing: list[str] = []
    for offset in range(1, n_days + 1):
        day = today - dt.timedelta(days=offset)
        stem = day.strftime("%Y-%m-%d")
        p = REPORTS_DIR / (stem + ".json")
        if not p.is_file():
            missing.append(stem)
            continue
        try:
            days.append(json.loads(p.read_text()))
        except Exception as e:
            print(f"# skipping {stem}: {e}", file=sys.stderr)
            missing.append(stem)
    days.sort(key=lambda d: d.get("day", ""))
    return days, missing


def aggregate(days: list[dict]) -> dict:
    if not days:
        return {"n_days_with_data": 0, "message": "no daily reports found"}

    modes_min: dict[str, int] = defaultdict(int)
    runtime_min: dict[str, int] = defaultdict(int)
    cost_kwh: dict[str, float] = defaultdict(float)
    cost_usd: dict[str, float] = defaultdict(float)
    soc_starts: list[float] = []
    soc_ends: list[float] = []
    soc_peaks: list[float] = []
    per_day: list[dict] = []

    for d in days:
        soc = d.get("soc") or {}
        if soc.get("start") is not None:
            soc_starts.append(float(soc["start"]))
        if soc.get("end") is not None:
            soc_ends.append(float(soc["end"]))
        if soc.get("peak") is not None:
            soc_peaks.append(float(soc["peak"]))

        for mode, m in (d.get("modes_min") or {}).items():
            modes_min[mode] += int(m)

        for room, m in (d.get("runtime_min") or {}).items():
            runtime_min[room] += int(m)

        for room, c in (d.get("costs") or {}).items():
            if room == "_total":
                continue
            if not isinstance(c, dict):
                continue
            cost_kwh[room] += float(c.get("kwh", 0))
            cost_usd[room] += float(c.get("usd", 0))

        day_total_min = sum((d.get("runtime_min") or {}).values())
        per_day.append({
            "day": d.get("day"),
            "soc_start": soc.get("start"),
            "soc_end": soc.get("end"),
            "soc_peak": soc.get("peak"),
            "ac_total_min": day_total_min,
        })

    total_kwh = round(sum(cost_kwh.values()), 2)
    total_usd = round(sum(cost_usd.values()), 2)

    return {
        "n_days_with_data": len(days),
        "modes_min": dict(modes_min),
        "runtime_min": dict(runtime_min),
        "cost_kwh": {k: round(v, 2) for k, v in cost_kwh.items()},
        "cost_usd": {k: round(v, 2) for k, v in cost_usd.items()},
        "total_kwh": total_kwh,
        "total_usd": total_usd,
        "soc_min": min(soc_ends + soc_starts) if (soc_ends or soc_starts) else None,
        "soc_max": max(soc_peaks) if soc_peaks else None,
        "soc_avg_start": round(sum(soc_starts) / len(soc_starts), 1) if soc_starts else None,
        "soc_avg_end": round(sum(soc_ends) / len(soc_ends), 1) if soc_ends else None,
        "per_day": per_day,
    }


def make_report(agg: dict, missing: list[str], now: dt.datetime) -> str:
    lines = [
        f"# Weekly smart_ac rollup ({now.strftime('%Y-W%V')})",
        "",
        f"Days with data: **{agg.get('n_days_with_data', 0)}**",
    ]
    if missing:
        lines.append(f"Days missing: {', '.join(missing)}")
    lines.append("")
    if agg.get("n_days_with_data", 0) == 0:
        lines.append("_(no daily reports found in the window)_")
        return "\n".join(lines)

    lines += [
        "## SoC",
        f"- Min end-of-day: {agg.get('soc_min')}",
        f"- Peak: {agg.get('soc_max')}",
        f"- Avg start-of-day: {agg.get('soc_avg_start')}",
        f"- Avg end-of-day: {agg.get('soc_avg_end')}",
        "",
        "## Mode minutes (total across the week)",
    ]
    for mode, m in sorted(agg["modes_min"].items(), key=lambda x: -x[1]):
        lines.append(f"- {mode}: {m}")
    lines += [
        "",
        "## Per-AC runtime + cost",
        "| Room | Minutes | kWh | USD |",
        "|---|---:|---:|---:|",
    ]
    all_rooms = sorted(set(agg["runtime_min"]) | set(agg["cost_kwh"]))
    for r in all_rooms:
        m = agg["runtime_min"].get(r, 0)
        k = agg["cost_kwh"].get(r, 0.0)
        u = agg["cost_usd"].get(r, 0.0)
        lines.append(f"| {r} | {m} | {k:.2f} | ${u:.2f} |")
    lines.append(f"| **total** |  | **{agg['total_kwh']:.2f}** | **${agg['total_usd']:.2f}** |")

    lines += ["", "## Per-day thumbnail",
              "| Day | SoC start | SoC end | Peak | AC total min |",
              "|---|---:|---:|---:|---:|"]
    for d in agg.get("per_day", []):
        lines.append(
            f"| {d['day']} | {d.get('soc_start','?')} | {d.get('soc_end','?')} "
            f"| {d.get('soc_peak','?')} | {d.get('ac_total_min', 0)} |"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    now = dt.datetime.now().astimezone()

    # Prefer the SQLite daily_summaries table if it has enough data. Fall
    # back to parsing per-day JSON sidecars when the DB is empty (fresh
    # install) or partial.
    try:
        from . import stats as _stats  # type: ignore
    except (ImportError, ValueError):
        try:
            import stats as _stats  # type: ignore
        except ImportError:
            _stats = None  # type: ignore

    agg: dict = {}
    missing: list[str] = []
    used_db = False
    if _stats is not None:
        try:
            with _stats.opened() as conn:
                db_agg = _stats.weekly_from_db(conn, days_back=n_days)
                if db_agg.get("n_days_with_data", 0) >= 1:
                    agg = db_agg
                    used_db = True
        except Exception as e:
            print(f"# stats DB read failed, falling back to JSON: {e}", file=sys.stderr)

    if not used_db:
        days, missing = load_days(n_days)
        agg = aggregate(days)
        print("# aggregated from per-day JSON sidecars (DB was empty)", file=sys.stderr)
    else:
        print(f"# aggregated {agg['n_days_with_data']} days from daily_summaries table",
              file=sys.stderr)

    report = make_report(agg, missing, now)

    stem = now.strftime("weekly-%Y-W%V")
    out_path = REPORTS_DIR / (stem + ".md")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"# wrote {out_path}", file=sys.stderr)
    print(report)

    # Push summary to HA. Keep the payload compact -- HA truncates
    # large attribute dicts and history is cheaper when small.
    try:
        cfg = load_config()
    except Exception as e:
        print(f"# skipping HA push: {e}", file=sys.stderr)
        return 0

    attrs = {
        "friendly_name": "Smart AC weekly",
        "icon": "mdi:calendar-week",
        "generated_at": now.astimezone(dt.timezone.utc).isoformat(),
        "n_days_with_data": agg.get("n_days_with_data", 0),
        "missing_days": missing,
        "modes_min": agg.get("modes_min", {}),
        "runtime_min": agg.get("runtime_min", {}),
        "cost_usd": agg.get("cost_usd", {}),
        "total_kwh": agg.get("total_kwh", 0),
        "total_usd": agg.get("total_usd", 0),
        "soc_min": agg.get("soc_min"),
        "soc_max": agg.get("soc_max"),
        "soc_avg_start": agg.get("soc_avg_start"),
        "soc_avg_end": agg.get("soc_avg_end"),
        "per_day": agg.get("per_day", []),
        "report_path": str(out_path),
    }
    try:
        ha_set_state(
            cfg,
            "sensor.smart_ac_weekly",
            f"${agg.get('total_usd', 0):.2f}/wk",
            attrs,
        )
        print("# updated sensor.smart_ac_weekly", file=sys.stderr)
    except Exception as e:
        print(f"# HA push failed: {e}", file=sys.stderr)

    # Prune the stats DB. Weekly cadence gives us a natural cleanup
    # opportunity. Env-overridable retention: default keep 90 days of
    # decisions (~26k rows) and observations + daily_summaries forever.
    if _stats is not None:
        try:
            keep_dec = int(os.environ.get("STATS_PRUNE_DECISIONS_DAYS", "90"))
            with _stats.opened() as conn:
                deleted = _stats.prune(
                    conn,
                    decisions_days=keep_dec,
                    observations_days=None,
                    daily_summaries_days=None,
                )
                if any(deleted.values()):
                    print(f"# pruned {deleted}", file=sys.stderr)
        except Exception as e:
            print(f"# prune failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
