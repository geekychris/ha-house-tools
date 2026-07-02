#!/usr/bin/env python3
"""
Create / overwrite the "/water" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/water` from Telegram -> snapshot reply with current tank depth + an
estimate of water usage over the last 24h, 3d, and 7d windows.

Sample reply:

    Water tank
      Now   4.81 ft   (~1683 gal)

    Used (vs peak in window):
      24h:  82 gal   (~82 gal/day)
      3d:  130 gal   (~43 gal/day)
      7d:  130 gal   (~19 gal/day)

The "used" number is the difference between the highest depth in that
window and the current depth, converted to gallons via WATER_GAL_PER_FOOT.
This approximates "water used since the last refill" -- if the tank was
refilled within the window, the peak IS that refill, so the number is
usage since then. If no refill happened, the peak is just the value at
the start of the window.

Edge case: if depth has gone UP since the start of the window (net
gain), drop is negative; we show "+N gal added" instead of "used".

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

1. Statistics sensors created by setup_water_statistics_sensors.py.
   They appear as `sensor.front_of_house_water_depth_sensor_water_depth_max_*`
   (referenced via constants below).
2. Telegram bot integration set up.
3. Run set_telegram_bot_commands.py after deploying so /water shows up
   in Telegram autocomplete.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_water_command.py
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_water_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

WATER_DEPTH = "sensor.water_depth_sensor_distance"
WATER_GAL_PER_FOOT = 350

# Statistics sensors created by setup_water_statistics_sensors.py.
# HA prefixes them with the source device's area + name; if you ever
# rename the YoLink device or change its area, update these.
MAX_24H = "sensor.front_of_house_water_depth_sensor_water_depth_max_24h"
MAX_3D = "sensor.front_of_house_water_depth_sensor_water_depth_max_3d"
MAX_7D = "sensor.front_of_house_water_depth_sensor_water_depth_max_7d"


# Jinja macro -- given a max-depth entity and number of days, produce a
# one-line summary of usage in that window. Defined once and called for
# each window so the formatting stays consistent.
USAGE_MACRO = """\
{%- macro usage(label, max_entity, days) -%}
{%- set max_ft = states(max_entity) | float(0) -%}
{%- set now_ft = states('""" + WATER_DEPTH + """') | float(0) -%}
{%- set drop_ft = max_ft - now_ft -%}
{%- set drop_gal = (drop_ft * """ + str(WATER_GAL_PER_FOOT) + """) | round(0) | int -%}
{%- if drop_gal > 5 -%}
{%- set rate = (drop_gal / days) | round(0) | int -%}
  {{ label }}: {{ drop_gal }} gal (~{{ rate }} gal/day)
{%- elif drop_gal < -5 -%}
  {{ label }}: +{{ drop_gal | abs }} gal added
{%- else -%}
  {{ label }}: stable
{%- endif -%}
{%- endmacro -%}\
"""

REPLY_TEMPLATE = USAGE_MACRO + f"""
Water tank
  Now   {{{{ states('{WATER_DEPTH}') | float(0) | round(2) }}}} ft \
(~{{{{ (states('{WATER_DEPTH}') | float(0) * {WATER_GAL_PER_FOOT}) | round(0) | int }}}} gal)

Used vs peak in window:
  {{{{ usage('24h', '{MAX_24H}', 1) }}}}
  {{{{ usage('3d ', '{MAX_3D}', 3) }}}}
  {{{{ usage('7d ', '{MAX_7D}', 7) }}}}\
"""


AUTOMATION_CONFIG = {
    "alias": "Telegram /water command",
    "description": (
        "Reply to /water with current depth + usage estimates over 24h, "
        "3d, 7d (max-in-window minus current). Managed by "
        "create_telegram_water_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/water"},
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
