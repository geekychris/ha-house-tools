#!/usr/bin/env python3
"""
Create / overwrite the EG4 energy dashboard in Home Assistant.

This script talks to Home Assistant over its WebSocket API and saves a fully
laid-out Lovelace dashboard at the URL path "energy" (default).

It uses the EG4 Web Monitor integration's sensors. Edit the IDs at the top of
the script if your inverter serial / battery bank serial differ.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

1) Create a long-lived access token in Home Assistant:
     Profile -> Security -> "Create Token". Copy it.

2) Install the one dependency:
     pip install websockets

3) Run the script with two env vars set:
     HA_URL=ws://ha.example.local:8123/api/websocket \\
     HA_TOKEN=eyJhbG... \\
     python3 create_energy_dashboard.py

   The dashboard URL slug is "dashboard-energy" by default; override with
   DASH_URL_PATH if you want a different one. The dashboard must already exist
   in HA (Settings -> Dashboards -> Add Dashboard); this script overwrites its
   contents.

4) Open the dashboard in HA. Refresh the browser tab if it was already open.

The script is safe to re-run any time -- it overwrites the dashboard's config
with the layout below.

----------------------------------------------------------------------------
WHAT IT BUILDS
----------------------------------------------------------------------------
Sections:
  1. Right Now            -- glance card with instant Solar / Battery / Home /
                             Grid / EPS power readings.
  1a. Power Breakdown     -- markdown card: live load decomposed into
                             baseline + per-AC (from sensor.smart_ac_calibration)
                             + unaccounted residual.
  1b. Off-Grid Savings    -- today and lifetime $ saved vs grid rate.
  1c. Water Tank          -- YoLink depth sensor reading + approximate gallons.
  2. Battery              -- SoC gauge plus voltage, current, pack count,
                             minimum state-of-health, max cell temperature.
  3. Solar - PV by String -- 24h history graph of PV total + each string.
  4. Power Flows          -- 24h history graph of solar / home / battery /
                             grid in / grid out.
  5. Energy Today         -- today's kWh totals (yield, consumption, charge,
                             discharge, grid import/export).
  6. Lifetime             -- same totals, lifetime.

To add or remove cards, edit DASHBOARD_CONFIG below and re-run.
"""

import asyncio
import json
import os
import sys

import websockets


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")
DASH_URL_PATH = os.environ.get("DASH_URL_PATH", "dashboard-energy")

INVERTER_SLUG = os.environ.get("EG4_INVERTER_SLUG", "sna_us_15k_53562j0683")
BANK_SLUG = os.environ.get("EG4_BANK_SLUG", "53562j0683")

# Avoided-cost rate for the savings card. The house is off-grid, so HA's native
# Energy-dashboard cost feature stays at $0; instead we value home consumption
# against what the local grid would have charged. Override per run if needed.
GRID_RATE_USD_PER_KWH = float(os.environ.get("GRID_RATE_USD_PER_KWH", "0.30"))

# Approximate gallons of water per foot of depth in the house water tank. The
# YoLink depth sensor reports its measurement directly as feet of water, so the
# volume estimate is just depth_ft * WATER_GAL_PER_FOOT.
WATER_GAL_PER_FOOT = float(os.environ.get("WATER_GAL_PER_FOOT", "350"))


def s(suffix: str) -> str:
    return f"sensor.{INVERTER_SLUG}_{suffix}"


def bank(suffix: str) -> str:
    return f"sensor.battery_bank_{BANK_SLUG}_{suffix}"


# Off-grid home consumption is measured as energy delivered through the inverter's
# EPS (Emergency Power Supply) port, which is what actually powers household loads.
# EG4's `consumption_lifetime` is ~7 % higher because it includes inverter
# conversion and idle overhead; for "what would I have paid the utility?" the
# EPS counter is the right denominator.
_EPS = "sensor.living_room_eg4_eps_energy"
_EPS_LIFE = "sensor.living_room_eg4_eps_energy_lifetime"
SAVINGS_MARKDOWN = (
    f"### Today\n"
    f"## **${{{{ (states('{_EPS}') | float(0) * {GRID_RATE_USD_PER_KWH}) | round(2) }}}}** saved\n"
    f"_{{{{ states('{_EPS}') | float(0) | round(1) }}}} kWh delivered to home_\n\n"
    f"### Lifetime\n"
    f"## **${{{{ (states('{_EPS_LIFE}') | float(0) * {GRID_RATE_USD_PER_KWH}) | round(2) }}}}** saved\n"
    f"_{{{{ states('{_EPS_LIFE}') | float(0) | round(1) }}}} kWh delivered to home_\n\n"
    f"<sub>Valued at ${GRID_RATE_USD_PER_KWH:.2f}/kWh — avoided utility purchase (measured at the EPS off-grid output).</sub>"
)

