#!/usr/bin/env python3
"""
Long-term statistics database for smart_ac.

Stores every tick's decision + every action's follow-up observation in a
local SQLite file. The JSONL files (decisions.log, observations.jsonl)
remain the source of truth; this DB is a derived, query-friendly view
that other tools (retrospective.py, web.py, analyze scripts) read.

Schema:
  decisions       -- one row per 5-min tick
  observations    -- one row per action follow-up (post-settle)

Design notes:
- No migrations framework. Each run calls `init(conn)` which uses
  CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS. Additive
  changes only.
- The full JSON of each record is stored in `raw_json` alongside the
  parsed columns, so schema evolution can materialise new columns from
  history without needing the original files.
- `INSERT OR IGNORE` on primary keys makes backfill idempotent.

Env:
  SMART_AC_STATS_DB   -- override the DB path. Default:
                         ${HOME}/smart_ac/stats.sqlite3
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
from typing import Iterable, Iterator


DEFAULT_DB_PATH = pathlib.Path(os.environ.get(
    "SMART_AC_STATS_DB",
    str(pathlib.Path.home() / "smart_ac" / "stats.sqlite3"),
))


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ts TEXT NOT NULL PRIMARY KEY,
    ts_local TEXT NOT NULL,
    mode TEXT NOT NULL,
    soc REAL NOT NULL,
    battery_power_w INTEGER NOT NULL,
    pv_power_w INTEGER NOT NULL,
    load_w INTEGER NOT NULL,
    outdoor_f REAL NOT NULL,
    indoor_f_json TEXT NOT NULL,
    ac_on_json TEXT NOT NULL,
    target_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    unoccupied INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts_local ON decisions(ts_local);
CREATE INDEX IF NOT EXISTS idx_decisions_mode ON decisions(mode);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_observed TEXT NOT NULL,
    ts_action TEXT NOT NULL,
    age_min REAL NOT NULL,
    n_actions INTEGER NOT NULL,
    actions_json TEXT NOT NULL,
    primary_room TEXT NOT NULL,
    primary_action TEXT NOT NULL,
    primary_reason TEXT,
    mode_at_action TEXT,
    before_load_w INTEGER NOT NULL,
    after_load_w INTEGER NOT NULL,
    delta_load_w INTEGER NOT NULL,
    expected_delta_w INTEGER NOT NULL,
    delta_vs_expected_w INTEGER NOT NULL,
    before_soc REAL,
    after_soc REAL,
    delta_soc REAL,
    before_outdoor_f REAL,
    after_outdoor_f REAL,
    delta_indoor_f_room REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(ts_action, primary_room, primary_action)
);
CREATE INDEX IF NOT EXISTS idx_obs_ts_observed ON observations(ts_observed);
CREATE INDEX IF NOT EXISTS idx_obs_room_action ON observations(primary_room, primary_action);
CREATE INDEX IF NOT EXISTS idx_obs_mode ON observations(mode_at_action);

-- Materialised daily summary. Written by retrospective.py at end of each
-- day's run. weekly.py queries this instead of parsing per-day JSON
-- sidecars (faster, and no filesystem walk).
CREATE TABLE IF NOT EXISTS daily_summaries (
    day TEXT NOT NULL PRIMARY KEY,      -- YYYY-MM-DD (local)
    generated_at TEXT NOT NULL,
    soc_start REAL,
    soc_peak REAL,
    soc_end REAL,
    total_ac_min INTEGER,               -- sum across all rooms
    total_kwh REAL,
    total_usd REAL,
    per_room_min_json TEXT,             -- {"master": 480, ...}
    per_room_cost_json TEXT,            -- {"master": {"kwh":4.2,"usd":1.26}, ...}
    modes_min_json TEXT,                -- {"NIGHT":480, "ON_TRACK":120, ...}
    turn_off_savings_w INTEGER,         -- sum of |delta_load_w| over turn_offs (from observations)
    turn_on_load_w INTEGER,             -- sum of delta_load_w over turn_ons
    n_actions INTEGER,
    decision_count INTEGER,
    raw_json TEXT                       -- full retrospective summary blob
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_summaries(day);
"""


