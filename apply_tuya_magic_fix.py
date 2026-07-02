#!/usr/bin/env python3
"""
Apply the ZHA Toolkit "tuya_magic" unlock to a no-neutral Tuya dual switch.

----------------------------------------------------------------------------
BACKGROUND
----------------------------------------------------------------------------

The house has several MOES / `_TZ3000_18ejxno0` TS0012 no-neutral 2-gang
wall switches (Sconce Lights in the living room, Kyle's light+fan,
Master bedroom light+fan). Each device is one Zigbee endpoint pair: ep1
controls one relay, ep2 controls the other.

Symptom of the bug: turning one channel on in Home Assistant also turns
the other on. The second entity's state changes arrive in the logbook
with no `context_service` -- the *device* is reporting both channels as
toggled, even though HA only commanded one. Originally suspected as a
side effect of the no-neutral wiring (both relays share a live wire),
but the real cause is a Tuya firmware oddity: until the device receives
a specific cluster-0x0000 read sequence, it mirrors ep2's state to ep1.

The fix is the "tuya magic incantation" -- read attributes 4, 0, 1, 5,
7, 0xFFFE on the Basic cluster (0x0000) in one command. ZHA Toolkit
exposes this directly as `zha_toolkit.tuya_magic`. The call is read-only
(no relay state changes), idempotent, and safe to re-run any time.

Verified working on:
  - Sconce Lights (living_room)        ieee a4:c1:38:a3:92:17:04:ec
  - Kyle Light/Fan (kyle_room)         ieee a4:c1:38:7f:01:eb:62:04
  - Master Light/Fan (master_bedroom)  ieee a4:c1:38:23:85:cd:ac:80

If the bug ever recurs (e.g. after a re-pair or coordinator rebuild),
re-run this script with the affected device's IEEE.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

Prereqs: ZHA Toolkit installed in HA (it exposes the `zha_toolkit.*`
service domain). The script just calls one of those services.

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 apply_tuya_magic_fix.py <ieee>

Examples:

    # Re-unlock the master bedroom dual switch
    python3 apply_tuya_magic_fix.py a4:c1:38:23:85:cd:ac:80

    # Re-unlock every known device in one go
    for ieee in \\
        a4:c1:38:a3:92:17:04:ec \\
        a4:c1:38:7f:01:eb:62:04 \\
        a4:c1:38:23:85:cd:ac:80; do
      python3 apply_tuya_magic_fix.py "$ieee"
    done

Both HA_URL and HA_TOKEN can also be supplied via a sibling token.txt
file (one line: just the token) and the default HA_URL.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Known devices (for documentation -- the actual IEEE is passed on the CLI).
KNOWN_DEVICES = {
    "a4:c1:38:a3:92:17:04:ec": "Sconce Lights (living_room)",
    "a4:c1:38:7f:01:eb:62:04": "Kyle Light/Fan (kyle_room)",
    "a4:c1:38:23:85:cd:ac:80": "Master Light/Fan (master_bedroom)",
}


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def apply_tuya_magic(ieee: str) -> None:
    token = _load_token()
    label = KNOWN_DEVICES.get(ieee, "<unknown device>")
    print(f"Applying tuya_magic to {ieee} ({label}) via {HA_URL} ...")

    url = f"{HA_URL}/api/services/zha_toolkit/tuya_magic?return_response=true"
    req = urllib.request.Request(
        url,
        data=json.dumps({"ieee": ieee}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")

    service_response = body.get("service_response", {})
    errors = service_response.get("errors", [])
    if errors:
        sys.exit(f"tuya_magic returned errors: {errors}")

    print(f"OK. ZHA Toolkit v{service_response.get('zha_toolkit_version', '?')} "
          f"completed at {service_response.get('start_time', '?')}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python3 {sys.argv[0]} <ieee>\n"
                 f"Known devices:\n" +
                 "\n".join(f"  {ieee}  {name}" for ieee, name in KNOWN_DEVICES.items()))
    apply_tuya_magic(sys.argv[1])
