#!/usr/bin/env python3
"""
Create / overwrite the "/on" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/on <name>` from Telegram -> turn on every light + switch whose
friendly_name, area name, or entity_id contains <name> (case-insensitive
substring match). Replies with the list of what was turned on.

Examples:
  /on bar      -> "Bar Light" on
  /on sconce   -> both sconces on
  /on master   -> everything in master_bedroom on

Unlike `/off`, this command does NOT accept "all" or an empty argument --
turning on every plug in the house at midnight is rarely what you want.
If you genuinely want everything on, you have to say `/on all` explicitly
or send `/on light` etc. The empty-arg case replies asking for a name.

Candidate filter is the same DENY_TOKENS list as /off and /status.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_on_command.py

Idempotent; uses AUTOMATION_ID `telegram_on_command`.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_on_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

DENY_TOKENS = ["do_not_disturb", "identify", "time_watermark", "echo_"]


def _deny_check(var: str) -> str:
    return " and ".join(f"'{t}' not in {var}" for t in DENY_TOKENS)


# Same matching as /off, but `arg` defaults to "" (no match) instead of "all".
TARGETS_TEMPLATE = f"""\
{{%- set arg = (trigger.event.data.args[0] if trigger.event.data.args else '') | lower -%}}
{{%- set ns = namespace(items=[]) -%}}
{{%- if arg -%}}
{{%- for s in (states.light | list + states.switch | list) if {_deny_check('s.entity_id')} -%}}
  {{%- set fn = (s.attributes.friendly_name or '') | lower -%}}
  {{%- set area = (area_name(s.entity_id) or '') | lower -%}}
  {{%- if arg == 'all' or arg in fn or arg in area or arg in s.entity_id -%}}
    {{%- set ns.items = ns.items + [s.entity_id] -%}}
  {{%- endif -%}}
{{%- endfor -%}}
{{%- endif -%}}
{{{{ ns.items }}}}\
"""

ARG_TEMPLATE = (
    "{{ (trigger.event.data.args[0] if trigger.event.data.args else '') | lower }}"
)

REPLY_NO_ARG = "Usage: /on <name>. Example: /on bar, /on sconce, /on master, /on all."
REPLY_NO_MATCH = (
    "No light/switch matched '{{ arg }}'.\n"
    "Usage: /on <name> (substring match on friendly_name / area / entity_id). "
    "Try /on bar, /on sconce, /on master."
)
REPLY_OK = (
    "On ({{ targets | count }}):\n"
    "{% for eid in targets[:8] %}"
    "- {{ state_attr(eid, 'friendly_name') or eid }}\n"
    "{% endfor %}"
    "{% if (targets | count) > 8 %}... + {{ (targets | count) - 8 }} more{% endif %}"
)


AUTOMATION_CONFIG = {
    "alias": "Telegram /on command",
    "description": (
        "Telegram /on <name> -> homeassistant.turn_on on every light/switch "
        "whose friendly_name / area / entity_id contains <name>. Requires "
        "explicit argument (no implicit 'all'). Managed by "
        "create_telegram_on_command.py."
    ),
    "mode": "queued",
    "max": 5,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/on"},
        }
    ],
    "variables": {"arg": ARG_TEMPLATE, "targets": TARGETS_TEMPLATE},
    "actions": [
        {
            "choose": [
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ not arg }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_NO_ARG},
                        }
                    ],
                },
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ targets | count == 0 }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_NO_MATCH},
                        }
                    ],
                },
            ],
            "default": [
                {
                    "action": "homeassistant.turn_on",
                    "target": {"entity_id": "{{ targets }}"},
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
