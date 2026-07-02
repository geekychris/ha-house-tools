#!/usr/bin/env python3
"""
Create one input_boolean per AC for HA-UI toggle cards.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

Creates `input_boolean.ac_<room>` for each AC in ROOMS via HA's
WebSocket helper API (`input_boolean/create`). HA derives the entity
ID from the name, so "AC Master" -> `input_boolean.ac_master`.

These input_booleans are the user-facing toggles on the Lovelace
dashboard. The actual AC control happens in the matching automation
(see `create_ac_toggle_automations.py`) which watches each boolean
and fires the matching Alexa routine.

Architecture:
    UI tile (toggle) -> input_boolean.ac_<room> state change
        -> automation triggered
            -> media_player.play_media routine "ac on|off <room>"
                -> Alexa routine -> SmartLife skill -> AC

Caveat: HA can't observe actual AC state. If you toggle from the
SmartLife app or via Alexa voice, the HA boolean drifts. Toggling
in HA resyncs it. Limitation of the Alexa-routines bridge.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=ws://homeassistant.local:8123/api/websocket \\
    HA_TOKEN=eyJhbG... \\
    python3 setup_ac_input_booleans.py

Idempotent: skips creation if the input_boolean already exists.
Requires `websockets` (pip install websockets) -- the only script in
this repo besides create_energy_dashboard.py / setup_energy_prefs.py
that needs it, because input_boolean is a storage helper exposed via
WS, not REST.
"""

import asyncio
import json
import os
import pathlib
import sys

import websockets


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Rooms whose AC we want a toggle for. Matches the Alexa routines we created
# (see README 2026-06-30 entry). Update both this list and the routines if
# you add a new AC.
ROOMS = ["master", "guest", "dining", "living", "office", "kyle"]


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


async def main() -> None:
    token = _load_token()
    async with websockets.connect(HA_URL, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_reply = json.loads(await ws.recv())
        if auth_reply.get("type") != "auth_ok":
            sys.exit(f"Auth failed: {auth_reply}")

        # List existing input_booleans
        await ws.send(json.dumps({"id": 1, "type": "input_boolean/list"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                existing_ids = {b["id"] for b in reply.get("result", [])}
                break

        msg_id = 2
        for room in ROOMS:
            ib_id = f"ac_{room}"
            if ib_id in existing_ids:
                print(f"Skip: input_boolean.{ib_id} already exists.")
                continue
            payload = {
                "id": msg_id,
                "type": "input_boolean/create",
                "name": f"AC {room.capitalize()}",
                "icon": "mdi:air-conditioner",
            }
            await ws.send(json.dumps(payload))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == msg_id:
                    if reply.get("success"):
                        new_id = reply["result"]["id"]
                        print(f"Created: input_boolean.{new_id} ({reply['result']['name']})")
                    else:
                        print(f"FAILED creating {ib_id}: {reply.get('error')}")
                    break
            msg_id += 1


if __name__ == "__main__":
    asyncio.run(main())
