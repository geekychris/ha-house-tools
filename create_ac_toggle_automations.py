#!/usr/bin/env python3
"""
Create one automation per AC that bridges its input_boolean to Alexa.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

For each AC in ROOMS, creates an HA automation that:
- Triggers on `input_boolean.ac_<room>` state change.
- Action: `media_player.play_media` with `media_content_type=routine` and
  `media_content_id="ac on <room>"` or `"ac off <room>"` depending on
  the new state.

This is what makes the UI tile toggles in the Lovelace dashboard
actually control the ACs -- toggling the input_boolean triggers the
matching Alexa routine.

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

1. `setup_ac_input_booleans.py` ran successfully -- the input_booleans
   exist (one per room in ROOMS).
2. The matching Alexa routines exist with names "ac on <room>" and
   "ac off <room>" (see README 2026-06-30).

----------------------------------------------------------------------------
DEVIATION FROM REPO CONVENTION
----------------------------------------------------------------------------

The repo convention is one script per automation. This script creates
6 automations in one go because they are 100% symmetric (same shape,
different room) -- splitting into 6 near-identical files would be
worse than the single loop here. The AUTOMATION_IDs are still stable
("ac_toggle_<room>") so re-running overwrites in place.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_ac_toggle_automations.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]

ALEXA_MEDIA_PLAYER = os.environ.get(
    "TELEGRAM_AC_ALEXA_TARGET", "media_player.everywhere"
)


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def automation_for(room: str) -> dict:
    return {
        "alias": f"AC toggle: {room}",
        "description": (
            f"input_boolean.ac_{room} state change -> fires "
            f"Alexa routine 'ac on/off {room}' via media_player.play_media. "
            "Managed by create_ac_toggle_automations.py."
        ),
        "mode": "queued",
        "max": 5,
        "triggers": [
            {
                "trigger": "state",
                "entity_id": f"input_boolean.ac_{room}",
                "to": ["on", "off"],
            }
        ],
        "actions": [
            {
                "action": "media_player.play_media",
                "target": {"entity_id": ALEXA_MEDIA_PLAYER},
                "data": {
                    "media_content_type": "routine",
                    "media_content_id": (
                        f"ac {{{{ 'on' if trigger.to_state.state == 'on' "
                        f"else 'off' }}}} {room}"
                    ),
                },
            }
        ],
    }


def save_automation(automation_id: str, config: dict) -> None:
    token = _load_token()
    url = f"{HA_URL}/api/config/automation/config/{automation_id}"
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
            print(f"OK ({automation_id}): {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    for room in ROOMS:
        save_automation(f"ac_toggle_{room}", automation_for(room))
