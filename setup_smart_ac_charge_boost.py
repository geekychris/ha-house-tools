#!/usr/bin/env python3
"""
Create the "charge boost" input helpers:

  input_boolean.smart_ac_charge_boost         -- convenience toggle
  input_datetime.smart_ac_charge_boost_until  -- authoritative expiry

The scheduler reads the input_datetime only; the input_boolean is a
convenience for dashboards ("is boost active right now?"). Both are
optional: if they don't exist, the scheduler silently skips the
charge_boost check.

Idempotent: existing helpers are left alone.

Usage:
    HA_URL=ws://ha.example.local:8123/api/websocket HA_TOKEN=eyJ... \\
        python3 setup_smart_ac_charge_boost.py
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


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


async def _ws() -> None:
    token = _load_token()
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        await ws.recv()

        # input_boolean.smart_ac_charge_boost
        await ws.send(json.dumps({"id": 1, "type": "input_boolean/list"}))
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 1:
                existing = {b.get("name") for b in r.get("result", [])}
                break
        if "Smart AC charge boost" in existing:
            print("skip: input_boolean 'Smart AC charge boost' already exists")
        else:
            await ws.send(json.dumps({
                "id": 2,
                "type": "input_boolean/create",
                "name": "Smart AC charge boost",
                "icon": "mdi:battery-charging-high",
            }))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == 2:
                    print("OK: created input_boolean.smart_ac_charge_boost" if r.get("success")
                          else f"FAIL: {r}")
                    break

        # input_datetime.smart_ac_charge_boost_until
        await ws.send(json.dumps({"id": 3, "type": "input_datetime/list"}))
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 3:
                dt_existing = {d.get("name") for d in r.get("result", [])}
                break
        if "Smart AC charge boost until" in dt_existing:
            print("skip: input_datetime 'Smart AC charge boost until' already exists")
        else:
            await ws.send(json.dumps({
                "id": 4,
                "type": "input_datetime/create",
                "name": "Smart AC charge boost until",
                "has_date": True,
                "has_time": True,
                "icon": "mdi:timer-outline",
            }))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == 4:
                    print("OK: created input_datetime.smart_ac_charge_boost_until" if r.get("success")
                          else f"FAIL: {r}")
                    break


if __name__ == "__main__":
    asyncio.run(_ws())
