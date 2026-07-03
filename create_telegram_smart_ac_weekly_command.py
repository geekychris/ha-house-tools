#!/usr/bin/env python3
"""
Create / overwrite the "/smart_ac_weekly" Telegram command automation.

Reads `sensor.smart_ac_weekly` attributes (populated by pi-side
weekly.py, which runs Monday 00:45 via smart-ac-weekly.timer) and
pretty-prints them.

USAGE:
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=eyJ... \\
    python3 create_telegram_smart_ac_weekly_command.py

  Idempotent -- overwrites at the stable AUTOMATION_ID.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_smart_ac_weekly_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)
SENSOR = "sensor.smart_ac_weekly"


# Deliberately no Markdown metacharacters -- Telegram default parser
# trips on unpaired underscores. Same lesson as /smart_ac_report.
REPLY_TEMPLATE = """\
Smart AC weekly
{%- set d = state_attr('""" + SENSOR + """', 'n_days_with_data') %}
{%- if d is none or d == 0 %}
(no weekly rollup yet -- runs every Monday 00:45, or run manually:
   ssh chris@pi-sf.hitorro.com 'cd /home/chris/smart_ac && . smart_ac.env && python3 weekly.py')
{%- else %}
Days in window: {{ d }}
{%- set missing = state_attr('""" + SENSOR + """', 'missing_days') or [] %}
{%- if missing %}
Missing days: {{ missing | join(', ') }}
{%- endif %}

SoC across week:
  end-of-day min: {{ state_attr('""" + SENSOR + """', 'soc_min') }}
  peak:           {{ state_attr('""" + SENSOR + """', 'soc_max') }}
  avg start:      {{ state_attr('""" + SENSOR + """', 'soc_avg_start') }}
  avg end:        {{ state_attr('""" + SENSOR + """', 'soc_avg_end') }}

Mode minutes:
{%- set modes = state_attr('""" + SENSOR + """', 'modes_min') or {} %}
{%- for m in modes | sort %}
  {{ m }}: {{ modes[m] }}
{%- endfor %}

Per-AC minutes:
{%- set rt = state_attr('""" + SENSOR + """', 'runtime_min') or {} %}
{%- for r in rt | sort %}
  {{ r }}: {{ rt[r] }}
{%- endfor %}

Weekly cost:
{%- set costs = state_attr('""" + SENSOR + """', 'cost_usd') or {} %}
{%- for r in costs | sort %}
  {{ r }}: ${{ costs[r] }}
{%- endfor %}
  Total: {{ state_attr('""" + SENSOR + """', 'total_kwh') }} kWh, ${{ state_attr('""" + SENSOR + """', 'total_usd') }}
{%- endif %}
"""


AUTOMATION_CONFIG = {
    "alias": "Telegram /smart_ac_weekly command",
    "description": (
        "Reply to /smart_ac_weekly with the last weekly rollup from "
        "sensor.smart_ac_weekly. Managed by "
        "create_telegram_smart_ac_weekly_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/smart_ac_weekly"},
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
