#!/usr/bin/env python3
"""
Create / overwrite the "/pump" Telegram command automation.

Provides status + control for a DAB Pumps e.symini (or similar DAB
model) exposed through the HACS `hass-dabpumps` integration.

Grammar:
    /pump                 -- status snapshot (default)
    /pump status          -- same
    /pump on              -- enable the pump (clear "disabled" state)
    /pump off             -- disable the pump (STOPS water supply)
    /pump boost           -- start Power Shower (turbo pressure)
    /pump boost off       -- stop Power Shower
    /pump power           -- alias for boost
    /pump power off       -- alias for boost off
    /pump sleep on        -- enable sleep mode (energy saver overnight)
    /pump sleep off       -- disable sleep mode
    /pump reset           -- clear current faults

Since disabling the pump stops your water supply, the `off` branch
replies with a reminder to re-enable it. Foolproof: unknown arg -> usage.

----------------------------------------------------------------------------
ENTITY IDs
----------------------------------------------------------------------------

DAB integration entities all share a slug like `esyminiv2_<devid>`,
where <devid> is 6 chars derived from the serial number. Find yours in
HA under Settings -> Devices & Services -> DAB Pumps -> click a
sensor -> the entity_id shows the full slug.

Set the PUMP_SLUG env var when running this script:
    PUMP_SLUG=esyminiv2_rhjl6 python3 create_telegram_pump_command.py

Default is 'esyminiv2_rhjl6' (San Felipe install). Override if you have
a different device or model.

Model support: e.symini series and, with matching entity name, likely
esybox / esyboxpro. If your entity names differ (e.g. `esybox_xxxxx`)
override the individual sensor names via env too (see block below).

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    PUMP_SLUG=esyminiv2_rhjl6 \\
    python3 create_telegram_pump_command.py

Idempotent: re-runs overwrite at the stable AUTOMATION_ID.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_pump_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Entity ID prefix. Override per-sensor below if your naming diverges.
PUMP_SLUG = os.environ.get("PUMP_SLUG", "esyminiv2_rhjl6")

def _e(kind: str, suffix: str) -> str:
    return f"{kind}.{PUMP_SLUG}_{suffix}"

# Read-only status sensors.
STATUS       = os.environ.get("PUMP_STATUS_ENTITY",       _e("sensor", "pumpstatus"))
SYSTEM       = os.environ.get("PUMP_SYSTEM_ENTITY",       _e("sensor", "systemstatus"))
FAULTS       = os.environ.get("PUMP_FAULTS_ENTITY",       _e("sensor", "faultpumpsnumber"))
PRESSURE     = os.environ.get("PUMP_PRESSURE_ENTITY",     _e("sensor", "vp_pressurepsi"))
FLOW         = os.environ.get("PUMP_FLOW_ENTITY",         _e("sensor", "vf_flowgall"))
POWER        = os.environ.get("PUMP_POWER_ENTITY",        _e("sensor", "po_outputpower"))
CURRENT_A    = os.environ.get("PUMP_CURRENT_A_ENTITY",    _e("sensor", "c1_pumpphasecurrent"))
VOLTAGE      = os.environ.get("PUMP_VOLTAGE_ENTITY",      _e("sensor", "sv_supplyvoltage"))
SETPOINT     = os.environ.get("PUMP_SETPOINT_ENTITY",     _e("number", "sp_setpointpressurepsi"))
FLOW_MONTH   = os.environ.get("PUMP_FLOW_MONTH_ENTITY",   _e("sensor", "actual_period_flow_counter_gall"))
FLOW_LAST    = os.environ.get("PUMP_FLOW_LAST_ENTITY",    _e("sensor", "last_period_flow_counter_gall"))
FLOW_TOTAL   = os.environ.get("PUMP_FLOW_TOTAL_ENTITY",   _e("sensor", "fct_total_delivered_flow_gall"))
ENERGY_MONTH = os.environ.get("PUMP_ENERGY_MONTH_ENTITY", _e("sensor", "actual_period_energy_counter"))
ENERGY_LAST  = os.environ.get("PUMP_ENERGY_LAST_ENTITY",  _e("sensor", "last_period_energy_counter"))
ENERGY_TOTAL = os.environ.get("PUMP_ENERGY_TOTAL_ENTITY", _e("sensor", "totalenergy"))
STARTS       = os.environ.get("PUMP_STARTS_ENTITY",       _e("sensor", "startnumber"))
RUN_SECONDS  = os.environ.get("PUMP_RUN_SECONDS_ENTITY",  _e("sensor", "so_pumprunseconds"))
HEATSINK_F   = os.environ.get("PUMP_HEATSINK_ENTITY",     _e("sensor", "te_heatsinktemperaturef"))
SLEEP_MODE   = os.environ.get("PUMP_SLEEP_ENTITY",        _e("switch", "sleepmodeenable"))

# Control entities. All selects; write via select.select_option.
ENABLE_SELECT = os.environ.get("PUMP_ENABLE_SELECT",     _e("select", "pumpdisable"))
BOOST_SELECT  = os.environ.get("PUMP_BOOST_SELECT",      _e("select", "powershowercommand"))
RESET_BUTTON  = os.environ.get("PUMP_RESET_BUTTON",      _e("button", "resetactualfault"))


# Argument extraction. Command = "on" / "off" / "boost" / "reset" / "status" /
# "" (empty = status). Second arg only used for "boost off".
ARG_TEMPLATE = (
    "{{ (trigger.event.data.args[0] | lower) if trigger.event.data.args else '' }}"
)
ARG2_TEMPLATE = (
    "{{ (trigger.event.data.args[1] | lower) if (trigger.event.data.args | count) > 1 else '' }}"
)


REPLY_USAGE = (
    "Usage:\n"
    "  /pump              -- status snapshot\n"
    "  /pump status       -- same as /pump\n"
    "  /pump on           -- enable the pump\n"
    "  /pump off          -- disable the pump (STOPS water supply)\n"
    "  /pump boost        -- start Power Shower (turbo)\n"
    "  /pump boost off    -- stop Power Shower\n"
    "  /pump power        -- alias for boost\n"
    "  /pump power off    -- alias for boost off\n"
    "  /pump sleep on     -- enable sleep mode (overnight energy saver)\n"
    "  /pump sleep off    -- disable sleep mode\n"
    "  /pump reset        -- clear pump fault"
)


# Status reply. Keep it Markdown-safe: no unpaired _, *, [, or `.
# The nested Jinja is verbose but readable; each block is one line item.
REPLY_STATUS = (
    "Water pump\n"
    f"  Status:  {{{{ states('{STATUS}') }}}} / system {{{{ states('{SYSTEM}') }}}}\n"
    f"  Errors:  {{{{ states('{FAULTS}') }}}}\n"
    f"  Pressure: {{{{ states('{PRESSURE}') }}}} psi (setpoint {{{{ states('{SETPOINT}') }}}})\n"
    f"  Flow:    {{{{ states('{FLOW}') }}}} gal/min\n"
    f"  Power:   {{{{ states('{POWER}') }}}} W  ({{{{ states('{CURRENT_A}') }}}} A @ {{{{ states('{VOLTAGE}') }}}} V)\n"
    f"  Heatsink: {{{{ states('{HEATSINK_F}') }}}} F\n"
    "\n"
    f"  This month: {{{{ states('{FLOW_MONTH}') }}}} gal, {{{{ states('{ENERGY_MONTH}') }}}} kWh\n"
    f"  Last month: {{{{ states('{FLOW_LAST}') }}}} gal, {{{{ states('{ENERGY_LAST}') }}}} kWh\n"
    f"  Lifetime:   {{{{ states('{FLOW_TOTAL}') }}}} gal, {{{{ states('{ENERGY_TOTAL}') }}}} kWh\n"
    f"  Starts:     {{{{ states('{STARTS}') }}}}"
    f"  runtime:  {{{{ (states('{RUN_SECONDS}') | float(0) / 3600) | round(1) }}}} h\n"
    "\n"
    f"  Sleep mode: {{{{ states('{SLEEP_MODE}') }}}}"
)


AUTOMATION_CONFIG = {
    "alias": "Telegram /pump command",
    "description": (
        "Snapshot + control the DAB e.symini pump. Reads status sensors, "
        "controls via select.select_option (pumpdisable / powershowercommand) "
        "or button.press (resetactualfault). Managed by "
        "create_telegram_pump_command.py."
    ),
    "mode": "queued",
    "max": 5,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/pump"},
        }
    ],
    "variables": {
        "arg": ARG_TEMPLATE,
        "arg2": ARG2_TEMPLATE,
    },
    "actions": [
        {
            "choose": [
                # /pump on -- enable the pump
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ arg == 'on' }}"}
                    ],
                    "sequence": [
                        {
                            "action": "select.select_option",
                            "target": {"entity_id": ENABLE_SELECT},
                            "data": {"option": "Enable"},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": "Pump enabled."},
                        },
                    ],
                },
                # /pump off -- disable
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ arg == 'off' }}"}
                    ],
                    "sequence": [
                        {
                            "action": "select.select_option",
                            "target": {"entity_id": ENABLE_SELECT},
                            "data": {"option": "Disable"},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": (
                                "Pump disabled. Water supply stopped until "
                                "you /pump on it again."
                            )},
                        },
                    ],
                },
                # /pump boost / /pump boost off (or /pump power / /pump power off)
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg in ('boost', 'power') }}"}
                    ],
                    "sequence": [
                        {
                            "action": "select.select_option",
                            "target": {"entity_id": BOOST_SELECT},
                            "data": {
                                "option": "{{ 'Stop' if arg2 in ['off','stop'] else 'Start' }}"
                            },
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": (
                                "Power Shower {{ 'stopped' if arg2 in ['off','stop'] "
                                "else 'started' }}."
                            )},
                        },
                    ],
                },
                # /pump sleep off
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg == 'sleep' and arg2 == 'off' }}"}
                    ],
                    "sequence": [
                        {
                            "action": "switch.turn_off",
                            "target": {"entity_id": SLEEP_MODE},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": "Sleep mode disabled."},
                        },
                    ],
                },
                # /pump sleep [on]
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg == 'sleep' and arg2 in ('on','') }}"}
                    ],
                    "sequence": [
                        {
                            "action": "switch.turn_on",
                            "target": {"entity_id": SLEEP_MODE},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": "Sleep mode enabled."},
                        },
                    ],
                },
                # /pump reset -- clear faults
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ arg == 'reset' }}"}
                    ],
                    "sequence": [
                        {
                            "action": "button.press",
                            "target": {"entity_id": RESET_BUTTON},
                        },
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": "Faults cleared."},
                        },
                    ],
                },
                # /pump or /pump status -- snapshot (default)
                {
                    "conditions": [
                        {"condition": "template",
                         "value_template": "{{ arg in ('', 'status') }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_STATUS},
                        }
                    ],
                },
            ],
            # Default: unknown arg -> usage.
            "default": [
                {
                    "action": "notify.send_message",
                    "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                    "data": {"message": REPLY_USAGE},
                }
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
