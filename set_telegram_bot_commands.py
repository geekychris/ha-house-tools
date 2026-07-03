#!/usr/bin/env python3
"""
Register the bot's slash-command list with Telegram (BotFather) so commands
autocomplete when the user types `/` in a Telegram chat with the bot.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

Telegram bots expose an autocomplete menu of available commands. This is
controlled per-bot via the `setMyCommands` Bot API method, NOT via HA.
You run this script once after creating a new bot (or whenever the
command set changes) and Telegram updates the autocomplete server-side
within seconds.

Hits the public Telegram API directly (no HA involvement). Idempotent --
setMyCommands replaces the whole list each call.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    TELEGRAM_BOT_TOKEN="1234567890:AAH..." python3 set_telegram_bot_commands.py

or with the token in `bot_token.txt` sibling file (one line), just:

    python3 set_telegram_bot_commands.py

After it succeeds, open the chat with your bot, tap the `/` menu button
in the input area, and you should see the registered commands. They
populate within a few seconds (Telegram caches client-side, so a force-
close/reopen of the app may be needed).
"""

import json
import os
import pathlib
import subprocess
import sys


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


# Update this list to match the commands you actually have automations for
# (or want to advertise). Re-run after each change.
COMMANDS = [
    {"command": "help", "description": "Show every command + syntax"},
    {"command": "status", "description": "Snapshot: power, temps, water, on-list"},
    {"command": "on", "description": "Turn on (e.g. /on bar, /on sconce, /on master)"},
    {"command": "off", "description": "Turn off (e.g. /off bar, /off master, /off all)"},
    {"command": "say", "description": "Speak via Alexa (e.g. /say hi, /say lab dinner, /say all alarm)"},
    {"command": "announce", "description": "Speak with chime (e.g. /announce dinner's ready)"},
    {"command": "sayhere", "description": "Speak through the Pi's local speaker (e.g. /sayhere hello)"},
    {"command": "water", "description": "Tank depth + estimated usage over 24h/3d/7d"},
    {"command": "ac", "description": "AC on/off (e.g. /ac on master, /ac off kyle)"},
    {"command": "smart_ac", "description": "Show smart AC scheduler's current decision + reasoning"},
    {"command": "smart_ac_report", "description": "Retrospective: SoC, mode times, per-AC runtime + est. draw"},
    {"command": "smart_ac_weekly", "description": "Weekly rollup: mode-minutes + total cost across last 7 days"},
    {"command": "override", "description": "Pin an AC state (e.g. /override living until 22:00, /override master for 2h)"},
    {"command": "pump", "description": "Water pump status / on / off / boost / reset"},
]


def _load_bot_token() -> str:
    if BOT_TOKEN:
        return BOT_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "bot_token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("TELEGRAM_BOT_TOKEN env var (or bot_token.txt sibling file) is required.")


def set_commands() -> None:
    # Shell out to curl: macOS Python.org installs don't ship a usable cert
    # bundle for urllib (you'd have to run Install Certificates.command), and
    # this script only does one HTTPS POST to a well-known public endpoint.
    # curl uses the system trust store and is universally available.
    token = _load_bot_token()
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    body = json.dumps({"commands": COMMANDS})
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", body, url],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        sys.exit(f"curl failed (rc={result.returncode}): {result.stderr}")
    reply = json.loads(result.stdout)
    if not reply.get("ok"):
        sys.exit(f"Telegram refused: {reply}")
    print(f"Registered {len(COMMANDS)} commands with Telegram:")
    for c in COMMANDS:
        print(f"  /{c['command']:<10} {c['description']}")


if __name__ == "__main__":
    set_commands()
