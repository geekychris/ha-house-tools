#!/usr/bin/env python3
"""
Create / overwrite the "/status" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

When the HA-linked Telegram bot receives the slash command `/status`,
this automation sends a snapshot to the configured Telegram chat:
live solar/battery/home figures, today's energy totals, and the water
tank depth + volume estimate.

Architecture note (matters when re-running on a fresh HA): HA 2026+
removed YAML config for `telegram_bot:` and the `telegram_bot.send_message`
service now requires a notify entity. The automation calls
`notify.send_message` with `entity_id: TELEGRAM_NOTIFY_ENTITY` -- that
entity is created by HA when you add the Telegram Bot integration via
the UI (Settings -> Devices & Services -> Telegram Bot) and add an
allowed-chat subentry. The entity name encodes the bot username + chat
name, e.g. `notify.living_room_homeassistantxyz11_chris_collins`. Set
the TELEGRAM_NOTIFY_ENTITY env var, or edit the default below.

Because the entity is tied to one chat, the reply always goes to that
chat regardless of who triggered `/status`. For multi-user setups,
add a subentry per chat and key off `trigger.event.data.chat_id` to
pick the right entity.

Reply format (plain text, no markdown so it's robust across clients):

    Solar       8083 W
    Battery     76 % (charging 4680 W)
    Home        3403 W
    Grid          0 W

    Today
      Solar yield      15.5 kWh
      Home use         12.7 kWh

    Water tank
      Depth     5.18 ft
      Volume    ~1814 gal

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

Run `setup_telegram_bot.py` first -- it creates the Telegram Bot config
entry + allowed-chat subentry and prints the resulting notify entity ID.
Set that ID in the TELEGRAM_NOTIFY_ENTITY env var when running this
script. Until the notify entity exists, this automation will trigger on
`/status` but the action will fail.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_status_command.py

Uses a stable `AUTOMATION_ID` so re-running overwrites the prior
automation rather than creating duplicates.

To test from Telegram: send your bot `/status`. To test from HA:
Developer Tools -> Events -> Fire Event -> type `telegram_command`, data:
  command: '/status'
  chat_id: <your chat_id>
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_status_command"

# Notify entity created by the Telegram Bot integration's allowed-chat
# subentry. Override via env if your bot name / chat name differs.
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# EG4 sensors used in the reply. Kept inline (not via the s() helper from
# the dashboard script) so this script is self-contained.
INVERTER = "sensor.sna_us_15k_53562j0683"
S_SOLAR = f"{INVERTER}_pv_total_power"
S_SOC = f"{INVERTER}_state_of_charge"
S_BATT_PWR = f"{INVERTER}_battery_power"
S_BATT_STATUS = f"{INVERTER}_battery_status"
# Three EG4 sensors look like "home load" -- consumption_power, eps_power,
# total_load_power. consumption_power is unreliable (intermittently reads 0 W).
# eps_power is the EPS off-grid output power. total_load_power is EG4's
# headline "load" number that matches the EG4 app -- prefer it. The two
# reliable ones live under different naming conventions: total_load_power
# comes from the integration's per-inverter `living_room_eg4_*` entity,
# while everything else uses the `sna_us_15k_<serial>_*` prefix.
S_LOAD = "sensor.living_room_eg4_total_load_power"
S_GRID = f"{INVERTER}_grid_power"
S_YIELD_TODAY = f"{INVERTER}_yield"
S_LOAD_TODAY = f"{INVERTER}_load_energy"  # also more reliable than `consumption`

WATER_DEPTH = "sensor.water_depth_sensor_distance"
WATER_GAL_PER_FOOT = 350  # match create_energy_dashboard.py default

# Zbeacon TH01 temp/humidity sensors, mapped by area via the device registry.
# Re-derive via:  config/device_registry/list  +  config/entity_registry/get
# if you ever swap/move sensors. Temps already in °F.
T_LIVING = "sensor.temp_humidity_temperature"     # area: living_room
T_BEDROOM = "sensor.temp_humidity_temperature_3"  # area: master_bedroom
T_OUTSIDE = "sensor.temp_humidity_temperature_2"  # area: front_patio (used as "outside")
H_LIVING = "sensor.temp_humidity_humidity"
H_BEDROOM = "sensor.temp_humidity_humidity_3"
H_OUTSIDE = "sensor.temp_humidity_humidity_2"

# Battery capacity for the time-to-empty estimate. No EG4 kWh sensor exists, so
# we compute kWh = remaining_Ah * battery_voltage / 1000. Approximate (voltage
# drops as SoC drops on LFP), but close enough to be useful at a glance.
S_BATT_VOLTAGE = "sensor.battery_bank_53562j0683_battery_bank_voltage"
S_BATT_REMAINING_AH = "sensor.battery_bank_53562j0683_battery_bank_remaining_capacity"

# Entity_id for the battery/solar alert automation -- HA derives it from the
# alias (slugified), NOT from AUTOMATION_ID in create_telegram_battery_alert.py.
# If you rename the alert's alias, update this too.
ALERT_ENTITY = "automation.telegram_battery_solar_alert"

# Substring blacklist for the "currently on" section -- skips virtual /
# diagnostic switches that aren't physical loads (Alexa do-not-disturb
# toggles, Zigbee identify buttons, camera time-watermark toggles, etc.).
ON_LIST_DENY = ["do_not_disturb", "identify", "time_watermark", "echo_"]
ON_LIST_FILTER = " and ".join(f"'{tok}' not in s.entity_id" for tok in ON_LIST_DENY)

# Plain-text reply. Telegram preserves whitespace so the column alignment
# renders fine in the chat. The "Currently on" Jinja loop walks both light
# and switch domains, keeps the ones whose state is "on", and filters out
# the virtual-switch noise via ON_LIST_FILTER.
REPLY_TEMPLATE = f"""\
Power now
  Load     {{{{ states('{S_LOAD}') | float(0) | round(0) | int }}}} W
  Solar    {{{{ states('{S_SOLAR}') | float(0) | round(0) | int }}}} W
  Battery  {{{{ states('{S_BATT_PWR}') | float(0) | round(0) | int | abs }}}} W \
