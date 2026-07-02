#!/usr/bin/env python3
"""
Configure Home Assistant's built-in Energy dashboard (Settings -> Energy) to use
the EG4 Web Monitor integration's lifetime kWh sensors.

This is separate from create_energy_dashboard.py, which builds a custom Lovelace
dashboard. This script wires up HA's native Energy feature, which gives proper
hourly/daily kWh attribution, self-consumption %, and (optionally) cost tracking.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------
    pip install websockets
    HA_URL=ws://ha.example.local:8123/api/websocket \\
    HA_TOKEN=eyJhbG... \\
    python3 setup_energy_prefs.py

Env var overrides:
  EG4_INVERTER_SLUG   inverter sensor slug (default sna_us_15k_53562j0683)

----------------------------------------------------------------------------
WHAT IT WIRES UP
----------------------------------------------------------------------------
  Solar production:  *_yield_lifetime
  Battery:           charge = *_charging_lifetime
                     discharge = *_discharging_lifetime
  Grid:              import = *_grid_import_lifetime
                     export = *_grid_export_lifetime

Home consumption is computed by HA automatically from the above; no separate
"consumption" sensor is registered.

Cost tracking is NOT configured here -- add `cost_adjustment_day` / `entity_energy_price`
to the grid flows if you want it.

Safe to re-run -- the energy/save_prefs WS command replaces prefs wholesale.
"""

import asyncio
import json
import os
import sys

import websockets


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")
INVERTER_SLUG = os.environ.get("EG4_INVERTER_SLUG", "sna_us_15k_53562j0683")


def s(suffix: str) -> str:
    return f"sensor.{INVERTER_SLUG}_{suffix}"


ENERGY_PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": s("grid_import_lifetime"),
            "stat_energy_to": s("grid_export_lifetime"),
            "stat_rate": s("grid_power"),
            "cost_adjustment_day": 0.0,
        },
        {
            "type": "solar",
            "stat_energy_from": s("yield_lifetime"),
            "stat_rate": s("pv_total_power"),
            "config_entry_solar_forecast": None,
        },
        {
            "type": "battery",
            "stat_energy_from": s("discharging_lifetime"),
            "stat_energy_to": s("charging_lifetime"),
            "stat_rate": s("battery_power"),
            "stat_soc": s("state_of_charge"),
        },
    ],
    "device_consumption": [],
}


async def save_prefs() -> None:
    if not HA_TOKEN:
        sys.exit("HA_TOKEN env var is required.")
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth failed")
        await ws.send(
            json.dumps({"id": 1, "type": "energy/save_prefs", **ENERGY_PREFS})
        )
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                if reply.get("success"):
                    print("Energy preferences saved.")
                    print("Open Home Assistant -> Energy to view the dashboard.")
                else:
                    sys.exit(f"Save failed: {reply.get('error')}")
                return


if __name__ == "__main__":
    asyncio.run(save_prefs())