PUMP_SLUG = os.environ.get("PUMP_SLUG", "esyminiv2_rhjl6")


def _pump(kind: str, suffix: str) -> str:
    return f"{kind}.{PUMP_SLUG}_{suffix}"


PUMP_MARKDOWN = (
    "### Water Pump\n"
    f"Status: **{{{{ states('{_pump('sensor', 'pumpstatus')}') }}}}**  "
    f"/ System: {{{{ states('{_pump('sensor', 'systemstatus')}') }}}}  "
    f"/ Errors: {{{{ states('{_pump('sensor', 'faultpumpsnumber')}') }}}}\n\n"
    f"Pressure: **{{{{ states('{_pump('sensor', 'vp_pressurepsi')}') }}}} psi**  "
    f"(setpoint {{{{ states('{_pump('number', 'sp_setpointpressurepsi')}') }}}})  "
    f"| Flow: **{{{{ states('{_pump('sensor', 'vf_flowgall')}') }}}} gal/min**  "
    f"| Power: **{{{{ states('{_pump('sensor', 'po_outputpower')}') }}}} W**\n\n"
    f"This month: **{{{{ states('{_pump('sensor', 'actual_period_flow_counter_gall')}') }}}} gal**, "
    f"{{{{ states('{_pump('sensor', 'actual_period_energy_counter')}') }}}} kWh"
    f"  &nbsp;|&nbsp;  Last month: {{{{ states('{_pump('sensor', 'last_period_flow_counter_gall')}') }}}} gal, "
    f"{{{{ states('{_pump('sensor', 'last_period_energy_counter')}') }}}} kWh\n\n"
    f"Sleep mode: {{{{ states('{_pump('switch', 'sleepmodeenable')}') }}}}"
)


def _pump_section() -> dict:
    """Build the Water Pump grid section. Wrapped in a function so we can
    return None cleanly if you want to conditionally disable it."""
    return {
        "type": "grid",
        "cards": [
            {"type": "heading", "heading": "Water Pump"},
            {"type": "markdown", "content": PUMP_MARKDOWN},
            # Live 24h chart: pressure + flow + power on one graph.
            {
                "type": "history-graph",
                "hours_to_show": 24,
                "refresh_interval": 60,
                "title": "Last 24h",
                "entities": [
                    {"entity": _pump("sensor", "vp_pressurepsi"), "name": "Pressure (psi)"},
                    {"entity": _pump("sensor", "vf_flowgall"), "name": "Flow (gal/min)"},
                    {"entity": _pump("sensor", "po_outputpower"), "name": "Power (W)"},
                ],
            },
            # Multi-day usage: statistics-graph for long-term daily totals.
            # HA auto-collects long-term stats for sensors with proper
            # state_class; DAB integration marks these as measurement/total.
            {
                "type": "statistics-graph",
                "title": "Daily water usage (last 30 days)",
                "chart_type": "bar",
                "period": "day",
                "days_to_show": 30,
                "stat_types": ["change"],
                "entities": [
                    _pump("sensor", "fct_total_delivered_flow_gall"),
                ],
            },
            {
                "type": "statistics-graph",
                "title": "Daily pump energy (last 30 days)",
                "chart_type": "bar",
                "period": "day",
                "days_to_show": 30,
                "stat_types": ["change"],
                "entities": [
                    _pump("sensor", "totalenergy"),
                ],
            },
            # Quick controls: setpoint pressure + sleep mode toggle.
            {
                "type": "entities",
                "show_header_toggle": False,
                "entities": [
                    {"entity": _pump("number", "sp_setpointpressurepsi"), "name": "Setpoint pressure (psi)"},
                    {"entity": _pump("switch", "sleepmodeenable"), "name": "Sleep mode"},
                    {"entity": _pump("select", "pumpdisable"), "name": "Enable / Disable"},
                    {"entity": _pump("select", "powershowercommand"), "name": "Power Shower"},
                    {"entity": _pump("button", "resetactualfault"), "name": "Clear faults"},
                ],
            },
            {
                "type": "markdown",
                "content": (
                    "<sub>For a per-day usage view with prev/next pagination, "
                    "open <a href='http://pi-sf.hitorro.com:5010/water'>the "
                    "pi-sf water page</a>.</sub>"
                ),
            },
        ],
    }


