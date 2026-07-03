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
