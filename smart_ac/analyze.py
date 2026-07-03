#!/usr/bin/env python3
"""
Command-line query tool for the smart_ac stats DB.

Presets:
  summary                 Today's action summary + net effect.
  drift                   Per-room calibration drift (last 14 days).
  dynamics                Per-room warm-up rate (last 30 days).
  mode-time [days]        Minutes in each mode over the last N days (default 30).
  room-cost [days]        Per-room cost over the last N days (default 30).
  cost-trend [days]       Day-by-day kWh / $ trend.
  net-savings [days]      Day-by-day turn_off saved vs turn_on added (from observations).
  correlate <room> [days] Outdoor temp vs warmup rate scatter for one room.
  daily [n]               List last N daily_summaries rows (default 7).
  observations [n]        Most recent N observation rows (default 20).
  info                    DB stats: row counts, size, oldest/newest.
  sql "<query>"           Raw SQL passthrough. Result printed as a table.

Usage:
    python3 analyze.py summary
    python3 analyze.py correlate living 45
    python3 analyze.py sql "SELECT mode, COUNT(*) FROM decisions GROUP BY mode"
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

# Support both `python3 analyze.py` (relative import fails) and package use.
try:
    from . import stats  # type: ignore
except (ImportError, ValueError):
    HERE = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    import stats  # type: ignore


def _print_table(rows: list[dict], columns: list[str] | None = None) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = columns or list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = " | ".join(str(c).ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _cmd_summary(args: list[str]) -> int:
    with stats.opened() as conn:
        d = stats.daily_action_summary(conn)
    print(f"Day: {d['day']}")
    off = d.get("turn_off", {})
    on = d.get("turn_on", {})
    print(f"Turn-offs: {off.get('n', 0)}  total saved {off.get('total_saved_w', 0)} W  "
          f"(avg {off.get('avg_saved_w', 0)} W/action)")
    print(f"Turn-ons:  {on.get('n', 0)}  total added {on.get('total_added_w', 0)} W  "
          f"(avg {on.get('avg_added_w', 0)} W/action)")
    print(f"Net load delta today: {d.get('net_load_delta_w', 0)} W")
    return 0


def _cmd_drift(args: list[str]) -> int:
    with stats.opened() as conn:
        rows = stats.drift_by_room(conn)
    if not rows:
        print("(no drift data yet -- need ≥3 isolated observations per room+action)")
        return 0
    _print_table(rows, ["room", "action", "n", "avg_actual_w", "avg_expected_w",
                        "avg_diff_w", "drift_pct", "drift_flag"])
    return 0


def _cmd_dynamics(args: list[str]) -> int:
    with stats.opened() as conn:
        rows = stats.warm_up_rates(conn)
    if not rows:
        print("(no warm-up data yet -- need ≥3 isolated turn_off observations per room)")
        return 0
    _print_table(rows, ["room", "samples", "f_per_min", "avg_outdoor_f"])
    return 0


def _cmd_mode_time(args: list[str]) -> int:
    days = int(args[0]) if args else 30
    with stats.opened() as conn:
        mode_min = stats.mode_minutes_last_days(conn, days_back=days)
    if not mode_min:
        print(f"(no decisions in last {days} days)")
        return 0
    print(f"Mode minutes over last {days} days (5 min per tick):")
    for m, mins in sorted(mode_min.items(), key=lambda x: -x[1]):
        hours = mins / 60
        print(f"  {m:<20} {mins:>7} min  ({hours:>6.1f} h)")
    return 0


def _cmd_room_cost(args: list[str]) -> int:
    days = int(args[0]) if args else 30
    with stats.opened() as conn:
        agg = stats.weekly_from_db(conn, days_back=days)
    cost = agg.get("cost_usd", {})
    if not cost:
        print(f"(no daily summaries in last {days} days)")
        return 0
    print(f"Per-room cost over last {days} days:")
    for room, usd in sorted(cost.items(), key=lambda x: -x[1]):
        kwh = agg.get("cost_kwh", {}).get(room, 0)
        print(f"  {room:<10} {kwh:>7.2f} kWh   ${usd:>7.2f}")
    print(f"  {'-total':<10} {agg.get('total_kwh', 0):>7.2f} kWh   "
          f"${agg.get('total_usd', 0):>7.2f}")
    return 0


def _cmd_cost_trend(args: list[str]) -> int:
    days = int(args[0]) if args else 30
    with stats.opened() as conn:
        rows = stats.cost_by_day(conn, days_back=days)
    if not rows:
        print(f"(no daily summaries in last {days} days)")
        return 0
    _print_table(rows, ["day", "kwh", "usd"])
    return 0


def _cmd_net_savings(args: list[str]) -> int:
    days = int(args[0]) if args else 30
    with stats.opened() as conn:
        rows = stats.net_savings_by_day(conn, days_back=days)
    if not rows:
        print(f"(no daily summaries in last {days} days)")
        return 0
    _print_table(rows, ["day", "turn_off_savings_w", "turn_on_load_w", "net_conserved_w"])
    return 0


def _cmd_correlate(args: list[str]) -> int:
    if not args:
        sys.exit("Usage: analyze.py correlate <room> [days]")
    room = args[0]
    days = int(args[1]) if len(args) > 1 else 60
    with stats.opened() as conn:
        rows = stats.outdoor_temp_vs_warmup(conn, room, days_back=days)
    if not rows:
        print(f"(no isolated turn_off observations for {room} in last {days} days)")
        return 0
    print(f"Outdoor temp vs warm-up rate for {room} (last {days} days):")
    _print_table(rows, ["outdoor_f", "warmup_f_per_min"])
    return 0


def _cmd_daily(args: list[str]) -> int:
    n = int(args[0]) if args else 7
    with stats.opened() as conn:
        rows = stats.get_daily_summaries(conn, days_back=n)
    if not rows:
        print("(no daily summaries)")
        return 0
    # Show a compact view
    view = [{
        "day": r["day"],
        "soc_s/p/e": f"{r['soc_start']}/{r['soc_peak']}/{r['soc_end']}",
        "ac_min": r["total_ac_min"],
        "kwh": round(r["total_kwh"] or 0, 2),
        "usd": round(r["total_usd"] or 0, 2),
        "actions": r["n_actions"],
        "off_saved_w": r["turn_off_savings_w"],
        "on_added_w": r["turn_on_load_w"],
    } for r in rows]
    _print_table(view, list(view[0].keys()))
    return 0


def _cmd_observations(args: list[str]) -> int:
    n = int(args[0]) if args else 20
    with stats.opened() as conn:
        rows = stats.recent_observations(conn, n=n)
    if not rows:
        print("(no observations yet)")
        return 0
    view = [{
        "time": str(r["ts_observed"])[:19],
        "room": r["primary_room"],
        "action": r["primary_action"],
        "mode": r["mode_at_action"],
        "actual": r["delta_load_w"],
        "expected": r["expected_delta_w"],
        "diff": r["delta_vs_expected_w"],
    } for r in rows]
    _print_table(view, list(view[0].keys()))
    return 0


def _cmd_info(args: list[str]) -> int:
    path = stats.DEFAULT_DB_PATH
    with stats.opened() as conn:
        counts = {
            "decisions": conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "observations": conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "daily_summaries": conn.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0],
        }
        oldest = conn.execute("SELECT MIN(ts_local) FROM decisions").fetchone()[0]
        newest = conn.execute("SELECT MAX(ts_local) FROM decisions").fetchone()[0]
    print(f"DB: {path}")
    if path.is_file():
        print(f"Size: {path.stat().st_size:,} bytes")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"decisions span: {oldest} -> {newest}")
    return 0


def _cmd_sql(args: list[str]) -> int:
    if not args:
        sys.exit("Usage: analyze.py sql \"<query>\"")
    query = " ".join(args)
    with stats.opened() as conn:
        try:
            rows = [dict(r) for r in conn.execute(query).fetchall()]
        except Exception as e:
            sys.exit(f"SQL error: {e}")
    if not rows:
        print("(no rows)")
        return 0
    _print_table(rows)
    return 0


COMMANDS = {
    "summary": _cmd_summary,
    "drift": _cmd_drift,
    "dynamics": _cmd_dynamics,
    "mode-time": _cmd_mode_time,
    "room-cost": _cmd_room_cost,
    "cost-trend": _cmd_cost_trend,
    "net-savings": _cmd_net_savings,
    "correlate": _cmd_correlate,
    "daily": _cmd_daily,
    "observations": _cmd_observations,
    "info": _cmd_info,
    "sql": _cmd_sql,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        print("Commands:")
        for name in COMMANDS:
            print(f"  {name}")
        return 0
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        sys.exit(f"Unknown command: {cmd}. Run analyze.py --help.")
    return COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
