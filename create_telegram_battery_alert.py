#!/usr/bin/env python3
"""
Create / overwrite the "low solar + high battery draw during daylight" alert.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

Sends a Telegram message when, during a window where solar SHOULD be
producing well, the house is instead drawing meaningfully from the
battery and PV production is poor. Catches the failure mode the user
described: "we have been using more power from the batteries and don't
seem to have very good solar production and it's nowhere near the end
of a sunny day."

This is the LEVEL 1 alert -- static thresholds, no historical comparison.
Good enough to flag obvious problems (panel covered in dust, MPPT
fault, inverter throttling, unexpected midday consumption spike). For
more accuracy (adapts to season / cloud cover), upgrade later to
Level 2: compare actual vs a `statistics`-platform rolling-14-day
median of pv_total_power at this hour, OR install the Solcast HACS
integration and compare against its forecast.

Trigger logic (all must hold for 15 minutes):
  - battery_power < -ALERT_BATTERY_DRAW_W  (drawing from battery, i.e.
    EG4 reports negative power when discharging)
  - pv_total_power < ALERT_SOLAR_MIN_W      (solar production weak)
  - hour of day in [ALERT_HOUR_START, ALERT_HOUR_END)  (daylight window
    where solar SHOULD be strong)

`mode: single` + `max_exceeded: silent` means the alert only fires once
per matching window; once conditions clear and re-meet, it fires again.

----------------------------------------------------------------------------
TUNING
----------------------------------------------------------------------------

Edit the constants below and re-run the script -- it overwrites in place.
Reasonable starting values for a ~15 kW EG4 with healthy panels:

  ALERT_BATTERY_DRAW_W = 500    # >500W discharge while sun should be up
  ALERT_SOLAR_MIN_W    = 1000   # solar producing <1 kW is "weak"
  ALERT_HOUR_START     = 10     # 10 AM local
  ALERT_HOUR_END       = 16     # 4 PM local
  ALERT_SUSTAINED_MIN  = 15     # must hold for 15 min (filters clouds)

Note the hour comparison uses HA's local timezone (set in HA's
configuration). The trigger uses `for: 00:15:00` so transient cloud
shadow won't fire it.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    [TELEGRAM_NOTIFY_ENTITY=notify.<your-entity>] \\
    python3 create_telegram_battery_alert.py

Prereq: the Telegram Bot integration must be set up via HA's UI (or by
running `setup_telegram_bot.py`), which creates a `notify.telegram_*`
entity. The alert calls `notify.send_message` with that entity as the
target. See setup_telegram_bot.py for the architecture note about the
HA 2026 YAML-to-config-flow migration.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_battery_low_solar_alert"

# Notify entity created by the Telegram Bot integration's allowed-chat
# subentry. Override via env if your bot name / chat name differs.
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Thresholds (edit and re-run to retune).
ALERT_BATTERY_DRAW_W = 500
ALERT_SOLAR_MIN_W = 1000
ALERT_HOUR_START = 10
ALERT_HOUR_END = 16
ALERT_SUSTAINED_MIN = 15

INVERTER = "sensor.sna_us_15k_53562j0683"
S_SOLAR = f"{INVERTER}_pv_total_power"
S_SOC = f"{INVERTER}_state_of_charge"
S_BATT_PWR = f"{INVERTER}_battery_power"
S_HOME = f"{INVERTER}_consumption_power"


MESSAGE_TEMPLATE = f"""\
[ALERT] Low solar, drawing from battery

Solar     {{{{ states('{S_SOLAR}') | float(0) | round(0) | int }}}} W \
(threshold {ALERT_SOLAR_MIN_W} W)
Battery   {{{{ states('{S_SOC}') | float(0) | round(0) | int }}}} %, \
discharging at {{{{ states('{S_BATT_PWR}') | float(0) | round(0) | int | abs }}}} W \
(threshold {ALERT_BATTERY_DRAW_W} W)
Home      {{{{ states('{S_HOME}') | float(0) | round(0) | int }}}} W

This has held for {ALERT_SUSTAINED_MIN}+ minutes during the {ALERT_HOUR_START:02d}:00 \
- {ALERT_HOUR_END:02d}:00 daylight window. Check for panel obstruction, \
inverter fault, or unexpected midday load."""


# HA trigger: numeric_state on battery_power going below -ALERT_BATTERY_DRAW_W,
# sustained for 15 minutes. Conditions filter for the daylight hour window
# and for solar being weak at the moment the trigger fires.
AUTOMATION_CONFIG = {
    "alias": "Telegram battery/solar alert",
    "description": (
        f"Alert when battery is being drawn at >{ALERT_BATTERY_DRAW_W} W AND "
        f"solar is <{ALERT_SOLAR_MIN_W} W during the "
        f"{ALERT_HOUR_START:02d}:00-{ALERT_HOUR_END:02d}:00 window, sustained "
        f"for {ALERT_SUSTAINED_MIN} min. Managed by create_telegram_battery_alert.py."
    ),
    "mode": "single",
    "max_exceeded": "silent",
    "triggers": [
        {
            "trigger": "numeric_state",
            "entity_id": S_BATT_PWR,
            "below": -ALERT_BATTERY_DRAW_W,
            "for": {"minutes": ALERT_SUSTAINED_MIN},
        }
    ],
    "conditions": [
        {
            "condition": "numeric_state",
            "entity_id": S_SOLAR,
            "below": ALERT_SOLAR_MIN_W,
        },
        {
            "condition": "time",
            "after": f"{ALERT_HOUR_START:02d}:00:00",
            "before": f"{ALERT_HOUR_END:02d}:00:00",
        },
    ],
    "actions": [
        {
            "action": "notify.send_message",
            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
            "data": {"message": MESSAGE_TEMPLATE},
        },
        {
            # Also speak via pi-sf monitor so it's audible -- the
            # Telegram message is the persistent record, this is the
            # in-the-moment heads-up. Short phrase only because Alexa /
            # TTS doesn't read multi-paragraph text gracefully.
            "action": "rest_command.pi_sf_say",
            "data": {
                "message": (
                    "Alert: solar low and drawing from battery, "
                    "{{ states('sensor.sna_us_15k_53562j0683_pv_total_power') "
                    "| float(0) | round(0) | int }} watts solar, "
                    "battery at {{ states('sensor.sna_us_15k_53562j0683_state_of_charge') "
                    "| float(0) | round(0) | int }} percent."
                )
            },
        },
    ],
}


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def save_automation() -> None:
    token = _load_token()
    url = f"{HA_URL}/api/config/automation/config/{AUTOMATION_ID}"
    print(f"Saving automation '{AUTOMATION_ID}' to {url} ...")
    req = urllib.request.Request(
        url,
        data=json.dumps(AUTOMATION_CONFIG).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    print(f"OK: {body}")


if __name__ == "__main__":
    save_automation()
