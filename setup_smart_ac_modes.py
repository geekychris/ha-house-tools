#!/usr/bin/env python3
"""
Create the three smart_ac mode toggle helpers as HA input_booleans:

  * input_boolean.smart_ac_party_mode   -- looser comfort target, add
    living + dining to the "always run" set.
  * input_boolean.smart_ac_nap_mode     -- 60-min turbo on master AC.
  * input_boolean.smart_ac_vacation_mode -- stricter unoccupied rules +
    SoC-guarded alerts. Slightly different from house_unoccupied (the
    older / more permissive equivalent), which stays available too.

The scheduler branch that consumes these is implemented in smart_ac.py's
effective_params(). Config keys picked up per mode live in smart_ac.json:

  party_comfort_target_f
  party_night_min_acs           (default keeps living + dining always on)
  party_evening_extra_required  (default same as party_night_min_acs)
  nap_room                      (which AC gets nap turbo -- default "master")
  nap_duration_min              (default 60)
  vacation_max_acs_total        (default 1 -- tighter than unoccupied's 2)
  vacation_soc_alert_pct        (default 20)

Idempotent -- creates each helper if missing, silently skips if already
present. Adjust the flags below to disable creating a particular mode.
"""

import asyncio
import json
import os
import pathlib
import sys

try:
    import websockets
except ImportError:
    sys.exit("Missing 'websockets' -- pip install websockets")


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")


HELPERS = [
    {
        "name": "Smart AC party mode",
        "icon": "mdi:party-popper",
        "unique": "smart_ac_party_mode",
    },
    {
        "name": "Smart AC nap mode",
        "icon": "mdi:sleep",
        "unique": "smart_ac_nap_mode",
    },
    {
        "name": "Smart AC vacation mode",
        "icon": "mdi:airplane",
        "unique": "smart_ac_vacation_mode",
    },
]


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


async def create_helpers() -> None:
    token = _load_token()
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        await ws.recv()  # auth_ok

        # List existing input_booleans so we don't create duplicates
        await ws.send(json.dumps({"id": 1, "type": "input_boolean/list"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                existing = {r.get("name") for r in reply.get("result", [])}
                break

        for i, h in enumerate(HELPERS, start=2):
            if h["name"] in existing:
                print(f"skip: '{h['name']}' already exists")
                continue
            await ws.send(json.dumps({
                "id": i,
                "type": "input_boolean/create",
                "name": h["name"],
                "icon": h["icon"],
            }))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == i:
                    ok = reply.get("success")
                    print(f"{'OK' if ok else 'FAIL'}: '{h['name']}' -> {reply}")
                    break


if __name__ == "__main__":
    asyncio.run(create_helpers())
