#!/usr/bin/env python3
"""
Create / overwrite the "/ac" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/ac on <room>` or `/ac off <room>` from Telegram -> HA fires the matching
Alexa routine via `media_player.play_media` with `media_content_type=routine`.
The routine (created manually in the Alexa app, see README for the day we
set them up) does the actual smart-home call to turn the AC on/off.

Why routines and not direct control: the SF B-Air ACs only expose to HA
through the Alexa Smart Home graph, which Alexa Media Player doesn't read.
Routines bridge the gap: HA can trigger named routines, the routines have
permission to control Alexa smart-home devices.

Valid rooms are listed in VALID_ROOMS below -- matches the routines we
created (per AC). Notably "all" is NOT included because Christopher's
Alexa account also has ACs in other homes, and an "all" routine would
hit those too. Per-room only.

Examples:
  /ac on master   -> Alexa routine "ac on master"  -> Master Bedroom AC On
  /ac off kyle    -> Alexa routine "ac off kyle"   -> Kyle Room AC Off
  /ac             -> reply with usage hint
  /ac foo bar     -> reply: invalid action / room

----------------------------------------------------------------------------
LIMITATIONS
----------------------------------------------------------------------------

- On/off only. The B-Air Alexa integration doesn't expose set point or
  fan speed. (Would need direct Tuya/LocalTuya control to fix; we hit
  walls on that already.)
- No state feedback. HA fires the routine and assumes it worked.
  Whether the AC actually changed state isn't observable from HA.
- Cloud dependent: HA -> Alexa cloud -> SmartLife cloud -> AC. Any
  layer down = no control.

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

1. Alexa Media Player HACS integration installed and working.
2. Alexa routines created with EXACT names matching:
       "ac on <room>" and "ac off <room>"
   for each room in VALID_ROOMS. (Created in the Alexa app on
   2026-06-30, see README.)
3. Re-run `set_telegram_bot_commands.py` after deploy so /ac shows up
   in Telegram autocomplete.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_ac_command.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_ac_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Any Alexa media_player entity works -- the routine fires globally regardless
# of which Echo initiates the play. "everywhere" is the All-Echoes group and
# is the safest default. Override via env if you want to pin to a specific Echo.
ALEXA_MEDIA_PLAYER = os.environ.get(
    "TELEGRAM_AC_ALEXA_TARGET", "media_player.everywhere"
)

# Rooms we have routines for. Must match the Alexa routine names exactly
# (the second word of "ac on <room>" / "ac off <room>"). Update this list +
# create the matching Alexa routines if you add a new AC. "all" deliberately
# excluded -- this Alexa account is shared with other houses and "ac on all"
# would clobber ACs there too.
VALID_ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]
VALID_ACTIONS = ["on", "off"]


# Validation done in Jinja so the automation can short-circuit with a
# helpful reply instead of silently failing on a bad routine name.
ARG_ACTION_TEMPLATE = (
    "{{ (trigger.event.data.args[0] | lower) if trigger.event.data.args else '' }}"
)
ARG_ROOM_TEMPLATE = (
    "{{ (trigger.event.data.args[1] | lower) if "
    "(trigger.event.data.args | count) > 1 else '' }}"
)
ROUTINE_TEMPLATE = "ac {{ action }} {{ room }}"

REPLY_USAGE = (
    "Usage: /ac on|off <room>. Rooms: "
    + ", ".join(VALID_ROOMS)
    + ". Example: /ac on master."
)
REPLY_BAD_ACTION = "Bad action '{{ action }}'. Use 'on' or 'off'."
REPLY_BAD_ROOM = (
    "Bad room '{{ room }}'. Use one of: " + ", ".join(VALID_ROOMS) + "."
)
REPLY_OK = "Sent: {{ routine_name }} to Alexa."


AUTOMATION_CONFIG = {
    "alias": "Telegram /ac command",
    "description": (
        "Telegram /ac on|off <room> -> fires the matching Alexa routine "
        "via media_player.play_media. Per-AC only; 'all' deliberately not "
        "supported (shared Alexa account with other houses). Managed by "
        "create_telegram_ac_command.py."
    ),
    "mode": "queued",
    "max": 10,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/ac"},
        }
    ],
    "variables": {
        "action": ARG_ACTION_TEMPLATE,
        "room": ARG_ROOM_TEMPLATE,
        "routine_name": ROUTINE_TEMPLATE,
        "valid_actions": VALID_ACTIONS,
        "valid_rooms": VALID_ROOMS,
    },
    "actions": [
        {
            "choose": [
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ not action or not room }}",
                        }
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_USAGE},
                        }
                    ],
                },
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ action not in valid_actions }}",
                        }
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_BAD_ACTION},
                        }
                    ],
                },
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ room not in valid_rooms }}",
                        }
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_BAD_ROOM},
                        }
                    ],
                },
            ],
            "default": [
                {
                    "action": "media_player.play_media",
                    "target": {"entity_id": ALEXA_MEDIA_PLAYER},
                    "data": {
                        "media_content_type": "routine",
                        "media_content_id": "{{ routine_name }}",
                    },
                },
                {
                    "action": "notify.send_message",
                    "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                    "data": {"message": REPLY_OK},
                },
            ],
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
