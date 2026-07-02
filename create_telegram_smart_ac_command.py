#!/usr/bin/env python3
"""
Create / overwrite the "/smart_ac" Telegram command automation.

`/smart_ac` from Telegram -> reply with the scheduler's current
decision (mode, target set, per-AC reasoning, last decision time).

The scheduler writes its state to sensor.smart_ac_status after every
evaluation tick (see smart_ac/smart_ac.py -> publish_status()). The
state attributes carry: mode, soc, battery_power_w, pv_power_w,
load_w, outdoor_f, indoor_f (dict), target_on / target_off lists,
per-room reasons, actions_this_tick, last_decision_at.

This automation just reads those attributes via state_attr() in a
Jinja template and pretty-prints them.

Prereqs:
- Telegram bot integration set up.
- Smart AC scheduler running on pi-sf (so sensor.smart_ac_status exists).
- Run set_telegram_bot_commands.py after deploying so /smart_ac shows
  up in Telegram autocomplete.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_smart_ac_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)
STATUS_SENSOR = "sensor.smart_ac_status"


# The whole reply is one big Jinja-templated string. Keep room ordering stable
# so the message looks consistent across calls.
REPLY_TEMPLATE = """\
Smart AC: {{ states('""" + STATUS_SENSOR + """') }}
{%- set a = state_attr('""" + STATUS_SENSOR + """', 'mode') %}
{%- if a == None %}
(scheduler hasn't reported yet -- check `journalctl -u smart-ac` on pi-sf)
{%- else %}

Battery   {{ state_attr('""" + STATUS_SENSOR + """', 'soc') }}% \
({{ state_attr('""" + STATUS_SENSOR + """', 'battery_power_w') }} W)
Solar     {{ state_attr('""" + STATUS_SENSOR + """', 'pv_power_w') }} W
Load      {{ state_attr('""" + STATUS_SENSOR + """', 'load_w') }} W
Outdoor   {{ state_attr('""" + STATUS_SENSOR + """', 'outdoor_f') }} F

Target ON:  {{ (state_attr('""" + STATUS_SENSOR + """', 'target_on') or []) | join(', ') or '(none)' }}
Target OFF: {{ (state_attr('""" + STATUS_SENSOR + """', 'target_off') or []) | join(', ') or '(none)' }}

Reasoning:
{%- set reasons = state_attr('""" + STATUS_SENSOR + """', 'reasons') or {} %}
{%- for room in reasons | sort %}
  {{ room }}: {{ reasons[room] }}
{%- endfor %}

{%- set acts = state_attr('""" + STATUS_SENSOR + """', 'actions_this_tick') or [] %}
{%- if acts %}

Actions last tick:
{%- for a in acts %}
  {{ a }}
{%- endfor %}
{%- endif %}

Enabled: {{ state_attr('""" + STATUS_SENSOR + """', 'enabled') }}
Last decision: {{ as_timestamp(state_attr('""" + STATUS_SENSOR + """', 'last_decision_at')) | timestamp_local | default('?', true) }}
{%- endif %}
"""


AUTOMATION_CONFIG = {
    "alias": "Telegram /smart_ac command",
    "description": (
        "Reply to /smart_ac with the current scheduler decision pulled "
        "from sensor.smart_ac_status. Managed by "
        "create_telegram_smart_ac_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/smart_ac"},
        }
    ],
    "actions": [
        {
            "action": "notify.send_message",
            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
            "data": {"message": REPLY_TEMPLATE},
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
