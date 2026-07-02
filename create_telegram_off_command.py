#!/usr/bin/env python3
"""
Create / overwrite the "/off" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/off <name>` from Telegram -> turn off every light + switch whose
friendly_name, area name, or entity_id contains <name> (case-insensitive
substring match). Replies with the list of what was turned off (truncated
at 8 with a "+N more" suffix to keep the message short).

Examples:
  /off bar      -> "Bar Light" off
  /off sconce   -> both sconces off (matches "right" + "left" via area "Living Room: Sconce ...")
  /off master   -> everything in master_bedroom off
  /off all      -> everything off
  /off          -> same as /off all (bedtime convenience)

The candidate pool excludes virtual / diagnostic switches via DENY_TOKENS
(same list as the /status "Currently on" section): do_not_disturb,
identify, time_watermark, echo_. Adjust below if more noise sneaks in.

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

Telegram Bot integration set up (`setup_telegram_bot.py`) and the
TELEGRAM_NOTIFY_ENTITY pointing at the right notify entity. After
running, optionally also run `set_telegram_bot_commands.py` so that `/off`
auto-completes in Telegram.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_off_command.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_off_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Same blacklist as in create_telegram_status_command.py -- skip virtual /
# diagnostic switches that aren't physical loads.
DENY_TOKENS = ["do_not_disturb", "identify", "time_watermark", "echo_"]


def _deny_check(var: str) -> str:
    """Jinja expression that returns true if var contains none of DENY_TOKENS."""
    return " and ".join(f"'{t}' not in {var}" for t in DENY_TOKENS)


# Jinja template that builds `targets`, a list of entity_ids matching the
# user's argument. Match strategy: substring against friendly_name (lowercased),
# area_name (lowercased), or entity_id. 'all' or no argument matches all.
TARGETS_TEMPLATE = f"""\
{{%- set arg = (trigger.event.data.args[0] if trigger.event.data.args else 'all') | lower -%}}
{{%- set ns = namespace(items=[]) -%}}
{{%- for s in (states.light | list + states.switch | list) if {_deny_check('s.entity_id')} -%}}
  {{%- set fn = (s.attributes.friendly_name or '') | lower -%}}
  {{%- set area = (area_name(s.entity_id) or '') | lower -%}}
  {{%- if arg == 'all' or arg in fn or arg in area or arg in s.entity_id -%}}
    {{%- set ns.items = ns.items + [s.entity_id] -%}}
  {{%- endif -%}}
{{%- endfor -%}}
{{{{ ns.items }}}}\
"""

ARG_TEMPLATE = (
    "{{ (trigger.event.data.args[0] if trigger.event.data.args else 'all') | lower }}"
)

REPLY_NO_MATCH = (
    "No light/switch matched '{{ arg }}'.\n"
    "Usage: /off <name> (substring match on friendly_name / area / entity_id), "
    "or /off all to turn everything off."
)

# Truncate at 8 entries so a /off all reply stays short.
REPLY_OK = (
    "Off ({{ targets | count }}):\n"
    "{% for eid in targets[:8] %}"
    "- {{ state_attr(eid, 'friendly_name') or eid }}\n"
    "{% endfor %}"
    "{% if (targets | count) > 8 %}... + {{ (targets | count) - 8 }} more{% endif %}"
)


AUTOMATION_CONFIG = {
    "alias": "Telegram /off command",
    "description": (
        "Telegram /off <name> -> homeassistant.turn_off on every light/switch "
        "whose friendly_name / area / entity_id contains <name>. /off with no "
        "arg or /off all means everything. Managed by create_telegram_off_command.py."
    ),
    "mode": "queued",
    "max": 5,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/off"},
        }
    ],
    "variables": {"arg": ARG_TEMPLATE, "targets": TARGETS_TEMPLATE},
    "actions": [
        {
            "choose": [
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
                }
            ],
            "default": [
                {
                    "action": "homeassistant.turn_off",
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
