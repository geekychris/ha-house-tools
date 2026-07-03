#!/usr/bin/env python3
"""
Create / overwrite the "/help" Telegram command automation.

`/help` replies with a compact list of every command the bot supports, its
syntax, and one or two examples per verb. Written as a single message with
no Markdown metacharacters (`_`, `*`, `[`, `` ` ``) so Telegram's default
Markdown parser can't trip on unpaired characters (same footgun that
broke /smart_ac_report earlier -- see the note in
create_telegram_smart_ac_report_command.py).

Keep this file in sync with:
  - the other create_telegram_*_command.py files (source of the syntax
    each command actually accepts)
  - set_telegram_bot_commands.py (the shorter Telegram autocomplete list)

USAGE:
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=eyJ... \\
    python3 create_telegram_help_command.py

  Idempotent -- overwrites at the stable AUTOMATION_ID.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_help_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)


# One long help message. Grouped by intent so someone scanning it can find
# what they want quickly. No Markdown metachars anywhere: Telegram's default
# parser interprets underscores as italics and unpaired ones fail the whole
# message. See note in create_telegram_smart_ac_report_command.py.
HELP_TEXT = """\
Home Assistant bot commands

STATUS
  /status - snapshot: power, temps, water, lights on now
  /water - tank depth + usage over 24h/3d/7d + days-until-empty projection
  /smart_ac - scheduler's current decision + reasoning
  /smart_ac_report - last night's retrospective + cost
  /smart_ac_weekly - last week's rollup: mode-minutes + total cost

LIGHTS AND SWITCHES
  /on <name> - turn on by substring match
    examples: /on bar, /on sconce, /on master, /on all
  /off <name> - turn off by substring match
    examples: /off bar, /off master, /off all
    (empty means everything off - bedtime convenience)

AIR CONDITIONERS
  /ac on <room>  or  /ac off <room>
    rooms: master, guest, dining, living, office, kyle
    example: /ac on master
  /charge_boost 5h            - favor charging for 5 hours (skips extras)
  /charge_boost until 17:00   - same but until a specific time
  /charge_boost clear         - cancel
  /charge_boost               - show current state
  /override <room> [on|off] until <time-spec>
  /override <room> [on|off] for <duration>
  /override <room> clear
  /override list
    time-spec: HH:MM (24h), +2h, +30m, 2h, 30m
    'till' also works as a synonym for 'until'.
    Without on/off, the CURRENT state is pinned. If the AC is
    currently off, it stays off. Always include on/off to flip.
    examples: /override living on until 23:00   (turns on + pins)
              /override master for 2h           (pins current state)
              /override kyle clear

WATER PUMP
  /pump              - status snapshot
  /pump on / off     - enable / disable (off STOPS water)
  /pump boost        - Power Shower (turbo pressure)
  /pump boost off    - stop Power Shower
  /pump power [off]  - alias for boost
  /pump sleep on/off - overnight energy-saver mode
  /pump reset        - clear pump fault
  Per-day usage with pagination: http://pi-sf.hitorro.com:5010/water

MODES (toggle from the HA dashboard)
  input_boolean.smart_ac_party_mode - looser comfort, always run
    living + dining (75F target).
  input_boolean.smart_ac_nap_mode - flip on and master AC gets pinned
    for 60m of turbo cooling, then auto-clears.
  input_boolean.smart_ac_vacation_mode - stricter than the classic
    house_unoccupied: max 1 AC, 84F target.

SPEAKING
  /say [room] <text> - Alexa (no chime)
    examples: /say hi, /say lab dinner ready, /say all fire alarm
  /announce [room] <text> - Alexa (with chime)
  /sayhere <text> - speak on the pi-sf living-room monitor

INFO
  /help - this list

Tip: /commands are case-insensitive. Most take a substring, so partial
names work: /on kit finds the Kitchen light without you needing the
exact entity_id.
"""


# Automation: on /help event, reply with HELP_TEXT.
AUTOMATION_CONFIG = {
    "alias": "Telegram /help command",
    "description": (
        "Reply to /help with the full command list. Managed by "
        "create_telegram_help_command.py."
    ),
    "mode": "single",
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/help"},
        }
    ],
    "actions": [
        {
            "action": "notify.send_message",
            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
            "data": {"message": HELP_TEXT},
        }
    ],
}


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def save_automation() -> None:
    token = _load_token()
    url = f"{HA_URL}/api/config/automation/config/{AUTOMATION_ID}"
    print(f"Saving automation '{AUTOMATION_ID}' to {url} ...")
    req = urllib.request.Request(
        url,
        data=json.dumps(AUTOMATION_CONFIG).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"OK: {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    save_automation()
