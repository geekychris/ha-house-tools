#!/usr/bin/env python3
"""
Create one input_datetime per AC to hold the "override until" timestamp.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

Creates `input_datetime.ac_<room>_override_until` for each AC. These
serve as the SINGLE SOURCE OF TRUTH for scheduler overrides:

  - Telegram /override writes the target time here (+ flips the input_boolean)
  - Web /overrides page writes here
  - smart_ac.py reads these on each 5-min tick and honors any value that
    is still in the future

Storing state in HA input_datetime helpers instead of the pi-sf state
file means:
  - No dependency on smart-ac-web being installed as a systemd service
  - No dependency on the rest_command YAML block being pasted
  - Overrides survive across scheduler restarts (they always did) AND
    survive pi-sf reboots (they always did, but the file did too)
  - Overrides are visible + editable from HA's UI directly

Default state: 1970-01-01 (past, so no override active on first deploy).

Idempotent: skips creation if the helper already exists.
"""

import asyncio
import json
import os
import pathlib
import sys

import websockets


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Same room list as the AC toggles (create_ac_toggle_automations.py).
ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


async def main() -> None:
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": _load_token()}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"Auth failed: {auth}")

        await ws.send(json.dumps({"id": 1, "type": "input_datetime/list"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                existing = {b["id"] for b in reply.get("result", [])}
                break

        msg_id = 2
        for room in ROOMS:
            ib_id = f"ac_{room}_override_until"
            if ib_id in existing:
                print(f"Skip: input_datetime.{ib_id} already exists.")
                continue
            await ws.send(json.dumps({
                "id": msg_id,
                "type": "input_datetime/create",
                "name": f"AC {room.capitalize()} override until",
                "has_date": True,
                "has_time": True,
                "icon": "mdi:timer-outline",
            }))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == msg_id:
                    if reply.get("success"):
                        print(f"Created: input_datetime.{reply['result']['id']}")
                    else:
                        print(f"FAILED creating {ib_id}: {reply.get('error')}")
                    break
            msg_id += 1


if __name__ == "__main__":
    asyncio.run(main())
