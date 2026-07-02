#!/usr/bin/env python3
"""
Create / overwrite the "side table switch toggles master light" automation
in Home Assistant.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

The master bedroom has a battery-powered Tuya TS004F scene remote named
"side table switch" (`_TZ3000_ja5osu5g TS004F`, ieee a4:c1:38:98:a4:ce:4b:b5,
area master_bedroom). Quirk: `TuyaSmartRemote004FSK_v2`. It sits on the
nightstand; the user wants pressing button 1 to toggle the room's main
light fixture (the ep1 half of the MOES dual switch, NOT the fan).

In ZHA, the TS004F doesn't expose buttons as HA entities. Each press fires
a `zha_event` event with `device_ieee`, `cluster_id`, `command`, and
`endpoint_id`. The TS004F's button 1, with the `TuyaSmartRemote004FSK_v2`
quirk, fires `{cluster_id: 6, command: "toggle"}` (OnOff cluster, Toggle
command). Buttons 2-4 fire different commands on different clusters --
re-use the same script and change the trigger if you want to wire them up
later.

Target light: `light.master_light_light` -- endpoint 1 of the master
bedroom MOES TS0012 (`a4:c1:38:23:85:cd:ac:80`). The ep2 entity
`light.master_light_light_2` is actually the ceiling fan and should NOT
be toggled by this button.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

The script POSTs to HA's automation config REST endpoint with a stable
automation_id, so re-running OVERWRITES the existing automation rather
than creating duplicates. Safe to run any number of times.

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_side_table_automation.py

After saving, HA reloads automations automatically. Press the button to
verify; the light entity should toggle. If nothing happens, watch the
HA Developer Tools -> Events panel (listen to `zha_event`) and confirm
which `command` your button actually fires -- adjust BUTTON_COMMAND
below if the quirk version on your device differs.

To delete the automation:

    curl -X DELETE \\
      -H "Authorization: Bearer $HA_TOKEN" \\
      http://homeassistant.local:8123/api/config/automation/config/side_table_switch_toggle_master_light
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Stable automation_id -- determines the URL path. Re-running with the same id
# overwrites the prior automation in place. Don't change this casually or
# you'll leave orphans.
AUTOMATION_ID = "side_table_switch_toggle_master_light"

# Device firing the ZHA event.
BUTTON_IEEE = "a4:c1:38:98:a4:ce:4b:b5"

# Cluster / command for button 1 of a TS004F with the
# TuyaSmartRemote004FSK_v2 quirk. Other buttons on the same remote:
#   button 2 -> cluster_id 8 (LevelControl), command "step"
#   button 3 -> cluster_id 768 (Color), command "step_color_temperature"
#   button 4 -> cluster_id 5 (Scenes), command "recall"
BUTTON_CLUSTER_ID = 6
BUTTON_COMMAND = "toggle"

# Target light. light.master_light_light = ep1 of the master MOES TS0012,
# the actual ceiling/wall light. ep2 (light.master_light_light_2) is the fan.
TARGET_LIGHT = "light.master_light_light"


AUTOMATION_CONFIG = {
    "alias": "Side table switch toggles master light",
    "description": (
        "Master bedroom side-table TS004F (button 1) toggles the room's main "
        "light fixture (ep1 of the MOES dual switch). Managed by "
        "create_side_table_automation.py -- edit there and re-run."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "zha_event",
            "event_data": {
                "device_ieee": BUTTON_IEEE,
                "cluster_id": BUTTON_CLUSTER_ID,
                "command": BUTTON_COMMAND,
            },
        }
    ],
    "actions": [
        {
            "action": "light.toggle",
            "target": {"entity_id": TARGET_LIGHT},
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
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    print(f"OK: {body}")


if __name__ == "__main__":
    save_automation()
