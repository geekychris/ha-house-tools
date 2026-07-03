#!/usr/bin/env python3
"""
Fridge compressor duty-cycle monitor.

Approach: the fridge is the largest cyclic load in the "other" bucket
(live load - baseline - AC subtotal). When it's healthy, we see load
transitions of ~100-200W every few minutes (compressor on/off cycle).
When the door is left open OR the fridge has failed, we see either:
  - No cyclic transitions for several hours (fridge dead / unplugged)
  - Sustained elevated draw (compressor running continuously, e.g.
    because the door is ajar and it can't reach setpoint)

This monitor samples the "residual" (live load - baseline - AC_sum)
every 15 min via a systemd timer, keeps a rolling 4-hour window, and
alerts if:
  - Residual has been > NORMAL_RESIDUAL_MIN + FRIDGE_ELEVATED_MIN
    (i.e. compressor is on) for ≥ CONTINUOUS_ELEVATED_MIN (default 90m)
    → likely door ajar
  - OR residual variance in the last 4h is < TINY_VARIANCE_W without any
    high period (compressor never cycled) → fridge might be off

Uses HA REST for live sensor reads. State kept in /tmp/fridge_monitor.json.

Env:
  HA_URL, HA_TOKEN
  SMART_AC_CONFIG (default ../smart_ac/smart_ac.json) for calibration
  TELEGRAM_NOTIFY_ENTITY -- override the notify target

Config knobs (via env):
  BASELINE_W_FALLBACK  -- if calibration baseline unavailable (default 1000)
  FRIDGE_ELEVATED_MIN  -- watts above baseline to call "compressor on" (default 80)
  CONTINUOUS_ELEVATED_MIN -- alert threshold in minutes (default 90)
  TINY_VARIANCE_W      -- 4h stddev below this + no high period = alert (default 20)
  ALERT_COOLDOWN_MIN   -- default 240 (4h)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import statistics
import urllib.error
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
STATE_PATH = pathlib.Path("/tmp/fridge_monitor.json")
WINDOW_MIN = 240  # 4 hours

BASELINE_W_FALLBACK = float(os.environ.get("BASELINE_W_FALLBACK", "1000"))
FRIDGE_ELEVATED_MIN = float(os.environ.get("FRIDGE_ELEVATED_MIN", "80"))
CONTINUOUS_ELEVATED_MIN = int(os.environ.get("CONTINUOUS_ELEVATED_MIN", "90"))
TINY_VARIANCE_W = float(os.environ.get("TINY_VARIANCE_W", "20"))
ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "240"))

AC_ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]


def _cfg() -> dict:
    p = pathlib.Path(
        os.environ.get("SMART_AC_CONFIG", HERE.parent / "smart_ac" / "smart_ac.json")
    )
    return json.loads(p.read_text())


def ha_get(cfg: dict, path: str):
    req = urllib.request.Request(
        f"{cfg['ha_url'].rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"# ha_get {path}: {e}", file=sys.stderr)
        return None


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


def compute_residual(cfg: dict) -> tuple[float, dict]:
    """Return (residual_W, debug_dict). residual = live_load - baseline - sum(ON_AC_deltas)."""
    load_state = ha_get(cfg, f"/api/states/{cfg['load_sensor']}")
    calib = ha_get(cfg, "/api/states/sensor.smart_ac_calibration") or {}
    calib_results = (calib.get("attributes") or {}).get("results") or {}

    load = 0.0
    try:
        load = float(load_state["state"])
    except Exception:
        pass

    baseline = BASELINE_W_FALLBACK
    if "master" in calib_results:
        baseline = float(calib_results["master"].get("baseline_w", baseline))

    ac_sum = 0.0
    on_rooms: list[str] = []
    for room in AC_ROOMS:
        bs = ha_get(cfg, f"/api/states/input_boolean.ac_{room}")
        if bs and bs.get("state") == "on":
            on_rooms.append(room)
            if room in calib_results:
                ac_sum += float(calib_results[room].get("delta_w", 1000))
            else:
                ac_sum += 1000

    residual = load - baseline - ac_sum
    return residual, {
        "load": load,
        "baseline": baseline,
        "ac_sum": ac_sum,
        "on_rooms": on_rooms,
        "residual": residual,
    }


def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=2))


def main() -> int:
    if "HA_TOKEN" not in os.environ:
        sys.exit("HA_TOKEN required")

    cfg = _cfg()
    now = dt.datetime.now().astimezone()

    residual, dbg = compute_residual(cfg)

    state = load_state()
    samples: list[dict] = state.get("samples", [])
    samples.append({"t": now.isoformat(), "w": residual})

    # Trim samples older than WINDOW_MIN
    cutoff = (now - dt.timedelta(minutes=WINDOW_MIN)).isoformat()
    samples = [s for s in samples if s["t"] >= cutoff]

    values = [s["w"] for s in samples]
    print(f"# {now:%H:%M} residual={residual:.0f}W (load={dbg['load']:.0f} - "
          f"baseline={dbg['baseline']:.0f} - AC={dbg['ac_sum']:.0f}); "
          f"samples={len(samples)}")

    last_alert = state.get("last_alert_at")
    warnings: list[str] = []

    # Elevated draw check: how many recent samples were > baseline+threshold?
    elevated = [s for s in samples if s["w"] > FRIDGE_ELEVATED_MIN]
    if elevated and samples:
        # Rough: assume samples are ~15 min apart, and continuously elevated
        # means the last N samples (N = CONTINUOUS_ELEVATED_MIN / 15) were all elevated.
        needed = max(1, CONTINUOUS_ELEVATED_MIN // 15)
        recent = samples[-needed:]
        if len(recent) == needed and all(s["w"] > FRIDGE_ELEVATED_MIN for s in recent):
            warnings.append(
                f"Residual has been elevated (> baseline+{FRIDGE_ELEVATED_MIN:.0f}W) "
                f"for ≥{CONTINUOUS_ELEVATED_MIN} min. Possible: fridge door ajar, "
                f"compressor stuck on, extra always-on load added."
            )

    # Tiny-variance check: fridge might be off
    if len(values) >= 8:
        sd = statistics.pstdev(values)
        max_v = max(values)
        if sd < TINY_VARIANCE_W and max_v < FRIDGE_ELEVATED_MIN:
            warnings.append(
                f"Residual is unusually flat (stddev {sd:.0f}W, max {max_v:.0f}W) "
                f"over the last {WINDOW_MIN} min. Fridge compressor never cycled -- "
                f"appliance may be off or dead."
            )

    new_state = {
        "samples": samples,
        "last_check_at": now.isoformat(),
        "last_alert_at": last_alert,
    }

    if not warnings:
        save_state(new_state)
        return 0

    if last_alert:
        try:
            elapsed = (now - dt.datetime.fromisoformat(last_alert)).total_seconds() / 60
            if elapsed < ALERT_COOLDOWN_MIN:
                print(f"# suppress alert: cooldown ({elapsed:.0f}m < {ALERT_COOLDOWN_MIN}m)",
                      file=sys.stderr)
                save_state(new_state)
                return 0
        except Exception:
            pass

    notify_entity = os.environ.get(
        "TELEGRAM_NOTIFY_ENTITY",
        "notify.living_room_homeassistantxyz11_chris_collins",
    )
    msg = "Fridge / appliance monitor alert\n" + "\n".join("- " + w for w in warnings) + (
        f"\n\n(residual now: {residual:.0f}W; load {dbg['load']:.0f}W - "
        f"baseline {dbg['baseline']:.0f}W - AC {dbg['ac_sum']:.0f}W; "
        f"ACs on: {', '.join(dbg['on_rooms']) or 'none'})"
    )
    try:
        ha_call(cfg, "notify", "send_message",
                {"entity_id": notify_entity, "message": msg})
        new_state["last_alert_at"] = now.isoformat()
        print("# alert sent", file=sys.stderr)
    except Exception as e:
        print(f"# alert send failed: {e}", file=sys.stderr)

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