def connect(path: pathlib.Path | None = None) -> sqlite3.Connection:
    p = path or DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextlib.contextmanager
def opened(path: pathlib.Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init(conn)
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- insert helpers


def _local_iso(ts_iso: str) -> str:
    """Convert a UTC ISO timestamp to local ISO (no offset). Best-effort."""
    try:
        d = dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return d.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ts_iso


def insert_decision(conn: sqlite3.Connection, record: dict) -> None:
    """Insert a decision record. `record` matches decisions.log JSON shape.
    Silently ignores duplicates (idempotent backfill)."""
    ts = record.get("ts") or ""
    conn.execute(
        "INSERT OR IGNORE INTO decisions ("
        "ts, ts_local, mode, soc, battery_power_w, pv_power_w, load_w, "
        "outdoor_f, indoor_f_json, ac_on_json, target_json, reasons_json, "
        "actions_json, enabled, unoccupied, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            _local_iso(ts),
            record.get("mode") or "?",
            float(record.get("soc") or 0.0),
            int(record.get("battery_power_w") or 0),
            int(record.get("pv_power_w") or 0),
            int(record.get("load_w") or 0),
            float(record.get("outdoor_f") or 0.0),
            json.dumps(record.get("indoor_f") or {}),
            json.dumps(record.get("ac_on") or {}),
            json.dumps(record.get("target") or record.get("target_on") or {}),
            json.dumps(record.get("reasons") or {}),
            json.dumps(record.get("actions") or record.get("actions_this_tick") or []),
            1 if record.get("enabled", True) else 0,
            1 if record.get("unoccupied") else 0,
            json.dumps(record, default=str),
        ),
    )
    conn.commit()


