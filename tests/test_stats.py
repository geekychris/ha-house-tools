"""Smoke tests for smart_ac/stats.py."""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "smart_ac"))

import stats  # noqa: E402


def test_init_and_insert_decision(tmp_path: pathlib.Path):
    stats.DEFAULT_DB_PATH = tmp_path / "stats.sqlite3"
    with stats.opened(tmp_path / "stats.sqlite3") as conn:
        rec = {
            "ts": "2026-07-03T18:00:00+00:00",
            "mode": "SURPLUS",
            "soc": 100.0,
            "battery_power_w": 500,
            "pv_power_w": 4200,
            "load_w": 3700,
            "outdoor_f": 92.4,
            "indoor_f": {"living": 78.4, "master": 77.5},
            "ac_on": {"living": True, "master": True},
            "target": {"living": True, "master": True, "kyle": False},
            "reasons": {"living": "SURPLUS"},
            "actions": ["living:turn_on"],
            "enabled": True,
            "unoccupied": False,
        }
        stats.insert_decision(conn, rec)
        # Re-insert = idempotent (PK on ts).
        stats.insert_decision(conn, rec)
        n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        assert n == 1


def test_insert_and_query_observation(tmp_path: pathlib.Path):
    with stats.opened(tmp_path / "stats.sqlite3") as conn:
        obs = {
            "ts_observed": "2026-07-03T18:05:00+00:00",
            "age_min": 5.0,
            "actions": [{
                "room": "living",
                "action": "turn_off",
                "reason": "DEFICIT",
                "mode": "DEFICIT",
                "ts_action": "2026-07-03T18:00:00+00:00",
            }],
            "before": {"load_w": 3800, "soc": 76.0, "outdoor_f": 91.0,
                       "indoor_f": {"living": 79.4}},
            "after": {"load_w": 2600, "soc": 76.1, "outdoor_f": 91.2,
                      "indoor_f": {"living": 79.7}},
            "deltas": {"load_w": -1200, "soc": 0.1,
                       "outdoor_f": 0.2,
                       "indoor_f": {"living": 0.3}},
            "expected": {"total_expected_delta_w": -1252},
            "delta_vs_expected_w": 52,
        }
        stats.insert_observation(conn, obs)
        stats.insert_observation(conn, obs)  # dup by unique key
        n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert n == 1

        summary = stats.daily_action_summary(
            conn, day=__import__("datetime").date(2026, 7, 3)
        )
        assert summary["turn_off"]["n"] == 1
        assert summary["turn_off"]["total_saved_w"] == 1200


def test_backfill_from_jsonl(tmp_path: pathlib.Path):
    dpath = tmp_path / "decisions.log"
    opath = tmp_path / "observations.jsonl"
    dpath.write_text(json.dumps({
        "ts": "2026-07-03T10:00:00+00:00", "mode": "ON_TRACK",
        "soc": 80, "battery_power_w": 100, "pv_power_w": 3000,
        "load_w": 3100, "outdoor_f": 85, "indoor_f": {}, "ac_on": {},
        "actions": [], "reasons": {}, "enabled": True,
    }) + "\n")
    opath.write_text(json.dumps({
        "ts_observed": "2026-07-03T10:05:00+00:00", "age_min": 5,
        "actions": [{"room": "guest", "action": "turn_on", "ts_action": "x"}],
        "before": {"load_w": 3100, "soc": 80, "outdoor_f": 85, "indoor_f": {}},
        "after": {"load_w": 4050, "soc": 80.2, "outdoor_f": 85, "indoor_f": {}},
        "deltas": {"load_w": 950, "soc": 0.2, "outdoor_f": 0, "indoor_f": {}},
        "expected": {"total_expected_delta_w": 973},
        "delta_vs_expected_w": -23,
    }) + "\n")
    with stats.opened(tmp_path / "stats.sqlite3") as conn:
        n_d, n_o = stats.backfill(conn, dpath, opath)
        assert n_d == 1
        assert n_o == 1
