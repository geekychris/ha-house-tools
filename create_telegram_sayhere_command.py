#!/usr/bin/env python3
"""
Create / overwrite the "/sayhere" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/sayhere <text>` from Telegram -> the *living-room* Pi (pi-sf) speaks
<text> through its HDMI monitor speakers. The HA host (a separate Pi 4)
itself has no audio output, so we offload TTS to pi-sf via a tiny HTTP
endpoint we run there.

Sibling of /say (Alexa, no chime) and /announce (Alexa, with chime).
Use /sayhere when you want sound out of the pi-sf living-room monitor
specifically (e.g. you're in the living room, want a quick spoken note
without disturbing every Echo in the house).

----------------------------------------------------------------------------
ARCHITECTURE
----------------------------------------------------------------------------

  Telegram /sayhere -> HA automation -> rest_command.pi_sf_say:
    POST http://pi.example.local:5006/say  {"text": "<message>"}
      -> tts-speaker.service on pi-sf (see setup_pi_sf_tts.py +
         pi_sf/tts_speaker.py) fetches Google Translate TTS mp3
            -> ffplay -> Pi 5 HDMI audio -> monitor speakers

The `rest_command.pi_sf_say` service must be defined in HA's
configuration.yaml first (see PREREQUISITES below) -- HA's rest_command
integration is YAML-only, no UI / config flow.

----------------------------------------------------------------------------
PREREQUISITES
----------------------------------------------------------------------------

1. **TTS server on pi-sf**. Run `setup_pi_sf_tts.py` (one-time), then
   the one sudo command it prints to install the systemd service.
   Verify with `curl http://pi.example.local:5006/healthz`.

2. **rest_command in HA's configuration.yaml** (one-time YAML paste):

       rest_command:
         pi_sf_say:
           url: "http://pi.example.local:5006/say"
           method: POST
           content_type: "application/json"
           payload: '{"text": "{{ message }}"}'
           timeout: 30

   Save configuration.yaml, then Developer Tools -> YAML -> Reload
   "REST commands" (or restart HA). After reload, the service
   `rest_command.pi_sf_say` becomes callable.

3. Telegram bot integration set up (`setup_telegram_bot.py`).

4. Re-run `set_telegram_bot_commands.py` so /sayhere appears in
   Telegram autocomplete (already done unless you customised that list).

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 create_telegram_sayhere_command.py

The automation: trigger on /sayhere event, build `message` from
trigger args, call `rest_command.pi_sf_say` with `message` in payload.
Replies to Telegram with confirmation.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_sayhere_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Name of the rest_command defined in HA's configuration.yaml. If you
# change this, also change the matching key in configuration.yaml.
REST_COMMAND_SERVICE = os.environ.get(
    "TELEGRAM_SAYHERE_REST_COMMAND", "rest_command.pi_sf_say"
)


REPLY_NO_TEXT = "Usage: /sayhere <text>. Example: /sayhere dinner's ready."
REPLY_OK = "Said on pi-sf monitor: {{ message }}"


AUTOMATION_CONFIG = {
    "alias": "Telegram /sayhere command",
    "description": (
        f"Telegram /sayhere <text> -> {REST_COMMAND_SERVICE} -> "
        "HTTP POST to the tts-speaker.service on pi-sf, which speaks "
        "<text> through the living-room monitor's HDMI audio. Managed "
        "by create_telegram_sayhere_command.py."
    ),
    "mode": "queued",
    "max": 10,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/sayhere"},
        }
    ],
    "variables": {
        "message": (
            "{{ trigger.event.data.args | join(' ') "
            "if trigger.event.data.args else '' }}"
        ),
    },
    "actions": [
        {
            "choose": [
                {
                    "conditions": [
                        {"condition": "template", "value_template": "{{ not message }}"}
                    ],
                    "sequence": [
                        {
                            "action": "notify.send_message",
                            "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                            "data": {"message": REPLY_NO_TEXT},
                        }
                    ],
                }
            ],
            "default": [
                {
                    # rest_command services use the domain.service form
                    # rather than action+target. Pass the message via
                    # data; the rest_command's payload template plugs
                    # it into the POST body.
                    "action": REST_COMMAND_SERVICE,
                    "data": {"message": "{{ message }}"},
                },
                {
                    "action": "notify.send_message",
                    "target": {"entity_id": TELEGRAM_NOTIFY_ENTITY},
                    "data": {"message": REPLY_OK},
                },
            ],
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
    print(
        f"Saving automation '{AUTOMATION_ID}' to {url}\n"
        f"  rest_command service: {REST_COMMAND_SERVICE}"
    )
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