def insert_observation(conn: sqlite3.Connection, obs: dict) -> None:
    """Insert one observation record. Silently skips duplicates
    (composite unique key on ts_action + primary_room + primary_action)."""
    actions = obs.get("actions") or []
    if not actions:
        return
    primary = actions[0]
    primary_room = primary.get("room", "?")
    primary_action = primary.get("action", "?")
    before = obs.get("before") or {}
    after = obs.get("after") or {}
    deltas = obs.get("deltas") or {}
    expected = obs.get("expected") or {}

    delta_indoor = None
    if primary_room in (deltas.get("indoor_f") or {}):
        try:
            delta_indoor = float(deltas["indoor_f"][primary_room])
        except (TypeError, ValueError):
            pass

    conn.execute(
        "INSERT OR IGNORE INTO observations ("
        "ts_observed, ts_action, age_min, n_actions, actions_json, "
        "primary_room, primary_action, primary_reason, mode_at_action, "
        "before_load_w, after_load_w, delta_load_w, "
        "expected_delta_w, delta_vs_expected_w, "
        "before_soc, after_soc, delta_soc, "
        "before_outdoor_f, after_outdoor_f, delta_indoor_f_room, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            obs.get("ts_observed") or "",
            actions[0].get("ts_action") or obs.get("ts_action") or "",
            float(obs.get("age_min") or 0.0),
            len(actions),
            json.dumps(actions),
            primary_room,
            primary_action,
            primary.get("reason"),
            primary.get("mode"),
            int(before.get("load_w") or 0),
            int(after.get("load_w") or 0),
            int(deltas.get("load_w") or 0),
            int(expected.get("total_expected_delta_w") or 0),
            int(obs.get("delta_vs_expected_w") or 0),
            (float(before.get("soc")) if before.get("soc") is not None else None),
            (float(after.get("soc")) if after.get("soc") is not None else None),
            (float(deltas.get("soc")) if deltas.get("soc") is not None else None),
            (float(before.get("outdoor_f")) if before.get("outdoor_f") is not None else None),
            (float(after.get("outdoor_f")) if after.get("outdoor_f") is not None else None),
            delta_indoor,
            json.dumps(obs, default=str),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------- backfill


def backfill(
    conn: sqlite3.Connection,
    decisions_path: pathlib.Path,
    observations_path: pathlib.Path,
) -> tuple[int, int]:
    """Read the JSONL files and insert every record. Idempotent -- primary
    keys make re-inserts a no-op. Returns (n_decisions, n_observations)."""
    n_dec = 0
    if decisions_path.is_file():
        for line in decisions_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            insert_decision(conn, rec)
            n_dec += 1
    n_obs = 0
    if observations_path.is_file():
        for line in observations_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obs = json.loads(line)
            except Exception:
                continue
            insert_observation(conn, obs)
            n_obs += 1
    return n_dec, n_obs


# ---------------------------------------------------------------- queries


def daily_action_summary(
    conn: sqlite3.Connection, day: dt.date | None = None,
) -> dict:
    """Aggregate today's (or given day's) observations by action.
    Returns:
      {
        "turn_off": {"n": 3, "total_saved_w": 4200, "avg_saved_w": 1400},
        "turn_on":  {"n": 2, "total_added_w": 2500, "avg_added_w": 1250},
        "net_load_delta_w": -1700,
        "day": "2026-07-03",
      }
    Positive `total_saved_w` = watts saved by turn_offs (delta_load_w was negative).
    Positive `total_added_w` = watts added by turn_ons."""
    d = day or dt.date.today()
    start = d.strftime("%Y-%m-%dT00:00:00")
    end = (d + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    rows = conn.execute(
        "SELECT primary_action, delta_load_w FROM observations "
        "WHERE ts_observed BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    off_deltas = [int(r["delta_load_w"]) for r in rows if r["primary_action"] == "turn_off"]
    on_deltas = [int(r["delta_load_w"]) for r in rows if r["primary_action"] == "turn_on"]
    total_saved = -sum(off_deltas) if off_deltas else 0
    total_added = sum(on_deltas) if on_deltas else 0
    return {
        "day": d.isoformat(),
        "turn_off": {
            "n": len(off_deltas),
            "total_saved_w": total_saved,
            "avg_saved_w": round(total_saved / len(off_deltas)) if off_deltas else 0,
        },
        "turn_on": {
            "n": len(on_deltas),
            "total_added_w": total_added,
            "avg_added_w": round(total_added / len(on_deltas)) if on_deltas else 0,
        },
        "net_load_delta_w": sum(off_deltas) + sum(on_deltas),
    }


def drift_by_room(
    conn: sqlite3.Connection,
    days_back: int = 14,
    min_samples: int = 3,
    drift_threshold_pct: float = 20.0,
) -> list[dict]:
    """Compare actual vs calibration-expected delta per room over the last
    N days, considering only isolated (n_actions=1) observations. Flags
    rooms where |avg_diff / avg_expected| > drift_threshold_pct.
    Returns rows sorted by |drift_pct| descending."""
    start = (dt.datetime.utcnow() - dt.timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    rows = conn.execute(
        "SELECT primary_room, primary_action, "
        "  COUNT(*) AS n, "
        "  AVG(delta_load_w) AS avg_actual, "
        "  AVG(expected_delta_w) AS avg_expected, "
        "  AVG(delta_vs_expected_w) AS avg_diff "
        "FROM observations "
        "WHERE ts_observed >= ? "
        "  AND n_actions = 1 "
        "GROUP BY primary_room, primary_action "
        "HAVING n >= ?",
        (start, min_samples),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        avg_exp = float(r["avg_expected"] or 0)
        avg_act = float(r["avg_actual"] or 0)
        avg_diff = float(r["avg_diff"] or 0)
        drift_pct = (
            100.0 * abs(avg_diff) / abs(avg_exp)
            if abs(avg_exp) > 1e-6
            else 0.0
        )
        out.append({
            "room": r["primary_room"],
            "action": r["primary_action"],
            "n": int(r["n"]),
            "avg_actual_w": round(avg_act),
            "avg_expected_w": round(avg_exp),
            "avg_diff_w": round(avg_diff),
            "drift_pct": round(drift_pct, 1),
            "drift_flag": drift_pct > drift_threshold_pct,
        })
    out.sort(key=lambda x: abs(x["drift_pct"]), reverse=True)
    return out


def warm_up_rates(
    conn: sqlite3.Connection,
    days_back: int = 30,
    min_samples: int = 3,
) -> list[dict]:
    """Compute per-room warm-up rate (°F/min) after isolated turn_offs.
    Uses only observations where age_min in [3, 15], indoor delta > 0,
    n_actions = 1. Returns list of {room, samples, f_per_min, avg_outdoor_f}."""
    start = (dt.datetime.utcnow() - dt.timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    rows = conn.execute(
        "SELECT primary_room AS room, "
        "  COUNT(*) AS n, "
        "  AVG(delta_indoor_f_room / age_min) AS f_per_min, "
        "  AVG(before_outdoor_f) AS avg_outdoor_f "
        "FROM observations "
        "WHERE ts_observed >= ? "
        "  AND n_actions = 1 "
        "  AND primary_action = 'turn_off' "
        "  AND delta_indoor_f_room IS NOT NULL "
        "  AND delta_indoor_f_room > 0 "
        "  AND age_min BETWEEN 3 AND 15 "
        "GROUP BY primary_room "
        "HAVING n >= ?",
        (start, min_samples),
    ).fetchall()
    return [
        {
            "room": r["room"],
            "samples": int(r["n"]),
            "f_per_min": round(float(r["f_per_min"] or 0), 3),
            "avg_outdoor_f": round(float(r["avg_outdoor_f"] or 0), 1),
        }
        for r in rows
    ]


def insert_daily_summary(
    conn: sqlite3.Connection, day: str, summary: dict, observations_summary: dict | None = None,
) -> None:
    """Idempotent write of one day's rollup. `day` is 'YYYY-MM-DD' (local).
    Existing rows for the same day are replaced (retrospective can be re-run)."""
    soc = summary.get("soc") or {}
    runtime = summary.get("runtime_min") or {}
    costs = summary.get("costs") or {}
    modes = summary.get("modes_min") or {}
    obs = observations_summary or (summary.get("observations") or {})

    per_room_cost = {
        r: {"kwh": c.get("kwh", 0), "usd": c.get("usd", 0)}
        for r, c in costs.items() if r != "_total" and isinstance(c, dict)
    }
    total_kwh = float((costs.get("_total") or {}).get("kwh") or 0)
    total_usd = float((costs.get("_total") or {}).get("usd") or 0)
    total_ac_min = int(sum(runtime.values()))
    off = (obs or {}).get("turn_off") or {}
    on = (obs or {}).get("turn_on") or {}

    conn.execute(
        "INSERT OR REPLACE INTO daily_summaries ("
        "day, generated_at, soc_start, soc_peak, soc_end, "
        "total_ac_min, total_kwh, total_usd, "
        "per_room_min_json, per_room_cost_json, modes_min_json, "
        "turn_off_savings_w, turn_on_load_w, n_actions, decision_count, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            day,
            dt.datetime.utcnow().isoformat(),
            soc.get("start"),
            soc.get("peak"),
            soc.get("end"),
            total_ac_min,
            total_kwh,
            total_usd,
            json.dumps(runtime),
            json.dumps(per_room_cost),
            json.dumps(modes),
            int(off.get("total_saved_w") or 0),
            int(on.get("total_added_w") or 0),
            len(summary.get("actions") or []),
            int(summary.get("decision_count") or 0),
            json.dumps(summary, default=str),
        ),
    )
    conn.commit()


def get_daily_summaries(
    conn: sqlite3.Connection, days_back: int = 7,
) -> list[dict]:
    """Return the last `days_back` daily_summaries rows, oldest first.
    Empty list if none. weekly.py's primary data source."""
    rows = conn.execute(
        "SELECT * FROM daily_summaries "
        "WHERE day >= date('now', ?) "
        "ORDER BY day ASC",
        (f"-{days_back} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def weekly_from_db(
    conn: sqlite3.Connection, days_back: int = 7,
) -> dict:
    """Aggregate the last N days of daily_summaries into a weekly report.
    Same shape as weekly.py's aggregate() so downstream code can consume
    either path. Returns {} if no daily rows found."""
    rows = get_daily_summaries(conn, days_back)
    if not rows:
        return {"n_days_with_data": 0}
    modes_min: dict[str, int] = {}
    runtime_min: dict[str, int] = {}
    cost_kwh: dict[str, float] = {}
    cost_usd: dict[str, float] = {}
    total_kwh = 0.0
    total_usd = 0.0
    soc_starts: list[float] = []
    soc_ends: list[float] = []
    soc_peaks: list[float] = []
    per_day: list[dict] = []

    for r in rows:
        modes = json.loads(r["modes_min_json"] or "{}")
        for m, mins in modes.items():
            modes_min[m] = modes_min.get(m, 0) + int(mins)
        prm = json.loads(r["per_room_min_json"] or "{}")
        for room, mins in prm.items():
            runtime_min[room] = runtime_min.get(room, 0) + int(mins)
        prc = json.loads(r["per_room_cost_json"] or "{}")
        for room, cost in prc.items():
            cost_kwh[room] = cost_kwh.get(room, 0.0) + float(cost.get("kwh") or 0)
            cost_usd[room] = cost_usd.get(room, 0.0) + float(cost.get("usd") or 0)
        total_kwh += float(r["total_kwh"] or 0)
        total_usd += float(r["total_usd"] or 0)
        if r["soc_start"] is not None:
            soc_starts.append(float(r["soc_start"]))
        if r["soc_end"] is not None:
            soc_ends.append(float(r["soc_end"]))
        if r["soc_peak"] is not None:
            soc_peaks.append(float(r["soc_peak"]))
        per_day.append({
            "day": r["day"],
            "soc_start": r["soc_start"],
            "soc_end": r["soc_end"],
            "soc_peak": r["soc_peak"],
            "ac_total_min": r["total_ac_min"],
            "kwh": r["total_kwh"],
            "usd": r["total_usd"],
        })

    return {
        "n_days_with_data": len(rows),
        "modes_min": modes_min,
        "runtime_min": runtime_min,
        "cost_kwh": {k: round(v, 2) for k, v in cost_kwh.items()},
        "cost_usd": {k: round(v, 2) for k, v in cost_usd.items()},
        "total_kwh": round(total_kwh, 2),
        "total_usd": round(total_usd, 2),
        "soc_min": min(soc_ends + soc_starts) if (soc_ends or soc_starts) else None,
        "soc_max": max(soc_peaks) if soc_peaks else None,
        "soc_avg_start": round(sum(soc_starts) / len(soc_starts), 1) if soc_starts else None,
        "soc_avg_end": round(sum(soc_ends) / len(soc_ends), 1) if soc_ends else None,
        "per_day": per_day,
    }


# ---------------------------------------------------------------- correlations


def mode_minutes_last_days(
    conn: sqlite3.Connection, days_back: int = 30,
) -> dict[str, float]:
    """Total minutes in each mode over the last N days, computed from the
    decisions table (one row per tick). Cross-checks the daily_summaries
    aggregate."""
    rows = conn.execute(
        "SELECT mode, COUNT(*) AS ticks FROM decisions "
        "WHERE ts_local >= datetime('now', ?) "
        "GROUP BY mode",
        (f"-{days_back} days",),
    ).fetchall()
    # Each tick = evaluation_interval_minutes worth of time. Caller can
    # scale by their interval; default assumption 5 min/tick.
    return {r["mode"]: int(r["ticks"]) * 5 for r in rows}


def outdoor_temp_vs_warmup(
    conn: sqlite3.Connection, room: str, days_back: int = 60,
) -> list[dict]:
    """Return {outdoor_f, warmup_f_per_min} pairs for one room's isolated
    turn_off observations. Feed into a scatter plot."""
    rows = conn.execute(
        "SELECT before_outdoor_f AS outdoor, "
        "  delta_indoor_f_room / age_min AS f_per_min "
        "FROM observations "
        "WHERE primary_room = ? AND primary_action = 'turn_off' "
        "  AND n_actions = 1 AND delta_indoor_f_room IS NOT NULL "
        "  AND delta_indoor_f_room > 0 AND age_min BETWEEN 3 AND 15 "
        "  AND ts_observed >= datetime('now', ?) "
        "ORDER BY outdoor",
        (room, f"-{days_back} days"),
    ).fetchall()
    return [
        {"outdoor_f": float(r["outdoor"]), "warmup_f_per_min": round(float(r["f_per_min"]), 3)}
        for r in rows if r["outdoor"] is not None
    ]


def cost_by_day(
    conn: sqlite3.Connection, days_back: int = 30,
) -> list[dict]:
    """Daily cost trend from daily_summaries. Sorted oldest first."""
    rows = conn.execute(
        "SELECT day, total_kwh, total_usd FROM daily_summaries "
        "WHERE day >= date('now', ?) "
        "ORDER BY day ASC",
        (f"-{days_back} days",),
    ).fetchall()
    return [
        {"day": r["day"], "kwh": float(r["total_kwh"] or 0), "usd": float(r["total_usd"] or 0)}
        for r in rows
    ]


def net_savings_by_day(
    conn: sqlite3.Connection, days_back: int = 30,
) -> list[dict]:
    """From daily_summaries, per-day: turn_off_savings_w (sum), turn_on_load_w,
    net = turn_off - turn_on (positive = net conservation)."""
    rows = conn.execute(
        "SELECT day, turn_off_savings_w AS off_w, turn_on_load_w AS on_w "
        "FROM daily_summaries WHERE day >= date('now', ?) "
        "ORDER BY day ASC",
        (f"-{days_back} days",),
    ).fetchall()
    out = []
    for r in rows:
        off_w = int(r["off_w"] or 0)
        on_w = int(r["on_w"] or 0)
        out.append({
            "day": r["day"],
            "turn_off_savings_w": off_w,
            "turn_on_load_w": on_w,
            "net_conserved_w": off_w - on_w,
        })
    return out


# ---------------------------------------------------------------- retention


def prune(
    conn: sqlite3.Connection,
    decisions_days: int = 90,
    observations_days: int | None = None,
    daily_summaries_days: int | None = None,
) -> dict[str, int]:
    """Delete rows older than the given retention. Set to None to keep forever.
    Returns count deleted per table. Runs VACUUM after to reclaim space
    (SQLite doesn't shrink files after DELETEs by itself)."""
    deleted: dict[str, int] = {}
    if decisions_days is not None:
        cur = conn.execute(
            "DELETE FROM decisions WHERE ts_local < datetime('now', ?)",
            (f"-{decisions_days} days",),
        )
        deleted["decisions"] = cur.rowcount
    if observations_days is not None:
        cur = conn.execute(
            "DELETE FROM observations WHERE ts_observed < datetime('now', ?)",
            (f"-{observations_days} days",),
        )
        deleted["observations"] = cur.rowcount
    if daily_summaries_days is not None:
        cur = conn.execute(
            "DELETE FROM daily_summaries WHERE day < date('now', ?)",
            (f"-{daily_summaries_days} days",),
        )
        deleted["daily_summaries"] = cur.rowcount
    conn.commit()
    if any(deleted.values()):
        conn.execute("VACUUM")
    return deleted


def rolling_stats_per_room_action(
    conn: sqlite3.Connection,
    n_samples: int = 20,
    isolated_only: bool = True,
) -> list[dict]:
    """Rolling stats over the LAST `n_samples` observations for each
    (room, action) pair. Includes both actual load delta and
    delta_vs_expected -- the latter is more useful for calibration
    drift (median filters the fridge/pump spike outliers).

    isolated_only=True limits to n_actions=1 samples, which strip out
    ticks where multiple ACs transitioned at once (harder to attribute).

    Returned rows include: n, mean/median/stddev/min/max for each of
    delta_load_w and delta_vs_expected_w.
    """
    import statistics as st

    # Pull raw values, sorted newest-first, capped at n_samples per group.
    # SQLite lacks MEDIAN / PERCENTILE, so we compute in Python. Volumes
    # are tiny (< a few hundred rows per room/action typically).
    filter_iso = "AND n_actions = 1" if isolated_only else ""
    rows = conn.execute(
        f"""
        SELECT primary_room, primary_action,
               delta_load_w, expected_delta_w, delta_vs_expected_w
        FROM observations
        WHERE 1=1 {filter_iso}
        ORDER BY primary_room, primary_action, ts_observed DESC
        """
    ).fetchall()

    from collections import defaultdict
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        k = (r["primary_room"], r["primary_action"])
        if len(grouped[k]) < n_samples:
            grouped[k].append(dict(r))

    out: list[dict] = []
    for (room, action), items in grouped.items():
        actuals = [int(x["delta_load_w"]) for x in items]
        expecteds = [int(x["expected_delta_w"]) for x in items]
        diffs = [int(x["delta_vs_expected_w"]) for x in items]

        def s(vals: list[int]) -> dict:
            if not vals:
                return {"n": 0}
            return {
                "n": len(vals),
                "mean": round(st.mean(vals)),
                "median": round(st.median(vals)),
                "stddev": round(st.pstdev(vals)) if len(vals) > 1 else 0,
                "min": min(vals),
                "max": max(vals),
            }

        out.append({
            "room": room,
            "action": action,
            "n_samples": len(items),
            "actual": s(actuals),
            "expected": s(expecteds),
            "diff": s(diffs),
        })

    # Sort: largest |median diff| first (most drifted), then by n desc.
    out.sort(key=lambda x: (-abs(x["diff"].get("median", 0)), -x["n_samples"]))
    return out


def rolling_stats_summary(
    conn: sqlite3.Connection,
    n_samples: int = 100,
    isolated_only: bool = True,
) -> dict:
    """Cross-room rolling stats over the last `n_samples` observations.
    Useful as an "overall noise level" indicator: if delta_vs_expected
    has median near 0 and stddev around 200-300W, calibration is
    reasonably tight against the fridge/pump noise floor."""
    import statistics as st
    filter_iso = "AND n_actions = 1" if isolated_only else ""
    rows = conn.execute(
        f"""
        SELECT delta_load_w, expected_delta_w, delta_vs_expected_w
        FROM observations
        WHERE 1=1 {filter_iso}
        ORDER BY ts_observed DESC
        LIMIT ?
        """,
        (n_samples,),
    ).fetchall()
    if not rows:
        return {"n": 0}
    actuals = [int(r["delta_load_w"]) for r in rows]
    diffs = [int(r["delta_vs_expected_w"]) for r in rows]

    def s(vals: list[int]) -> dict:
        return {
            "n": len(vals),
            "mean": round(st.mean(vals)),
            "median": round(st.median(vals)),
            "stddev": round(st.pstdev(vals)) if len(vals) > 1 else 0,
            "min": min(vals),
            "max": max(vals),
        }

    return {
        "n": len(rows),
        "isolated_only": isolated_only,
        "actual": s(actuals),
        "diff": s(diffs),
    }


def recent_observations(conn: sqlite3.Connection, n: int = 100) -> list[dict]:
    """Fetch the last N observation rows for display (web /observations)."""
    rows = conn.execute(
        "SELECT ts_observed, ts_action, age_min, n_actions, primary_room, "
        "  primary_action, primary_reason, mode_at_action, "
        "  before_load_w, after_load_w, delta_load_w, "
        "  expected_delta_w, delta_vs_expected_w, "
        "  before_soc, after_soc, delta_indoor_f_room "
        "FROM observations ORDER BY ts_observed DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- CLI backfill


def main() -> int:
    """Command-line backfill: python3 stats.py [decisions.log] [observations.jsonl]"""
    here = pathlib.Path(__file__).resolve().parent
    dpath = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path.home() / "smart_ac" / "decisions.log"
    opath = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path.home() / "smart_ac" / "observations.jsonl"
    with opened() as conn:
        n_dec, n_obs = backfill(conn, dpath, opath)
        total_dec = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        total_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        print(f"backfilled {n_dec} decision lines, {n_obs} observation lines from files")
        print(f"database now holds {total_dec} decisions, {total_obs} observations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