_DEPTH = "sensor.water_depth_sensor_distance"
WATER_MARKDOWN = (
    f"### Depth\n"
    f"## {{{{ states('{_DEPTH}') | float(0) | round(2) }}}} ft\n\n"
    f"### Volume (approx)\n"
    f"## **{{{{ (states('{_DEPTH}') | float(0) * {WATER_GAL_PER_FOOT}) | round(0) | int }}}}** gallons\n\n"
    f"<sub>Estimated at {WATER_GAL_PER_FOOT:.0f} gal/ft of depth.</sub>"
)


# Approximate live power breakdown: per-AC calibration wattages scaled to
# fit the actual live load, so the numbers add up to what the meter
# actually sees. Without scaling, a card claiming "using 6 kW of AC" while
# the meter reads 3 kW is confusing — real load is lower because
# compressors duty-cycle.
#
# Math:
#   raw_ac_sum   = sum of calibration delta_w for every AC currently ON
#   above_base   = live load - baseline
#   scale        = clamp(above_base / raw_ac_sum, 0, 1)
#                  (never > 1: ACs can't draw more than their calibrated
#                  peak; excess is attributed to "other loads")
#   per-AC shown = delta_w * scale
#
# When scale is materially < 1 (i.e. ACs are duty-cycling), the card adds
# a "(peak N W)" annotation per row and a "cycling at ~N% of peak" note so
# the calibrated max is still visible.
#
# `sensor.smart_ac_calibration` is written by pi-sf's calibrate.py.
_LOAD = s("consumption_power")
_AC_ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]
POWER_BREAKDOWN_MARKDOWN = (
    "### Approximate power breakdown\n\n"
    "{% set cal = state_attr('sensor.smart_ac_calibration', 'results') or {} %}\n"
    f"{{% set load = states('{_LOAD}') | float(0) %}}\n"
    "{% set baseline = (cal.master.baseline_w if 'master' in cal else 1065) %}\n"
    "{% set ns = namespace(raw_ac_sum=0, on_rooms=[]) %}\n"
    f"{{% for room in {_AC_ROOMS!r} %}}\n"
    "{%- if states('input_boolean.ac_' ~ room) == 'on' -%}\n"
    "{%- set peak = (cal[room].delta_w if room in cal else 1000) -%}\n"
    "{%- set ns.raw_ac_sum = ns.raw_ac_sum + peak -%}\n"
    "{%- set ns.on_rooms = ns.on_rooms + [(room, peak)] -%}\n"
    "{%- endif -%}\n"
    "{% endfor %}\n"
    "{% set above_base = load - baseline %}\n"
    "{% set scale = 1.0 %}\n"
    "{% if ns.raw_ac_sum > 0 %}\n"
    "{%- set scale = [1.0, [0.0, (above_base / ns.raw_ac_sum)] | max] | min -%}\n"
    "{% endif %}\n"
    "{% set scaled_ac = ns.raw_ac_sum * scale %}\n"
    "{% set other = above_base - scaled_ac %}\n"
    "\n"
    "**Live load:** {{ load | round(0) | int }} W\n\n"
    "**Air conditioners on now** (~{{ scaled_ac | round(0) | int }} W actual"
    "{% if ns.on_rooms and scale < 0.95 %}, duty-cycling at ~{{ (scale * 100) | round(0) | int }}% of peak"
    "{% endif %}):\n"
    "{% if ns.on_rooms %}"
    "{% for room, peak in ns.on_rooms %}"
    "- {{ room }}: ~{{ (peak * scale) | round(0) | int }} W"
    "{% if scale < 0.95 %} _(peak {{ peak | int }} W)_{% endif %}\n"
    "{% endfor %}"
    "{% else %}"
    "_(all off)_\n"
    "{% endif %}"
    "\n"
    "**Baseline** (fridge, lights, networking, always-on): ~{{ baseline | int }} W\n\n"
    "**Other loads:** {{ other | round(0) | int }} W"
    "{% if other > 400 %} ⚠ larger than typical — something else running"
    "{% elif other < -200 %} ⚠ negative — baseline may need re-calibration"
    "{% endif %}\n\n"
    "<sub>Per-AC peaks from `sensor.smart_ac_calibration` (last run: "
    "{{ state_attr('sensor.smart_ac_calibration', 'run_at') or 'never' }}), "
    "scaled proportionally so AC subtotal never exceeds live load − baseline. "
    "Peak values shown when duty-cycling.</sub>"
)


