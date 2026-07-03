#!/usr/bin/env python3
"""
Create two automations that shape the bedroom AC around sleep hours:

  1. sleep_window_precool: 30 min before BEDTIME_HOUR, pin the bedroom
     AC ON via input_datetime override until BEDTIME_HOUR. Ensures the
     room is comfortably cold when the family goes to bed instead of
     relying on smart_ac's DAY-mode decisions.

  2. wake_up_precool: 20 min before WAKE_HOUR, pin the bedroom AC ON via
     input_datetime override until WAKE_HOUR + 10 min. Ensures the room
     isn't hot at wake time even though NIGHT mode has been active.

Both automations write to input_datetime.ac_<room>_override_until so
smart_ac.py automatically honors them without needing new code paths.

Config (env at write-time):
  BEDTIME_HOUR      -- default 22 (10pm)
  WAKE_HOUR         -- default 7  (7am)
  PRECOOL_ROOM      -- default 'master'
  PRECOOL_LEAD_MIN  -- default 30 (for bedtime; wake uses 20)

USAGE:
    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    BEDTIME_HOUR=22 WAKE_HOUR=7 PRECOOL_ROOM=master \\
    python3 create_sleep_window_automations.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", "22"))
WAKE_HOUR = int(os.environ.get("WAKE_HOUR", "7"))
PRECOOL_ROOM = os.environ.get("PRECOOL_ROOM", "master")
BEDTIME_LEAD_MIN = int(os.environ.get("PRECOOL_LEAD_MIN", "30"))
WAKE_LEAD_MIN = 20

# We fire the trigger LEAD_MIN before the target hour. Compute the
# trigger clock as a "H:MM" string.
def _minus(hour: int, lead_min: int) -> str:
    """Return HH:MM string for `hour:00` minus `lead_min` minutes."""
    total = hour * 60 - lead_min
    total = total % (24 * 60)  # wrap around midnight
    return f"{total // 60:02d}:{total % 60:02d}"


BEDTIME_TRIGGER = _minus(BEDTIME_HOUR, BEDTIME_LEAD_MIN)
WAKE_TRIGGER = _minus(WAKE_HOUR, WAKE_LEAD_MIN)


BEDTIME_AUTOMATION = {
    "alias": "Sleep-window precool (bedtime)",
    "description": (
        f"At {BEDTIME_TRIGGER} local, force {PRECOOL_ROOM} AC on and pin "
        f"until {BEDTIME_HOUR:02d}:00 so the room is cold at bedtime. "
        "Managed by create_sleep_window_automations.py."
    ),
    "mode": "single",
    "triggers": [
        {"trigger": "time", "at": BEDTIME_TRIGGER},
    ],
    "actions": [
        {
            "action": "input_boolean.turn_on",
            "target": {"entity_id": f"input_boolean.ac_{PRECOOL_ROOM}"},
        },
        {
            "action": "input_datetime.set_datetime",
            "target": {
                "entity_id": f"input_datetime.ac_{PRECOOL_ROOM}_override_until"
            },
            "data": {
                "datetime": (
                    "{{ now().replace(hour=" + str(BEDTIME_HOUR)
                    + ", minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S') }}"
                )
            },
        },
    ],
}

WAKE_AUTOMATION = {
    "alias": "Sleep-window precool (wake)",
    "description": (
        f"At {WAKE_TRIGGER} local, force {PRECOOL_ROOM} AC on and pin "
        f"until {WAKE_HOUR:02d}:10 so the room is cold at wake time. "
        "Managed by create_sleep_window_automations.py."
    ),
    "mode": "single",
    "triggers": [
        {"trigger": "time", "at": WAKE_TRIGGER},
    ],
    "actions": [
        {
            "action": "input_boolean.turn_on",
            "target": {"entity_id": f"input_boolean.ac_{PRECOOL_ROOM}"},
        },
        {
            "action": "input_datetime.set_datetime",
            "target": {
                "entity_id": f"input_datetime.ac_{PRECOOL_ROOM}_override_until"
            },
            "data": {
                "datetime": (
                    "{{ now().replace(hour=" + str(WAKE_HOUR)
                    + ", minute=10, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S') }}"
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


def save_automation(automation_id: str, config: dict) -> None:
    token = _load_token()
    url = f"{HA_URL}/api/config/automation/config/{automation_id}"
    print(f"Saving automation '{automation_id}' to {url} ...")
    req = urllib.request.Request(
        url,
        data=json.dumps(config).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  OK: {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    save_automation("sleep_window_precool_bedtime", BEDTIME_AUTOMATION)
    save_automation("sleep_window_precool_wake", WAKE_AUTOMATION)
