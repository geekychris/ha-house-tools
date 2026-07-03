#!/usr/bin/env python3
"""
Create the "Solar-surplus opportunistic pump" automation.

Runs a target load (typically a water pump) only when there is spare
solar production that would otherwise be curtailed (battery is full and
PV > load). Turns it back off when conditions change.

Logic:
  ON when ALL of:
    - PUMP_ENTITY is currently off
    - SoC ≥ SURPLUS_SOC_PCT (default 100)
    - live PV > live load + PUMP_POWER_W (spare headroom)
    - hour is between EARLIEST_HOUR and LATEST_HOUR (default 9-15)
    - pump has been off for ≥ MIN_OFF_MIN (default 30)

  OFF when ANY of:
    - SoC < RELEASE_SOC_PCT (default 95, hysteresis)
    - live PV < live load (started draining battery)
    - hour is outside window
    - pump has been on for ≥ MAX_RUN_MIN (default 60, safety cap)

Trigger cadence: state change of the SoC sensor OR every 5 min. The
automation's own conditions gate the actual actions, so it's safe to
fire often.

Env / defaults are baked into the automation config; re-run to update.

Config knobs (env at write time):
  PUMP_ENTITY        -- default 'switch.well_pump'
  PUMP_POWER_W       -- default 1200 (nameplate; used only for headroom check)
  SURPLUS_SOC_PCT    -- default 100
  RELEASE_SOC_PCT    -- default 95
  EARLIEST_HOUR      -- default 9
  LATEST_HOUR        -- default 15
  MIN_OFF_MIN        -- default 30
  MAX_RUN_MIN        -- default 60

Uses sensors from smart_ac's config (soc, pv, load) so it stays in
sync with the scheduler's view of the world.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")
SMART_AC_CFG = pathlib.Path(
    os.environ.get(
        "SMART_AC_CONFIG",
        pathlib.Path(__file__).resolve().parent / "smart_ac" / "smart_ac.json",
    )
)

AUTOMATION_ID = "solar_surplus_pump"
PUMP_ENTITY = os.environ.get("PUMP_ENTITY", "switch.well_pump")
PUMP_POWER_W = int(os.environ.get("PUMP_POWER_W", "1200"))
SURPLUS_SOC_PCT = int(os.environ.get("SURPLUS_SOC_PCT", "100"))
RELEASE_SOC_PCT = int(os.environ.get("RELEASE_SOC_PCT", "95"))
EARLIEST_HOUR = int(os.environ.get("EARLIEST_HOUR", "9"))
LATEST_HOUR = int(os.environ.get("LATEST_HOUR", "15"))
MIN_OFF_MIN = int(os.environ.get("MIN_OFF_MIN", "30"))
MAX_RUN_MIN = int(os.environ.get("MAX_RUN_MIN", "60"))


def _read_smart_ac_cfg() -> dict:
    if not SMART_AC_CFG.is_file():
        sys.exit(f"smart_ac config not found at {SMART_AC_CFG}. "
                 f"Set SMART_AC_CONFIG.")
    return json.loads(SMART_AC_CFG.read_text())


ac_cfg = _read_smart_ac_cfg()
SOC_SENSOR = ac_cfg["soc_sensor"]
PV_SENSOR = ac_cfg["pv_power_sensor"]
LOAD_SENSOR = ac_cfg["load_sensor"]


TURN_ON_CONDITIONS = (
    "{{ is_state('" + PUMP_ENTITY + "', 'off') "
    f"and (states('{SOC_SENSOR}') | float(0)) >= {SURPLUS_SOC_PCT} "
    f"and (states('{PV_SENSOR}') | float(0)) > "
    f"(states('{LOAD_SENSOR}') | float(0)) + {PUMP_POWER_W} "
    f"and {EARLIEST_HOUR} <= now().hour < {LATEST_HOUR} "
    "and (as_timestamp(now()) - as_timestamp(states."
    + PUMP_ENTITY.replace(".", ".")
    + ".last_changed)) / 60 >= " + str(MIN_OFF_MIN) + " }}"
)

TURN_OFF_CONDITIONS = (
    "{{ is_state('" + PUMP_ENTITY + "', 'on') and ("
    f"(states('{SOC_SENSOR}') | float(100)) < {RELEASE_SOC_PCT} "
    f"or (states('{PV_SENSOR}') | float(0)) < (states('{LOAD_SENSOR}') | float(0)) "
    f"or now().hour < {EARLIEST_HOUR} or now().hour >= {LATEST_HOUR} "
    "or (as_timestamp(now()) - as_timestamp(states."
    + PUMP_ENTITY.replace(".", ".")
    + ".last_changed)) / 60 >= " + str(MAX_RUN_MIN)
    + ") }}"
)


AUTOMATION_CONFIG = {
    "alias": "Solar surplus pump",
    "description": (
        f"Run {PUMP_ENTITY} opportunistically when battery is full and there's "
        f"spare PV production. Auto-off when SoC drops below {RELEASE_SOC_PCT}% "
        f"or PV can't cover load + pump draw. Managed by "
        "create_solar_surplus_automation.py."
    ),
    "mode": "single",
    "triggers": [
        {"trigger": "state", "entity_id": SOC_SENSOR},
        {"trigger": "time_pattern", "minutes": "/5"},
    ],
    "actions": [
        {
            "choose": [
                {
                    "conditions": [
                        {"condition": "template", "value_template": TURN_ON_CONDITIONS}
                    ],
                    "sequence": [
                        {
                            "action": "homeassistant.turn_on",
                            "target": {"entity_id": PUMP_ENTITY},
                        }
                    ],
                },
                {
                    "conditions": [
                        {"condition": "template", "value_template": TURN_OFF_CONDITIONS}
                    ],
                    "sequence": [
                        {
                            "action": "homeassistant.turn_off",
                            "target": {"entity_id": PUMP_ENTITY},
                        }
                    ],
                },
            ]
        }
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
            print(f"OK: {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    save_automation()
