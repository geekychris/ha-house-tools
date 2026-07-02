#!/usr/bin/env python3
"""
Per-AC power calibration (v2 -- isolated-AC method, plateau detection).

Approach:
  1. Turn ALL ACs OFF.  Poll the EG4 load sensor until it plateaus
     (rolling stddev < STABILITY_SIGMA_W).  Record as the ALL-OFF baseline.
  2. For each AC in order:
     a. Turn ONLY that AC on (others stay off).
     b. Poll load until (i) stddev < STABILITY_SIGMA_W AND
        (ii) mean - baseline >= MIN_ON_DELTA_W  (or MAX_ON_SEC elapses).
        This is the "running" reading; delta = running - baseline.
     c. Turn that AC off.  Poll until load returns near baseline (or
        MAX_RECOVER_SEC elapses).  This is the recovery.
     d. If recovery landed cleanly near baseline, adopt it as the new
        baseline (tracks slow drift in fridge / other cycles over the run).

Why not v1's method:
  - v1 turned one AC on and measured against the load with the OTHER FIVE
    still cycling independently.  Their compressor timing bled into the
    delta.  Observed: living-room "delta -474 W" -- negative deltas are
    physically impossible for an AC turning on.
  - v1 used a fixed 60 s window without checking whether the load sensor
    actually reported enough fresh readings in that window.  If EG4 pushes
    updates every 30 s, 60 s == two samples, not a stable mean.
  - v1 accepted whatever delta came out.  v2 flags implausible values.

Expectation (from user):  each AC ~0.5-1.5 kW, all six ~5.5-6.5 kW total.

Duration:  a run typically takes 20-30 min.  ACs that plateau slowly, or
if the household load is noisy from other appliances, can push closer to
the ~60 min worst case.

Env-var overrides (all optional):
  POLL_INTERVAL_SEC        (default 2.0)   -- how often to hit /api/states
  WINDOW_SAMPLES           (default 20)    -- rolling window for stddev/mean
  STABILITY_SIGMA_W        (default 60)    -- window stddev below this = "flat"
  MIN_ON_DELTA_W           (default 250)   -- min plausible AC-on step
  INITIAL_SETTLE_MAX_SEC   (default 300)
  MAX_ON_SEC               (default 300)
  MAX_RECOVER_SEC          (default 240)
  EXPECTED_AC_W_MIN        (default 300)
  EXPECTED_AC_W_MAX        (default 1600)
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.request
from collections import deque


HERE = pathlib.Path(__file__).resolve().parent

# All prints must reach the log immediately; the /calibrate web page tails it.
print = functools.partial(print, flush=True)  # noqa: A001


POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "2.0"))
WINDOW_SAMPLES = int(os.environ.get("WINDOW_SAMPLES", "20"))
STABILITY_SIGMA_W = float(os.environ.get("STABILITY_SIGMA_W", "60"))
MIN_ON_DELTA_W = float(os.environ.get("MIN_ON_DELTA_W", "250"))
INITIAL_SETTLE_MAX_SEC = int(os.environ.get("INITIAL_SETTLE_MAX_SEC", "300"))
MAX_ON_SEC = int(os.environ.get("MAX_ON_SEC", "300"))
MAX_RECOVER_SEC = int(os.environ.get("MAX_RECOVER_SEC", "240"))
EXPECTED_AC_W_MIN = float(os.environ.get("EXPECTED_AC_W_MIN", "300"))
EXPECTED_AC_W_MAX = float(os.environ.get("EXPECTED_AC_W_MAX", "1600"))


def load_config() -> dict:
    p = pathlib.Path(os.environ.get("SMART_AC_CONFIG", HERE / "smart_ac.json"))
    return json.loads(p.read_text())


def now_hms() -> str:
    return dt.datetime.now().astimezone().strftime("%H:%M:%S")


def ha_get(cfg: dict, path: str):
    req = urllib.request.Request(
        f"{cfg['ha_url'].rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def ha_call(cfg: dict, domain: str, service: str, body: dict) -> None:
    url = f"{cfg['ha_url'].rstrip('/')}/api/services/{domain}/{service}"
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


def flip(cfg: dict, room: str, on: bool) -> None:
    svc = "turn_on" if on else "turn_off"
    ha_call(cfg, "input_boolean", svc, {"entity_id": f"input_boolean.ac_{room}"})


def poll_load(cfg: dict) -> tuple[float, str]:
    d = ha_get(cfg, f"/api/states/{cfg['load_sensor']}")
    return float(d["state"]), d.get("last_updated", "")


def sensor_freshness_check(cfg: dict, seconds: int = 20) -> None:
    """Quick sanity check: how often does the EG4 load sensor actually update?"""
    print(f"[{now_hms()}] Load-sensor freshness check ({seconds}s window)")
    updates: set[str] = set()
    values: list[float] = []
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            w, u = poll_load(cfg)
            updates.add(u)
            values.append(w)
        except Exception as e:
            print(f"  poll err: {e}")
        time.sleep(POLL_INTERVAL_SEC)
    rate = len(updates) / seconds if seconds else 0
    print(
        f"[{now_hms()}]   sensor {cfg['load_sensor']}: "
        f"{len(updates)} fresh updates in {seconds}s ({rate:.2f}/s), "
        f"{len(values)} polls, min={min(values):.0f}W max={max(values):.0f}W"
    )
    if rate < 0.1:
        print(
            f"[{now_hms()}]   WARN: sensor updates < 1 per 10s. Consider extending "
            f"MAX_ON_SEC/MAX_RECOVER_SEC via env vars, or the plateau detection "
            f"may time out before the sensor catches up."
        )


def wait_for_plateau(
    cfg: dict,
    label: str,
    max_sec: int,
    reference_w: float | None = None,
    step_sign: int = 0,
    min_step_w: float = 0.0,
) -> tuple[float, float, bool, int]:
    """
    Poll the load sensor until we have a stable window.

    Args:
        label: short tag for status prints.
        max_sec: give up and return current mean after this many seconds.
        reference_w: baseline to measure step against (None for initial baseline
            case, where we just wait for stddev to settle).
        step_sign: +1 if we expect load to go UP (AC turned on),
                   -1 if we expect load to go DOWN (recovery after off),
                    0 if we don't care about direction (initial baseline).
        min_step_w: how much load must have moved from reference to accept.

    Returns:
        (mean_w, seconds_taken, stable, samples).
        stable=True iff plateau + step criteria were met before max_sec.
    """
    window: deque[float] = deque(maxlen=WINDOW_SAMPLES)
    started = time.monotonic()
    last_status_at = started
    samples = 0

    while True:
        elapsed = time.monotonic() - started
        try:
            w, _ = poll_load(cfg)
            window.append(w)
            samples += 1
        except Exception as e:
            print(f"[{now_hms()}]   {label}: poll err: {e}")

        # Periodic status so a slow plateau doesn't look hung
        if time.monotonic() - last_status_at >= 15 and window:
            mean_w = statistics.mean(window)
            sd = statistics.pstdev(window) if len(window) >= 2 else 0
            if reference_w is not None:
                step = (mean_w - reference_w) * (step_sign or 1)
                step_str = f" step={step:+.0f}W (need >={min_step_w:.0f}W)"
            else:
                step_str = ""
            print(
                f"[{now_hms()}]   {label}: t={elapsed:.0f}s "
                f"mean={mean_w:.0f}W stddev={sd:.0f}W samples={samples}{step_str}"
            )
            last_status_at = time.monotonic()

        # Check plateau
        if len(window) >= WINDOW_SAMPLES:
            mean_w = statistics.mean(window)
            sd = statistics.pstdev(window)
            if sd < STABILITY_SIGMA_W:
                if reference_w is None:
                    # Initial baseline: any stable plateau is good
                    return mean_w, elapsed, True, samples
                step = (mean_w - reference_w) * (step_sign or 1)
                if step >= min_step_w:
                    return mean_w, elapsed, True, samples

        if elapsed >= max_sec:
            mean_w = statistics.mean(window) if window else 0.0
            return mean_w, elapsed, False, samples

        time.sleep(POLL_INTERVAL_SEC)


def classify_delta(delta_w: float) -> str:
    if delta_w < 0:
        return f"NEGATIVE ({delta_w:.0f}W) -- impossible, treat as noise; rerun"
    if delta_w < EXPECTED_AC_W_MIN:
        return (
            f"BELOW MIN ({delta_w:.0f}W < {EXPECTED_AC_W_MIN:.0f}W) -- AC may not "
            f"have engaged (Alexa routine failed? compressor delay?)"
        )
    if delta_w > EXPECTED_AC_W_MAX:
        return (
            f"ABOVE MAX ({delta_w:.0f}W > {EXPECTED_AC_W_MAX:.0f}W) -- "
            f"contamination or fridge kicked in mid-measurement"
        )
    return "ok"


def main() -> int:
    if "HA_TOKEN" not in os.environ:
        sys.exit("HA_TOKEN required")

    cfg = load_config()
    rooms = sorted(set(cfg["night_min_acs"]) | set(cfg["day_priority"]))
    started_at = dt.datetime.now().astimezone()

    est_min_worst = round(
        (
            INITIAL_SETTLE_MAX_SEC
            + len(rooms) * (MAX_ON_SEC + MAX_RECOVER_SEC)
            + 60  # freshness check + slack
        )
        / 60
    )

    print(f"[{now_hms()}] Calibration v2 (isolated-AC + plateau detection)")
    print(f"[{now_hms()}] Rooms in order: {', '.join(rooms)}")
    print(
        f"[{now_hms()}] poll={POLL_INTERVAL_SEC}s window={WINDOW_SAMPLES} "
        f"stability<{STABILITY_SIGMA_W:.0f}W min_step>{MIN_ON_DELTA_W:.0f}W"
    )
    print(
        f"[{now_hms()}] settle_max={INITIAL_SETTLE_MAX_SEC}s "
        f"on_max={MAX_ON_SEC}s recover_max={MAX_RECOVER_SEC}s -- "
        f"worst-case total ~{est_min_worst} min"
    )
    print()

    sensor_freshness_check(cfg)
    print()

    # ── Step 1: all off, get baseline
    print(f"[{now_hms()}] STEP 1: Turning ALL ACs OFF ({', '.join(rooms)})")
    for r in rooms:
        flip(cfg, r, False)

    baseline_w, took, stable, n = wait_for_plateau(
        cfg,
        label="ALL-OFF baseline",
        max_sec=INITIAL_SETTLE_MAX_SEC,
    )
    print(
        f"[{now_hms()}] Baseline: {baseline_w:.0f}W  "
        f"(stable={stable}, {took:.0f}s, {n} samples)"
    )
    if not stable:
        print(
            f"[{now_hms()}]   WARN: baseline did not settle within "
            f"{INITIAL_SETTLE_MAX_SEC}s. Household load is noisy; results below "
            f"may have wider error bars."
        )

    # ── Step 2: each AC in turn
    results: dict[str, dict] = {}
    for room in rooms:
        print()
        print(f"[{now_hms()}] === {room} ===  (baseline {baseline_w:.0f}W)")

        try:
            flip(cfg, room, True)
        except Exception as e:
            print(f"[{now_hms()}]   turn_on failed: {e}")
            results[room] = {"error": f"turn_on: {e}"}
            continue

        on_w, on_took, on_stable, on_n = wait_for_plateau(
            cfg,
            label=f"{room} ON",
            max_sec=MAX_ON_SEC,
            reference_w=baseline_w,
            step_sign=+1,
            min_step_w=MIN_ON_DELTA_W,
        )
        delta = on_w - baseline_w
        note = classify_delta(delta)
        print(
            f"[{now_hms()}]   ON plateau: {on_w:.0f}W "
            f"(stable={on_stable}, {on_took:.0f}s, {on_n} samples)  "
            f"delta={delta:+.0f}W  [{note}]"
        )

        try:
            flip(cfg, room, False)
        except Exception as e:
            print(f"[{now_hms()}]   turn_off failed: {e}")

        rec_w, rec_took, rec_stable, rec_n = wait_for_plateau(
            cfg,
            label=f"{room} recover",
            max_sec=MAX_RECOVER_SEC,
            reference_w=on_w,
            step_sign=-1,
            min_step_w=MIN_ON_DELTA_W,
        )
        print(
            f"[{now_hms()}]   OFF recovery: {rec_w:.0f}W "
            f"(stable={rec_stable}, {rec_took:.0f}s, {rec_n} samples)"
        )

        # If recovery landed cleanly near the last baseline, adopt it -- lets us
        # track slow drift over the ~30 min run. Otherwise leave baseline alone.
        gap = rec_w - baseline_w
        adopted_new_baseline = False
        if rec_stable and abs(gap) < STABILITY_SIGMA_W * 2:
            baseline_w = rec_w
            adopted_new_baseline = True
        elif rec_stable:
            print(
                f"[{now_hms()}]   note: recovery {rec_w:.0f}W drifted {gap:+.0f}W "
                f"from prior baseline -- keeping prior baseline; something else "
                f"in the house may have swung."
            )

        results[room] = {
            "baseline_w": round(baseline_w),  # updated above if recovery adopted
            "running_w": round(on_w),
            "delta_w": round(delta),
            "on_stable": on_stable,
            "on_seconds": round(on_took),
            "recovery_w": round(rec_w),
            "recovery_stable": rec_stable,
            "recovery_seconds": round(rec_took),
            "note": note,
        }

    # ── Summary
    print()
    print(f"[{now_hms()}] === Summary ===")
    print(f"  {'Room':<8} {'Baseline':>10} {'Running':>10} {'Delta':>10}   Note")
    total_positive = 0
    for r, info in results.items():
        if "error" in info:
            print(f"  {r:<8} ERROR {info['error']}")
            continue
        d = info["delta_w"]
        if d > 0:
            total_positive += d
        print(
            f"  {r:<8} {info['baseline_w']:>8}W {info['running_w']:>8}W "
            f"{d:>+8}W   {info['note']}"
        )
    print(f"  {'-- sum of positive deltas:':<32} {total_positive:>+8}W "
          f"(user expects ~5500-6500W with all 6 on and cooling)")

    finished_at = dt.datetime.now().astimezone()
    elapsed_min = (finished_at - started_at).total_seconds() / 60
    print(f"[{now_hms()}] Elapsed: {elapsed_min:.1f} min")

    ha_set_state(
        cfg,
        "sensor.smart_ac_calibration",
        "measured",
        {
            "friendly_name": "Smart AC calibration",
            "icon": "mdi:speedometer",
            "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": "v2 isolated-AC + plateau detection",
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "window_samples": WINDOW_SAMPLES,
            "stability_sigma_w": STABILITY_SIGMA_W,
            "min_on_delta_w": MIN_ON_DELTA_W,
            "results": results,
            "sum_positive_delta_w": total_positive,
        },
    )
    print(f"[{now_hms()}] sensor.smart_ac_calibration updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
