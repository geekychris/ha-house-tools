#!/usr/bin/env python3
"""
Create / overwrite the "/smart_ac_report" Telegram command automation.

Reads `sensor.smart_ac_retrospective` attributes (populated by pi-sf's
retrospective.py, which runs nightly via smart-ac-retrospective.timer)
and pretty-prints them into a multi-line Telegram reply.

Content: window, SoC start/peak/end, time in each mode, per-AC runtime,
per-AC estimated draw, and action count.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_smart_ac_report_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)
SENSOR = "sensor.smart_ac_retrospective"


REPLY_TEMPLATE = """\
Smart AC report
{%- set a = state_attr('""" + SENSOR + """', 'start') %}
{%- if a is none %}
(no retrospective yet -- runs nightly at 00:30 on pi-sf, or run manually:
   ssh $PI_HOST 'cd /home/chris/smart_ac && . smart_ac.env && python3 retrospective.py')
{%- else %}

Window: {{ state_attr('""" + SENSOR + """', 'start_local') }} to {{ state_attr('""" + SENSOR + """', 'end_local') }}

SoC
  start: {{ state_attr('""" + SENSOR + """', 'soc_start') }}%
  peak:  {{ state_attr('""" + SENSOR + """', 'soc_peak') }}% @ {{ state_attr('""" + SENSOR + """', 'soc_peak_local') }}
  end:   {{ state_attr('""" + SENSOR + """', 'soc_end') }}%

Mode minutes:
{%- set modes = state_attr('""" + SENSOR + """', 'modes_min') or {} %}
{%- for m in modes | sort %}
  {{ m }}: {{ modes[m] }}
{%- endfor %}

Runtime minutes:
{%- set rt = state_attr('""" + SENSOR + """', 'runtime_min') or {} %}
{%- for r in rt | sort %}
  {{ r }}: {{ rt[r] }}
{%- endfor %}

Estimated draw (from load deltas):
{%- set draws = state_attr('""" + SENSOR + """', 'draw_w') or {} %}
{%- if draws %}
{%- for r in draws %}
  {{ r }}: ~{{ draws[r].estimate_w }} W (n={{ draws[r].samples }})
{%- endfor %}
{%- else %}
  (no isolated transitions yet -- more data or run calibrate.py)
{%- endif %}

Est. energy + cost (approx, rate {{ state_attr('""" + SENSOR + """', 'cost_rate_usd_per_kwh') }}/kWh):
{%- set costs = state_attr('""" + SENSOR + """', 'costs') or {} %}
{%- for r in costs | sort %}
{%- if r != '_total' %}
  {{ r }}: {{ costs[r].kwh }} kWh, ${{ costs[r].usd }} ({{ costs[r].watts_used }} W {{ costs[r].watts_source }})
{%- endif %}
{%- endfor %}
  Total: {{ state_attr('""" + SENSOR + """', 'cost_total_kwh') }} kWh, ${{ state_attr('""" + SENSOR + """', 'cost_total_usd') }}

Recent actions ({{ state_attr('""" + SENSOR + """', 'action_count') }} total, showing last {{ (state_attr('""" + SENSOR + """', 'actions_recent') or []) | count }}):
{%- for a in state_attr('""" + SENSOR + """', 'actions_recent') or [] %}
  {{ a.time }} {{ a.action }} {{ a.room }} -- {{ a.reason }}
{%- endfor %}
{%- if (state_attr('""" + SENSOR + """', 'actions_recent') or []) | count == 0 %}
  (no actions in window)
{%- endif %}

Decisions: {{ state_attr('""" + SENSOR + """', 'decision_count') }}
{%- endif %}
"""
# NOTE: We deliberately do NOT include the report_path (e.g.
# /home/chris/smart_ac/reports/2026-06-30.md) in this reply. Telegram's
# default parse mode is Markdown, and a lone "_ac" in the path opens an
# italic that never closes, causing BadRequest: "can't find end of the
# entity starting at byte offset ...". notify.send_message does not
# expose a parse_mode override, so the fix is to keep the reply free of
# unpaired Markdown metacharacters (`_`, `*`, `[`, `` ` ``). The file
# path is still available via the web UI /reports/<date> page and the
# journal, so nothing is lost.


AUTOMATION_CONFIG = {
    "alias": "Telegram /smart_ac_report command",
    "description": (
        "Reply to /smart_ac_report with the last retrospective analysis "
        "from sensor.smart_ac_retrospective. Managed by "
        "create_telegram_smart_ac_report_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/smart_ac_report"},
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