DASHBOARD_CONFIG = {
    "title": "energy",
    "views": [
        {
            "type": "sections",
            "max_columns": 3,
            "sections": [
                # 1) Right Now
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Right Now"},
                        {
                            "type": "glance",
                            "show_state": True,
                            "columns": 3,
                            "entities": [
                                {"entity": s("pv_total_power"), "name": "Solar"},
                                {"entity": s("battery_power"), "name": "Battery"},
                                {"entity": s("consumption_power"), "name": "Home"},
                                {"entity": s("grid_power"), "name": "Grid"},
                                {"entity": s("eps_power"), "name": "EPS"},
                                {"entity": s("battery_status"), "name": "State"},
                            ],
                        },
                    ],
                },
                # 1a) Power breakdown (approximate)
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Power Breakdown"},
                        {"type": "markdown", "content": POWER_BREAKDOWN_MARKDOWN},
                    ],
                },
                # 1b) Savings vs grid
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Off-Grid Savings"},
                        {"type": "markdown", "content": SAVINGS_MARKDOWN},
                    ],
                },
                # 1c) Water tank
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Water Tank"},
                        {"type": "markdown", "content": WATER_MARKDOWN},
                        {
                            "type": "entities",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": _DEPTH, "name": "Depth"},
                                {"entity": "sensor.water_depth_sensor_battery", "name": "Sensor battery"},
                            ],
                        },
                    ],
                },
                # 1d) Water pump (DAB e.symini)
                # Requires the HACS `hass-dabpumps` integration installed.
                # Entity IDs use the pump's short slug (e.g. esyminiv2_rhjl6);
                # if you swap hardware, update PUMP_SLUG below and re-run.
                # For a browsable per-day usage view with pagination, see
                # http://<pi-sf>:5010/water (served by smart_ac/web.py).
                _pump_section(),
                # 1d) AC toggles (Alexa-routines bridge -- input_booleans
                # in this card are bridged to Alexa routines by the automations
                # created in create_ac_toggle_automations.py)
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Air Conditioners"},
                        {
                            "type": "entities",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": "input_boolean.smart_ac_enabled", "name": "Smart scheduler"},
                                {"entity": "input_boolean.smart_ac_notify_telegram", "name": "Notify on Telegram"},
                                {"entity": "input_boolean.house_occupied", "name": "House occupied"},
                                {"type": "section"},
                                {"entity": "input_boolean.ac_master", "name": "Master"},
                                {"entity": "input_boolean.ac_guest", "name": "Guest"},
                                {"entity": "input_boolean.ac_dining", "name": "Dining"},
                                {"entity": "input_boolean.ac_living", "name": "Living"},
                                {"entity": "input_boolean.ac_office", "name": "Office"},
                                {"entity": "input_boolean.ac_kyle", "name": "Kyle"},
                            ],
                        },
                        # Live summary of active overrides (from the six
                        # input_datetime.ac_<room>_override_until helpers).
                        # Any override whose datetime is in the past is
                        # treated as expired and excluded from the list.
                        {
                            "type": "markdown",
                            "content": (
                                "### Active overrides\n\n"
                                "{% set now_ts = now() | as_timestamp %}"
                                "{% set ns = namespace(any=false) %}"
                                "{% for r in ['master','guest','dining','living','office','kyle'] %}"
                                "{% set eid = 'input_datetime.ac_' + r + '_override_until' %}"
                                "{% set until = states(eid) %}"
                                "{% set until_ts = until | as_timestamp(0) %}"
                                "{% if until_ts and until_ts > now_ts %}"
                                "{% set ns.any = true %}"
                                "{% set remaining = ((until_ts - now_ts) / 60) | int %}"
                                "- **{{ r }}** — pinned until "
                                "{{ until | as_datetime | as_local | as_timestamp | timestamp_custom('%H:%M', True) }} "
                                "({{ remaining }} min left)\n"
                                "{% endif %}"
                                "{% endfor %}"
                                "{% if not ns.any %}_none_{% endif %}\n\n"
                                "<sub>Set via Telegram (`/override living on until 22:00`), "
                                "the web UI (`http://pi.example.local:5010/overrides`), "
                                "or by editing the input_datetime helpers below directly. "
                                "Set to a past time (or 1970-01-01) to clear.</sub>"
                            ),
                        },
                        # The 6 override input_datetimes exposed as editable
                        # UI entities. Tap any of them to open a datetime
                        # picker; set to a future time to pin, set to a past
                        # time (or 1970-01-01) to clear.
                        {
                            "type": "entities",
                            "title": "Set / edit overrides",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": "input_datetime.ac_master_override_until",
                                 "name": "Master until"},
                                {"entity": "input_datetime.ac_guest_override_until",
                                 "name": "Guest until"},
                                {"entity": "input_datetime.ac_dining_override_until",
                                 "name": "Dining until"},
                                {"entity": "input_datetime.ac_living_override_until",
                                 "name": "Living until"},
                                {"entity": "input_datetime.ac_office_override_until",
                                 "name": "Office until"},
                                {"entity": "input_datetime.ac_kyle_override_until",
                                 "name": "Kyle until"},
                            ],
                        },
                        {
                            "type": "markdown",
                            "content": (
                                "### Smart AC: {{ states('sensor.smart_ac_status') }}\n"
                                "{%- set a = state_attr('sensor.smart_ac_status','enabled') -%}"
                                "{%- if a is none %}\n_scheduler hasn't reported yet — check journalctl -u smart-ac_\n"
                                "{%- else %}\n"
                                "**Inputs**\n\n"
                                "Battery&nbsp;&nbsp;**{{ state_attr('sensor.smart_ac_status','soc') }}%** "
                                "({{ state_attr('sensor.smart_ac_status','battery_power_w') }} W)\n\n"
                                "Solar&nbsp;&nbsp;{{ state_attr('sensor.smart_ac_status','pv_power_w') }} W &nbsp; "
                                "Load&nbsp;&nbsp;{{ state_attr('sensor.smart_ac_status','load_w') }} W\n\n"
                                "Outdoor&nbsp;&nbsp;{{ state_attr('sensor.smart_ac_status','outdoor_f') }}°F &nbsp; "
                                "{%- set ind = state_attr('sensor.smart_ac_status','indoor_f') or {} -%}"
                                "{%- for r,t in ind.items() %}{{ r }}&nbsp;{{ t }}°F &nbsp; {% endfor %}\n\n"
                                "**Target**\n\n"
                                "ON: {{ (state_attr('sensor.smart_ac_status','target_on') or []) | join(', ') or '_none_' }}\n\n"
                                "OFF: {{ (state_attr('sensor.smart_ac_status','target_off') or []) | join(', ') or '_none_' }}\n\n"
                                "**Reasoning**\n\n"
                                "{%- set rs = state_attr('sensor.smart_ac_status','reasons') or {} -%}"
                                "{%- for r in rs | sort %}- **{{ r }}**: {{ rs[r] }}\n{% endfor %}\n\n"
                                "{%- set acts = state_attr('sensor.smart_ac_status','actions_this_tick') or [] -%}"
                                "{%- if acts %}**Actions last tick:** {{ acts | join(', ') }}\n\n{% endif %}"
                                "<sub>Last evaluated {{ "
                                "as_timestamp(state_attr('sensor.smart_ac_status','last_decision_at')) | timestamp_local "
                                "if state_attr('sensor.smart_ac_status','last_decision_at') else 'never' }} • "
                                "Enabled: {{ a }}</sub>\n"
                                "{%- endif -%}"
                            ),
                        },
                        {
                            "type": "logbook",
                            "title": "AC scheduler logbook",
                            "hours_to_show": 24,
                            "entities": [
                                "sensor.smart_ac_status",
                                "input_boolean.ac_master",
                                "input_boolean.ac_guest",
                                "input_boolean.ac_dining",
                                "input_boolean.ac_living",
                                "input_boolean.ac_office",
                                "input_boolean.ac_kyle",
                                "input_boolean.smart_ac_enabled",
                            ],
                        },
                    ],
                },
                # 2) Battery
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Battery"},
                        {
                            "type": "gauge",
                            "entity": s("state_of_charge"),
                            "name": "State of Charge",
                            "min": 0,
                            "max": 100,
                            "needle": True,
                            "severity": {"green": 50, "yellow": 20, "red": 0},
                        },
                        {
                            "type": "history-graph",
                            "title": "State of Charge (24h)",
                            "hours_to_show": 24,
                            "refresh_interval": 60,
                            "entities": [
                                {"entity": s("state_of_charge"), "name": "SoC %"},
                            ],
                        },
                        {
                            "type": "entities",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": s("battery_status"), "name": "Status"},
                                {"entity": s("battery_power"), "name": "Power"},
                                {"entity": s("battery_voltage"), "name": "Voltage"},
                                {"entity": bank("battery_bank_current"), "name": "Current"},
                                {"entity": bank("battery_count"), "name": "Packs"},
                                {"entity": bank("battery_bank_min_soh"), "name": "Min SoH"},
                                {"entity": bank("battery_bank_max_cell_temperature"), "name": "Max cell temp"},
                            ],
                        },
                    ],
                },
                # 3) Solar by string
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Solar - PV by String (24h)"},
                        {
                            "type": "history-graph",
                            "hours_to_show": 24,
                            "refresh_interval": 60,
                            "entities": [
                                {"entity": s("pv_total_power"), "name": "Total"},
                                {"entity": s("pv1_power"), "name": "String 1"},
                                {"entity": s("pv2_power"), "name": "String 2"},
                                {"entity": s("pv3_power"), "name": "String 3"},
                            ],
                        },
                    ],
                },
                # 4) Power flows
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Power Flows (24h)"},
                        {
                            "type": "history-graph",
                            "hours_to_show": 24,
                            "refresh_interval": 60,
                            "entities": [
                                {"entity": s("pv_total_power"), "name": "Solar"},
                                {"entity": s("consumption_power"), "name": "Home"},
                                {"entity": s("battery_power"), "name": "Battery"},
                                {"entity": s("grid_import_power"), "name": "Grid in"},
                                {"entity": s("grid_export_power"), "name": "Grid out"},
                            ],
                        },
                    ],
                },
                # 5) Energy today
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Energy Today (kWh)"},
                        {
                            "type": "entities",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": s("yield"), "name": "Solar yield"},
                                {"entity": s("consumption"), "name": "Home consumption"},
                                {"entity": s("charging"), "name": "Battery charged"},
                                {"entity": s("discharging"), "name": "Battery discharged"},
                                {"entity": s("grid_import"), "name": "Grid import"},
                                {"entity": s("grid_export"), "name": "Grid export"},
                            ],
                        },
                    ],
                },
                # 6) Lifetime
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "Lifetime (kWh)"},
                        {
                            "type": "entities",
                            "show_header_toggle": False,
                            "entities": [
                                {"entity": s("yield_lifetime"), "name": "Solar yield"},
                                {"entity": s("consumption_lifetime"), "name": "Home consumption"},
                                {"entity": s("charging_lifetime"), "name": "Battery charged"},
                                {"entity": s("discharging_lifetime"), "name": "Battery discharged"},
                                {"entity": s("grid_import_lifetime"), "name": "Grid import"},
                                {"entity": s("grid_export_lifetime"), "name": "Grid export"},
                            ],
                        },
                    ],
                },
            ],
        }
    ],
}


async def save_dashboard() -> None:
    if not HA_TOKEN:
        sys.exit("HA_TOKEN env var is required. See the docstring at the top of this file.")
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth_reply = json.loads(await ws.recv())
        if auth_reply.get("type") != "auth_ok":
            sys.exit(f"Auth failed: {auth_reply}")
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "lovelace/config/save",
                    "url_path": DASH_URL_PATH,
                    "config": DASHBOARD_CONFIG,
                }
            )
        )
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                if reply.get("success"):
                    print(f"Saved dashboard '{DASH_URL_PATH}'.")
                else:
                    sys.exit(f"Save failed: {reply.get('error')}")
                break


if __name__ == "__main__":
    asyncio.run(save_dashboard())
