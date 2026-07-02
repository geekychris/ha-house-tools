#!/usr/bin/env python3
"""
Create / overwrite the "/override" Telegram command automation.

Uses HA-native services only (input_boolean.turn_on/off +
input_datetime.set_datetime). No rest_command YAML paste required, no
pi-sf endpoint required, no extra systemd services required. All state
lives in HA-native helpers created by setup_ac_override_input_datetimes.py.

----------------------------------------------------------------------------
GRAMMAR
----------------------------------------------------------------------------

    /override <room> on until <spec>    -- turn AC on + pin until <spec>
    /override <room> off until <spec>   -- turn AC off + pin until <spec>
    /override <room> on for <duration>  -- turn on + pin for <duration>
    /override <room> off for <duration> -- turn off + pin for <duration>
    /override <room> until <spec>       -- pin CURRENT state (no flip)
    /override <room> for <duration>     -- pin current state for <duration>
    /override <room> clear              -- remove the override
    /override list                      -- show active overrides

Time / duration forms (parsed in Jinja at automation runtime):
    HH:MM        today at HH:MM local (rolls to tomorrow if past)
    +Nh / Nh     now + N hours
    +Nm / Nm     now + N minutes

Rooms: master, guest, dining, living, office, kyle.

----------------------------------------------------------------------------
HOW smart_ac PICKS UP THE OVERRIDE
----------------------------------------------------------------------------

smart_ac.py reads each `input_datetime.ac_<room>_override_until` on every
5-min tick. If the value is in the future, it treats that room as
manually-overridden and won't change its state until the timestamp passes.

To "clear" an override we just set the timestamp to a past date
(1970-01-01), which the scheduler always considers expired.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_override_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)
VALID_ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]


# Argument extraction. See docstring for the layouts we handle.
ROOM_TEMPLATE = (
    "{{ (trigger.event.data.args[0] | lower) if trigger.event.data.args else '' }}"
)
STATE_TEMPLATE = (
    "{% set a = trigger.event.data.args or [] %}"
    "{% if (a | count) > 1 and a[1] | lower in ['on', 'off'] %}"
    "{{ a[1] | lower }}{% endif %}"
)
# Normalise 'till' -> 'until' as a convenience -- "till" is a common
# informal spelling and it's more helpful to accept it than complain.
VERB_TEMPLATE = (
    "{% set a = trigger.event.data.args or [] %}"
    "{% if (a | count) > 1 and a[1] | lower in ['on', 'off'] %}"
    "{% set v = a[2] | lower if (a | count) > 2 else '' %}"
    "{% else %}"
    "{% set v = a[1] | lower if (a | count) > 1 else '' %}"
    "{% endif %}"
    "{{ 'until' if v == 'till' else v }}"
)
SPEC_TEMPLATE = (
    "{% set a = trigger.event.data.args or [] %}"
    "{% if (a | count) > 1 and a[1] | lower in ['on', 'off'] %}"
    "{{ a[3] if (a | count) > 3 else '' }}"
    "{% else %}"
    "{{ a[2] if (a | count) > 2 else '' }}"
    "{% endif %}"
)

# Time spec parser -- HH:MM, +Nh, +Nm, Nh, Nm.
# Outputs "YYYY-MM-DD HH:MM:SS" in HA's local timezone, or "" if unparseable.
TARGET_DT_TEMPLATE = (
    "{% set s = spec | lower | trim %}"
    "{% if s.startswith('+') %}{% set s = s[1:] %}{% endif %}"
    "{% set nowl = now() %}"
    "{% if s.endswith('h') %}"
    "  {% set n = s[:-1] | int(0) %}"
    "  {% if n > 0 %}"
    "    {{ (nowl + timedelta(hours=n)).strftime('%Y-%m-%d %H:%M:%S') }}"
    "  {% endif %}"
    "{% elif s.endswith('m') %}"
    "  {% set n = s[:-1] | int(0) %}"
    "  {% if n > 0 %}"
    "    {{ (nowl + timedelta(minutes=n)).strftime('%Y-%m-%d %H:%M:%S') }}"
    "  {% endif %}"
    "{% elif ':' in s %}"
    "  {% set parts = s.split(':') %}"
    "  {% set hh = parts[0] | int(-1) %}"
    "  {% set mm = parts[1] | int(-1) if parts | count > 1 else 0 %}"
    "  {% if 0 <= hh < 24 and 0 <= mm < 60 %}"
    "    {% set target = nowl.replace(hour=hh, minute=mm, second=0, microsecond=0) %}"
    "    {% if target <= nowl %}"
    "      {% set target = target + timedelta(days=1) %}"
    "    {% endif %}"
    "    {{ target.strftime('%Y-%m-%d %H:%M:%S') }}"
    "  {% endif %}"
    "{% endif %}"
)

# Reply templates.
REPLY_USAGE = (
    "Usage:\n"
    "  /override <room> on until <HH:MM>   e.g. /override living on until 23:00\n"
    "  /override <room> off until <HH:MM>  e.g. /override kyle off until 21:00\n"
    "  /override <room> on for <duration>  e.g. /override master on for 2h\n"
    "  /override <room> until <HH:MM>      (pins CURRENT state, no flip)\n"
    "  /override <room> clear              (remove override)\n"
    "  /override list                      (show active overrides)\n"
    "Note: 'till' works as a synonym for 'until'.\n"
    "Note: without 'on' or 'off', the current state is pinned. "
    "If the AC is currently off, it stays off.\n"
    "Rooms: " + ", ".join(VALID_ROOMS)
)
REPLY_BAD_ROOM = "Bad room '{{ room }}'. Use one of: " + ", ".join(VALID_ROOMS) + "."
REPLY_BAD_SPEC = (
    "Can't parse '{{ spec }}' as a time. Use HH:MM (24hr) or +Nh / +Nm "
    "(e.g. 23:00, +2h, +30m)."
)
REPLY_OK_SET = (
    "Override set: {{ room }} "
    "{% if state %}{{ state | upper }} + pinned{% else %}"
    "pinned in current state ({{ states('input_boolean.ac_' ~ room) | upper }})"
    "{% endif %} "
    "until {{ target_dt | trim }}."
    "{% if not state %}\n"
    "(No state flip because you didn't say on/off. "
    "For \"turn on then pin\", use: /override {{ room }} on until <time>. "
    "For \"turn off then pin\", use: /override {{ room }} off until <time>.)"
    "{% endif %}"
)
REPLY_OK_CLEAR = "Override on {{ room }} cleared."

# /override list -- iterate the six input_datetimes and list any whose
# value is still in the future.
REPLY_LIST = (
    "Active overrides:\n"
    "{% set now_l = now() %}"
    "{% set found = namespace(any=false) %}"
    "{% for r in " + json.dumps(VALID_ROOMS) + " %}"
    "{% set until = states('input_datetime.ac_' ~ r ~ '_override_until') %}"
    "{% if until and until != 'unknown' %}"
    "{% set until_dt = (until | as_datetime) %}"
    "{% if until_dt and until_dt > now_l %}"
    "{% set found.any = true %}"
    "  {{ r }}: until {{ until_dt.strftime('%Y-%m-%d %H:%M') }}\n"
    "{% endif %}"
    "{% endif %}"
    "{% endfor %}"
    "{% if not found.any %}(no active overrides){% endif %}"
)

# The 1970 clear-value.
CLEAR_DT = "1970-01-01 00:00:00"


AUTOMATION_CONFIG = {
    "alias": "Telegram /override command",
    "description": (
        "Telegram /override <room> [on|off] until <time> | for <duration> "
        "| clear | list. Writes to input_datetime.ac_<room>_override_until "
        "and optionally flips input_boolean.ac_<room>. No rest_command "
        "dependency. Managed by create_telegram_override_command.py."
    ),
    "mode": "queued",
    "max": 5,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/override"},
        }
    ],
    "variables": {
        "room": ROOM_TEMPLATE,
        "state": STATE_TEMPLATE,
        "verb": VERB_TEMPLATE,
        "spec": SPEC_TEMPLATE,
        "target_dt": TARGET_DT_TEMPLATE,
        "valid_rooms": VALID_ROOMS,
    },
    "actions": [
        {
            "choose": [
                # /override list
                # (Only the literal "list" -- bare /override falls through to
                # the usage branch below. Earlier this branch also matched
                # (not room and not verb), which meant bare /override silently
                # returned an empty list instead of teaching syntax.)
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ room == 'list' }}",
                        }
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_LIST},
                        }
                    ],
                },
                # /override <room> ...  -- must have valid room and a verb
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ not room or not verb }}",
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
                # clear -- set to 1970-01-01 (always in the past = expired)
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ verb == 'clear' }}"}
                    ],
                    "sequence": [
                        {
                            "action": "input_datetime.set_datetime",
                            "target": {
                                "entity_id": "input_datetime.ac_{{ room }}_override_until"
                            },
                            "data": {"datetime": CLEAR_DT},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_OK_CLEAR},
                        },
                    ],
                },
                # Bad time spec
                {
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ not (target_dt | trim) }}",
                        }
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_BAD_SPEC},
                        }
                    ],
                },
            ],
            # Default: set the override.
            #   - if state=on, turn the input_boolean on
            #   - if state=off, turn the input_boolean off
            #   - always set the override_until input_datetime
            "default": [
                # Optional flip first -- only if user said "on" or "off"
                {
                    "if": [
                        {
                            "condition": "template",
                            "value_template": "{{ state == 'on' }}",
                        }
                    ],
                    "then": [
                        {
                            "action": "input_boolean.turn_on",
                            "target": {
                                "entity_id": "input_boolean.ac_{{ room }}"
                            },
                        }
                    ],
                },
                {
                    "if": [
                        {
                            "condition": "template",
                            "value_template": "{{ state == 'off' }}",
                        }
                    ],
                    "then": [
                        {
                            "action": "input_boolean.turn_off",
                            "target": {
                                "entity_id": "input_boolean.ac_{{ room }}"
                            },
                        }
                    ],
                },
                {
                    "action": "input_datetime.set_datetime",
                    "target": {
                        "entity_id": "input_datetime.ac_{{ room }}_override_until"
                    },
                    "data": {"datetime": "{{ target_dt | trim }}"},
                },
                {
                    "action": "notify.send_message",
                    "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                    "data": {"message": REPLY_OK_SET},
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