({{{{ 'charging' if states('{S_BATT_STATUS}') == 'Charging' else \
'discharging' if states('{S_BATT_STATUS}') == 'Discharging' else 'idle' }}}})
  Grid     {{{{ states('{S_GRID}') | float(0) | round(0) | int }}}} W

Battery   {{{{ states('{S_SOC}') | float(0) | round(0) | int }}}}%
{{%- set bp = states('{S_BATT_PWR}') | float(0) -%}}
{{%- set bv = states('{S_BATT_VOLTAGE}') | float(51.2) -%}}
{{%- set rah = states('{S_BATT_REMAINING_AH}') | float(0) -%}}
{{%- if bp < -100 and rah > 0 -%}}
{{%- set hours = (rah * bv / 1000) / (bp | abs / 1000) %}}
  Time to empty at current draw:\
{{%- if hours > 48 %}} {{{{ (hours / 24) | round(1) }}}} days
{{%- elif hours > 1 %}} {{{{ hours | round(1) }}}} h
{{%- else %}} {{{{ (hours * 60) | round(0) | int }}}} min
{{%- endif %}}
{{%- endif %}}

Temps & humidity
  Living    {{{{ states('{T_LIVING}') | float(0) | round(0) | int }}}}°F / {{{{ states('{H_LIVING}') | float(0) | round(0) | int }}}}%
  Bedroom   {{{{ states('{T_BEDROOM}') | float(0) | round(0) | int }}}}°F / {{{{ states('{H_BEDROOM}') | float(0) | round(0) | int }}}}%
  Outside   {{{{ states('{T_OUTSIDE}') | float(0) | round(0) | int }}}}°F / {{{{ states('{H_OUTSIDE}') | float(0) | round(0) | int }}}}%

Today
  Solar yield  {{{{ states('{S_YIELD_TODAY}') | float(0) | round(1) }}}} kWh
  Load         {{{{ states('{S_LOAD_TODAY}') | float(0) | round(1) }}}} kWh

Water tank
  {{{{ states('{WATER_DEPTH}') | float(0) | round(2) }}}} ft \
(~{{{{ (states('{WATER_DEPTH}') | float(0) * {WATER_GAL_PER_FOOT}) | round(0) | int }}}} gal)

Last alert: \
{{%- set t = state_attr('{ALERT_ENTITY}', 'last_triggered') -%}}
{{%- if t %}} {{{{ relative_time(as_datetime(t)) }}}} ago
{{%- else %}} never
{{%- endif %}}

Currently on:{{% for s in (states.light | list + states.switch | list) if s.state == 'on' and {ON_LIST_FILTER} %}}
  - {{% set a = area_name(s.entity_id) %}}{{% if a %}}{{{{ a }}}}: {{% endif %}}{{{{ s.attributes.friendly_name or s.entity_id }}}}{{% endfor %}}\
"""


AUTOMATION_CONFIG = {
    "alias": "Telegram /status command",
    "description": (
        "Reply to /status with a snapshot of solar / battery / home / grid / "
        "today's totals / water tank. Managed by create_telegram_status_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/status"},
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
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    print(f"OK: {body}")


if __name__ == "__main__":
    save_automation()
