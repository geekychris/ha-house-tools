#!/usr/bin/env python3
"""
Solar underperformance anomaly detector.

Compares live PV production to what Open-Meteo's forecast + rolling
median expect. Fires a Telegram alert when live PV is materially below
expected AND the sun should be up.

Runs as a systemd timer (see weather-anomaly.timer). Every 15 min:
  1. Read live PV from sensor.<inverter>_pv_total_power.
  2. Read expected PV from sensor.weather_expected_pv_now (published by
     openmeteo.py).
  3. Read cloud cover from sensor.weather_cloud_now.
  4. If cloud cover < CLOUD_CLEAR_PCT (clear sky assumption)
     AND expected PV > MIN_EXPECTED_W (sun should be up)
     AND actual/expected < ANOMALY_RATIO
     for at least ANOMALY_STICKY_TICKS in a row → alert Telegram.
  5. Persist state in /tmp/pv_anomaly.json so we don't re-alert every
     tick during the same event.

Alert cooldown: won't refire within ALERT_COOLDOWN_MIN.

Notes / limits:
  - The expected-PV model in openmeteo.py is intentionally simple
    (radiation × panel_kw × efficiency). It's fine for ratio-based
    anomaly detection but shouldn't be trusted as an absolute PV
    forecast.
  - Panel soiling or shading is best-detected here (a clear day with
    output steadily below forecast).
  - We deliberately do NOT alert on cloudy days -- cloud cover
    naturally suppresses output; that's expected, not an anomaly.

Env:
  HA_URL, HA_TOKEN
  SMART_AC_CONFIG (default ../smart_ac/smart_ac.json) -- to read the PV sensor entity id
  TELEGRAM_NOTIFY_ENTITY -- override the default notify target
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
STATE_PATH = pathlib.Path("/tmp/pv_anomaly.json")

# Thresholds -- generous defaults, tune via env overrides if noisy.
CLOUD_CLEAR_PCT = float(os.environ.get("CLOUD_CLEAR_PCT", "40"))
MIN_EXPECTED_W = float(os.environ.get("MIN_EXPECTED_W", "1500"))
ANOMALY_RATIO = float(os.environ.get("ANOMALY_RATIO", "0.5"))
ANOMALY_STICKY_TICKS = int(os.environ.get("ANOMALY_STICKY_TICKS", "2"))
ALERT_COOLDOWN_MIN = int(os.environ.get("ALERT_COOLDOWN_MIN", "120"))


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
    except urllib.error.HTTPError as e:
        print(f"# ha_get {path}: HTTP {e.code}", file=sys.stderr)
        return None
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


def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=2))


def _num(s) -> float | None:
    try:
        return float(s.get("state"))
    except Exception:
        return None


def main() -> int:
    if "HA_TOKEN" not in os.environ:
        sys.exit("HA_TOKEN env var required")

    cfg = _cfg()
    now = dt.datetime.now().astimezone()

    live = ha_get(cfg, f"/api/states/{cfg['pv_power_sensor']}")
    expected = ha_get(cfg, "/api/states/sensor.weather_expected_pv_now")
    cloud = ha_get(cfg, "/api/states/sensor.weather_cloud_now")

    live_w = _num(live) if live else None
    expected_w = _num(expected) if expected else None
    cloud_pct = _num(cloud) if cloud else None

    if live_w is None or expected_w is None:
        print(f"# missing signals: live={live_w} expected={expected_w}. exiting.",
              file=sys.stderr)
        return 0

    state = load_state()
    streak = int(state.get("streak", 0))
    last_alert = state.get("last_alert_at")

    # Decide if this tick is "anomalous".
    is_anomaly = False
    reason = ""
    if expected_w < MIN_EXPECTED_W:
        reason = f"expected PV {expected_w:.0f}W below threshold {MIN_EXPECTED_W:.0f}W -- sun is low"
    elif cloud_pct is not None and cloud_pct >= CLOUD_CLEAR_PCT:
        reason = f"cloud cover {cloud_pct:.0f}% >= {CLOUD_CLEAR_PCT:.0f}% -- not a clear-sky test"
    else:
        ratio = live_w / max(expected_w, 1)
        if ratio < ANOMALY_RATIO:
            is_anomaly = True
            reason = f"ratio {ratio:.2f} < {ANOMALY_RATIO:.2f} (live {live_w:.0f}W / expected {expected_w:.0f}W, cloud {cloud_pct}%)"
        else:
            reason = f"ratio {ratio:.2f} >= {ANOMALY_RATIO:.2f} -- ok"

    if is_anomaly:
        streak += 1
    else:
        streak = 0

    print(f"# {now:%H:%M} live={live_w:.0f}W expected={expected_w:.0f}W "
          f"cloud={cloud_pct}% streak={streak} -- {reason}")

    save_state({
        "streak": streak,
        "last_check_at": now.isoformat(),
        "last_alert_at": last_alert,
    })

    if not is_anomaly or streak < ANOMALY_STICKY_TICKS:
        return 0

    # Check cooldown before alerting.
    if last_alert:
        try:
            last = dt.datetime.fromisoformat(last_alert)
            elapsed_min = (now - last).total_seconds() / 60
            if elapsed_min < ALERT_COOLDOWN_MIN:
                print(f"# skip alert: cooldown ({elapsed_min:.0f}m < {ALERT_COOLDOWN_MIN}m)",
                      file=sys.stderr)
                return 0
        except Exception:
            pass

    notify_entity = os.environ.get(
        "TELEGRAM_NOTIFY_ENTITY",
        "notify.living_room_homeassistantxyz11_chris_collins",
    )
    ratio = live_w / max(expected_w, 1)
    message = (
        "Solar underperformance alert\n"
        f"Live PV: {live_w:.0f} W (expected ~{expected_w:.0f} W, ratio {ratio:.2f})\n"
        f"Cloud cover: {cloud_pct}% -- clear sky, so this isn't weather\n"
        f"Streak: {streak} consecutive checks\n"
        "Possible causes: soiling, partial shading, a tripped string, "
        "inverter alarm. Check the array."
    )
    try:
        ha_call(cfg, "notify", "send_message",
                {"entity_id": notify_entity, "message": message})
        save_state({
            "streak": streak,
            "last_check_at": now.isoformat(),
            "last_alert_at": now.isoformat(),
        })
        print("# alert sent", file=sys.stderr)
    except Exception as e:
        print(f"# alert send failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
