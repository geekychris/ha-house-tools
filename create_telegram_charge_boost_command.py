#!/usr/bin/env python3
"""
Create / overwrite the "/charge_boost" Telegram command automation.

Grammar:
    /charge_boost                    -- show current boost state + usage
    /charge_boost status             -- same as bare command
    /charge_boost clear              -- cancel boost
    /charge_boost off                -- alias for clear
    /charge_boost <duration>         -- boost for a duration
    /charge_boost until <spec>       -- boost until a specific time
    /charge_boost till <spec>        -- 'till' is a synonym for 'until'
    /charge_boost for <duration>     -- 'for' is optional filler

Time / duration forms (Jinja parsed):
    HH:MM       today at HH:MM local (rolls to tomorrow if past)
    +Nh / Nh    now + N hours
    +Nm / Nm    now + N minutes

Examples:
    /charge_boost 5h          -- boost for 5 hours
    /charge_boost until 17:00 -- boost until 5pm
    /charge_boost clear       -- cancel

When boost is active:
    input_boolean.smart_ac_charge_boost is set to on
    input_datetime.smart_ac_charge_boost_until is set to the target
The scheduler reads the input_datetime on every tick. When it passes,
boost auto-expires and normal decisions resume.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_charge_boost_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

CLEAR_DT = "1970-01-01 00:00:00"


# args[0] = verb (duration or "until"/"till"/"for"/"clear"/"off"/"status")
# args[1] = spec (if verb was until/till/for)
ARG0 = "{{ (trigger.event.data.args[0] | lower) if trigger.event.data.args else '' }}"
ARG1 = ("{{ (trigger.event.data.args[1] | lower) if "
        "(trigger.event.data.args | count) > 1 else '' }}")

# The spec to parse. If arg0 is 'until'/'till'/'for', use arg1; otherwise use arg0.
SPEC_TEMPLATE = (
    "{% set a0 = arg0 %}"
    "{% if a0 in ['until','till','for'] %}"
    "{{ arg1 }}"
    "{% else %}"
    "{{ a0 }}"
    "{% endif %}"
)

# Reuse the /override time-spec grammar. See create_telegram_override_command.py.
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


REPLY_USAGE = (
    "Usage:\n"
    "  /charge_boost 5h              -- boost 5 hours\n"
    "  /charge_boost until 17:00     -- boost until 5pm today\n"
    "  /charge_boost clear           -- cancel\n"
    "  /charge_boost                 -- show current state\n"
    "While boost is active, the scheduler stops adding day-priority ACs "
    "so battery charges faster. Bedrooms (day_min) keep running."
)

REPLY_STATUS = (
    "Charge boost:"
    "\n{% set until = states('input_datetime.smart_ac_charge_boost_until') %}"
    "\n{% set now_ts = as_timestamp(now()) %}"
    "\n{% set until_ts = as_timestamp(as_datetime(until)) if until and until != 'unknown' else 0 %}"
    "\n{% if until_ts > now_ts %}"
    "\n  ACTIVE until {{ as_datetime(until).strftime('%Y-%m-%d %H:%M') }}"
    "\n  ({{ ((until_ts - now_ts) / 60) | round(0) | int }} minutes remaining)"
    "\n{% else %}"
    "\n  not active"
    "\n{% endif %}"
)

REPLY_OK_SET = (
    "Charge boost enabled. Scheduler will favor charging until "
    "{{ target_dt | trim }}. Only day_min ACs will run during DAY sub-modes; "
    "extras stay off. Cancel with /charge_boost clear."
)

REPLY_OK_CLEAR = "Charge boost cancelled. Scheduler resumes normal decisions."


AUTOMATION_CONFIG = {
    "alias": "Telegram /charge_boost command",
    "description": (
        "Telegram /charge_boost -- set input_datetime.smart_ac_charge_boost_until "
        "to a future time (or clear it). Scheduler reads on next tick and "
        "shifts to charging-favor behaviour. Managed by "
        "create_telegram_charge_boost_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/charge_boost"},
        }
    ],
    "variables": {
        "arg0": ARG0,
        "arg1": ARG1,
        "spec": SPEC_TEMPLATE,
        "target_dt": TARGET_DT_TEMPLATE,
    },
    "actions": [
        {
            "choose": [
                # empty or "status" -> report current state
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg0 in ('', 'status') }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_STATUS},
                        }
                    ],
                },
                # clear / off -> cancel
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg0 in ('clear', 'off') }}"}
                    ],
                    "sequence": [
                        {
                            "action": "input_datetime.set_datetime",
                            "target": {"entity_id": "input_datetime.smart_ac_charge_boost_until"},
                            "data": {"datetime": CLEAR_DT},
                        },
                        {
                            "action": "input_boolean.turn_off",
                            "target": {"entity_id": "input_boolean.smart_ac_charge_boost"},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_OK_CLEAR},
                        },
                    ],
                },
                # can't parse the spec -> usage
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ not (target_dt | trim) }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_USAGE},
                        }
                    ],
                },
            ],
            # default: set the datetime + turn the boolean on
            "default": [
                {
                    "action": "input_datetime.set_datetime",
                    "target": {"entity_id": "input_datetime.smart_ac_charge_boost_until"},
                    "data": {"datetime": "{{ target_dt | trim }}"},
                },
                {
                    "action": "input_boolean.turn_on",
                    "target": {"entity_id": "input_boolean.smart_ac_charge_boost"},
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
