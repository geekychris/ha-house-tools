#!/usr/bin/env python3
"""
Set input_boolean.smart_ac_enabled to `initial: false` so it does NOT
auto-turn-on across HA restarts.

Motivation: on 2026-07-19, an HA restart (following an ungraceful
shutdown that left the recorder DB in an unfinished session) restored
the input_boolean's PRE-CRASH state instead of the one it held right
before the restart -- the kill switch had been flipped off ~1 minute
before the restart but came back on because the DB session with the
"off" state hadn't been committed yet.

`initial: false` sidesteps that whole mess: no matter what the DB
says on startup, the entity begins each HA lifetime at OFF. The user
has to explicitly re-enable it to hand control back to smart_ac.
This is the right default for a safety-critical switch.

Idempotent: re-running is a no-op if the field is already set.

Usage:
    HA_URL=ws://ha-sf.hitorro.com:8123/api/websocket \\
    HA_TOKEN=$(cat ~/code/claude_world/homeassistant/token.txt) \\
    python3 set_smart_ac_enabled_initial_off.py
"""

import asyncio
import json
import os
import pathlib
import sys

import websockets


def _read_token() -> str:
    sib = pathlib.Path(__file__).with_name("token.txt")
    if sib.is_file():
        return sib.read_text().strip()
    return ""


HA_URL = os.environ.get(
    "HA_URL", "ws://homeassistant.local:8123/api/websocket",
)
HA_TOKEN = os.environ.get("HA_TOKEN") or _read_token()

INPUT_BOOLEAN_ID = "smart_ac_enabled"


async def main() -> None:
    if not HA_TOKEN:
        print("HA_TOKEN not set (env var or sibling token.txt).", file=sys.stderr)
        sys.exit(1)

    async with websockets.connect(HA_URL) as ws:
        # Handshake
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        await ws.recv()  # auth_ok

        # Update. input_boolean/update requires name; other fields are
        # optional. Passing initial=false forces the OFF startup default.
        await ws.send(json.dumps({
            "id": 1,
            "type": "input_boolean/update",
            "input_boolean_id": INPUT_BOOLEAN_ID,
            "name": "Smart AC enabled",
            "icon": "mdi:robot",
            "initial": False,
        }))
        resp = json.loads(await ws.recv())
        if not resp.get("success"):
            print(f"update failed: {resp}", file=sys.stderr)
            sys.exit(2)
        result = resp.get("result", {})
        print(
            f"OK: input_boolean.{INPUT_BOOLEAN_ID} "
            f"initial={result.get('initial')} "
            f"(HA will start this entity OFF on every restart).",
        )


if __name__ == "__main__":
    asyncio.run(main())
