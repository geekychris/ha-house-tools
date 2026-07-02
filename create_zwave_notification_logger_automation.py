#!/usr/bin/env python3
"""
Create / overwrite the "Z-Wave notification logger" automation.

Home Assistant's Z-Wave JS integration fires `zwave_js_value_notification`
events on the event bus for things like Central Scene presses (physical
button pushes on a ZEN16, remote key taps, etc). Those events are
transient -- they never show up in the Logbook / activity page by default,
so if you press a scene button and check history later, there's no record.

This automation catches every zwave_js_value_notification event and writes
a human-readable line to the HA Logbook via `logbook.log`. Now presses
persist in the activity page and you can trace what fired when.

BACKGROUND:
  The Zooz ZEN16 Multirelay in this house has S1/S2/S3 wired to momentary
  push-buttons. Config params 2/3/4 = 0 (momentary), 14/15/16 = 1
  (Central Scene enabled). Pressing a switch fires
  zwave_js_value_notification with:
    command_class_name: "Central Scene"
    property_key: "001" | "002" | "003"     (S1 / S2 / S3)
    value: "KeyPressed" | "KeyPressed2x" | "KeyPressed3x"
           | "KeyHeldDown" | "KeyReleased"
  This automation captures all of that + node_id in one Logbook line.

USAGE:
  HA_URL=http://ha.example.local:8123 HA_TOKEN=eyJ... \\
    python3 create_zwave_notification_logger_automation.py

  Idempotent: re-run overwrites the existing automation at the same ID.
  Delete with: DELETE {HA_URL}/api/config/automation/config/{AUTOMATION_ID}
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "zwave_notification_logger"


# The message deliberately packs everything relevant into one line so the
# Logbook stays scannable. Falls back to `property` for events where
# `property_key` is missing.
LOG_MESSAGE = (
    "{{ trigger.event.data.command_class_name }} on node "
    "{{ trigger.event.data.node_id }} "
    "({{ trigger.event.data.property_key_name "
    "or trigger.event.data.property_key "
    "or trigger.event.data.property_name "
    "or trigger.event.data.property }})"
    ": {{ trigger.event.data.value }}"
)


AUTOMATION_CONFIG = {
    "alias": "Z-Wave notification logger",
    "description": (
        "Write every zwave_js_value_notification event to the Logbook so "
        "physical button presses (Central Scene events) persist in the "
        "activity page. Managed by "
        "create_zwave_notification_logger_automation.py."
    ),
    "mode": "queued",  # bursty presses shouldn't drop events
    "max": 20,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "zwave_js_value_notification",
        }
    ],
    "actions": [
        {
            "action": "logbook.log",
            "data": {
                "name": "Z-Wave",
                "message": LOG_MESSAGE,
            },
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
