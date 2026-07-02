#!/usr/bin/env python3
"""
Create the three sliding-window statistics sensors used by /water.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

The YoLink water-depth sensor (`sensor.water_depth_sensor_distance`)
reports raw depth in feet. To answer "how much water have we used in
the last day / 3 days / week", we need historical aggregations. HA's
Statistics platform (now a UI helper / config_flow integration) does
exactly that.

This script creates three statistics sensors via HA's config_flow REST
API. Each tracks the **max depth** in its sliding window. Why max?
Because the tank empties downward over time and gets refilled
periodically. The max in the window approximates "the depth right
after the last refill"; subtracting current depth gives "water used
since that refill", which is the most useful read for the Telegram
/water command.

Resulting sensors (entity IDs assigned by HA when the helper is created):
  - "water depth max 24h"  -- max over the last 24 hours
  - "water depth max 3d"   -- max over the last 72 hours
  - "water depth max 7d"   -- max over the last 168 hours (7 days)

The /water Telegram command (create_telegram_water_command.py) references
these by friendly name pattern so it doesn't matter what the exact
entity_id is.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 setup_water_statistics_sensors.py

Idempotent: re-runs check for an existing helper with the same name and
skip creation if found. Safe to run any number of times.

----------------------------------------------------------------------------
TO REMOVE
----------------------------------------------------------------------------

Settings -> Devices & Services -> Helpers -> click each "water depth
max ..." -> Delete. Or via REST:
    DELETE /api/config/config_entries/entry/<entry_id>
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

SOURCE_SENSOR = "sensor.water_depth_sensor_distance"

# (name, max_age) pairs
WINDOWS = [
    ("water depth max 24h", {"hours": 24}),
    ("water depth max 3d", {"hours": 72}),
    ("water depth max 7d", {"hours": 168}),
]


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def _req(method: str, path: str, body: dict | None = None) -> dict:
    token = _load_token()
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {method} {path}: {e.read().decode('utf-8', errors='replace')}")


def existing_titles() -> set[str]:
    entries = _req("GET", "/api/config/config_entries/entry?domain=statistics")
    return {e.get("title", "") for e in entries}


def create_statistics_sensor(name: str, max_age: dict) -> None:
    existing = existing_titles()
    if name in existing:
        print(f"Skip: '{name}' already exists.")
        return
    print(f"Create: '{name}' (max_age={max_age}) ...")
    flow = _req(
        "POST",
        "/api/config/config_entries/flow",
        {"handler": "statistics", "show_advanced_options": False},
    )
    flow_id = flow["flow_id"]
    # Step user: name + entity_id
    flow = _req(
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"name": name, "entity_id": SOURCE_SENSOR},
    )
    # Step state_characteristic
    flow = _req(
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"state_characteristic": "value_max"},
    )
    # Step options: max_age + precision. Only set what's needed.
    flow = _req(
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {
            "entity_id": SOURCE_SENSOR,
            "state_characteristic": "value_max",
            "max_age": max_age,
            "precision": 2,
        },
    )
    if flow.get("type") != "create_entry":
        sys.exit(f"Flow ended unexpectedly for '{name}': {flow}")
    print(f"  Created entry_id={flow['result']['entry_id']}")


if __name__ == "__main__":
    for name, age in WINDOWS:
        create_statistics_sensor(name, age)
    print()
    print("Done. The new entities take a few seconds to appear in /api/states.")
    print("Look for entity_ids starting with 'sensor.water_depth_max_'.")
