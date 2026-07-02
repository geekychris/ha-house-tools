#!/usr/bin/env python3
"""
Create the three input_booleans the smart_ac scheduler reads.

----------------------------------------------------------------------------
WHAT GETS CREATED
----------------------------------------------------------------------------

`input_boolean.smart_ac_enabled`
    Master kill-switch. When OFF, scheduler still evaluates and logs
    decisions but does NOT apply them. Default: ON.

`input_boolean.smart_ac_notify_telegram`
    When ON, scheduler pushes a Telegram message on mode transitions
    and on every AC turn_on/turn_off it issues. Off by default (so
    you don't drown in chat).

`input_boolean.house_unoccupied`
    When ON, scheduler switches into a less-aggressive variant:
    relaxed night_min, higher comfort target, hard cap on total ACs,
    and daily-rotated priority to spread wear. See README's smart_ac
    section. Off by default.

All toggleable from the dashboard's Air Conditioners card or via the
existing /on / /off Telegram fuzzy matchers.

Idempotent.
"""

import asyncio
import json
import os
import pathlib
import sys

import websockets


HA_URL = os.environ.get("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.environ.get("HA_TOKEN")

BOOLEANS = [
    # (id, name, icon, initial_state)
    ("smart_ac_enabled", "Smart AC enabled", "mdi:robot", True),
    ("smart_ac_notify_telegram", "Smart AC notify Telegram", "mdi:send-circle-outline", False),
    ("house_unoccupied", "House unoccupied", "mdi:home-export-outline", False),
]


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

        await ws.send(json.dumps({"id": 1, "type": "input_boolean/list"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1:
                existing = {b["id"] for b in reply.get("result", [])}
                break

        msg_id = 2
        for ib_id, ib_name, ib_icon, initial in BOOLEANS:
            if ib_id in existing:
                print(f"Skip: input_boolean.{ib_id} already exists.")
                continue
            await ws.send(json.dumps({
                "id": msg_id,
                "type": "input_boolean/create",
                "name": ib_name,
                "icon": ib_icon,
                "initial": initial,
            }))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == msg_id:
                    if reply.get("success"):
                        print(f"Created: input_boolean.{reply['result']['id']} "
                              f"(default {'ON' if initial else 'OFF'})")
                    else:
                        print(f"FAILED creating {ib_id}: {reply.get('error')}")
                    break
            msg_id += 1


if __name__ == "__main__":
    asyncio.run(main())
