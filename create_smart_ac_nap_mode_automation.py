#!/usr/bin/env python3
"""
Create / overwrite the "Smart AC nap mode" automation.

Turning on input_boolean.smart_ac_nap_mode:
  1. Turns on input_boolean.ac_<nap_room> (default 'master').
  2. Sets input_datetime.ac_<nap_room>_override_until = now + <duration>.
  3. Waits <duration> minutes.
  4. Turns input_boolean.smart_ac_nap_mode back off (self-clears).

The override_until pin ensures smart_ac.py won't override the nap-mode
turn-on. When the override expires, the scheduler resumes normal control.

Config knobs:
  NAP_ROOM       -- which AC to nap-cool. Default 'master'.
  NAP_DURATION_MIN -- how long to hold. Default 60.

Both are settable via env at automation-write time so a re-run with
different NAP_DURATION_MIN=90 updates the config.

USAGE:
    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    NAP_ROOM=master NAP_DURATION_MIN=60 \\
    python3 create_smart_ac_nap_mode_automation.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "smart_ac_nap_mode"
NAP_ROOM = os.environ.get("NAP_ROOM", "master")
NAP_DURATION_MIN = int(os.environ.get("NAP_DURATION_MIN", "60"))


AUTOMATION_CONFIG = {
    "alias": "Smart AC nap mode",
    "description": (
        f"When input_boolean.smart_ac_nap_mode turns on, force "
        f"input_boolean.ac_{NAP_ROOM} on and pin the override for "
        f"{NAP_DURATION_MIN} min. Auto-clears at expiry. Managed by "
        "create_smart_ac_nap_mode_automation.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "state",
            "entity_id": "input_boolean.smart_ac_nap_mode",
            "to": "on",
        }
    ],
    "actions": [
        {
            "action": "input_boolean.turn_on",
            "target": {"entity_id": f"input_boolean.ac_{NAP_ROOM}"},
        },
        {
            "action": "input_datetime.set_datetime",
            "target": {"entity_id": f"input_datetime.ac_{NAP_ROOM}_override_until"},
            "data": {
                "datetime": (
                    "{{ (now() + timedelta(minutes=" + str(NAP_DURATION_MIN)
                    + ")).strftime('%Y-%m-%d %H:%M:%S') }}"
                )
            },
        },
        {"delay": {"minutes": NAP_DURATION_MIN}},
        {
            "action": "input_boolean.turn_off",
            "target": {"entity_id": "input_boolean.smart_ac_nap_mode"},
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
            print(f"OK: {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    save_automation()
