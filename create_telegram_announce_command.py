#!/usr/bin/env python3
"""
Create / overwrite the "/announce" Telegram command automation.

Same behaviour as /say (see create_telegram_say_command.py for the
full design notes -- room matching, default speaker, etc.) but with
`type: announce` instead of `type: tts`. That means Alexa plays its
attention-getting chime before the message instead of speaking
without preamble. Use for time-sensitive notifications ("dinner's
ready", "alarm tripped"); use /say for ambient / non-urgent stuff
("weather is 72 and clear").

After running this script, run set_telegram_bot_commands.py so
/announce appears in Telegram's slash-command autocomplete.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    [TELEGRAM_TTS_DEFAULT=alexa_media_lab] \\
    python3 create_telegram_announce_command.py

Idempotent.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_announce_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)
DEFAULT_ALEXA_SUFFIX = os.environ.get("TELEGRAM_TTS_DEFAULT", "alexa_media_everywhere")

# Differs from /say only in this: chime then speak vs just speak.
TTS_TYPE = "announce"

MATCH_LOGIC = """\
{%- set arg0 = (trigger.event.data.args[0] if trigger.event.data.args else '') | lower -%}
{%- set ns = namespace(found='') -%}
{%- if arg0 == 'all' -%}
  {%- set ns.found = 'alexa_media_everywhere' -%}
{%- elif arg0 -%}
  {%- set exact = 'alexa_media_' + arg0 -%}
  {%- if exact in services.notify -%}
    {%- set ns.found = exact -%}
  {%- else -%}
    {%- for sn in services.notify | list -%}
      {%- if 'alexa_media_' in sn and arg0 in sn and not ns.found -%}
        {%- set ns.found = sn -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endif -%}
{%- endif -%}
"""

SPEAKER_TEMPLATE = MATCH_LOGIC + (
    "{{ ns.found if ns.found else '" + DEFAULT_ALEXA_SUFFIX + "' }}"
)
MESSAGE_TEMPLATE = MATCH_LOGIC + (
    "{%- if ns.found -%}{{ trigger.event.data.args[1:] | join(' ') }}"
    "{%- else -%}{{ trigger.event.data.args | join(' ') }}{%- endif -%}"
)

REPLY_NO_TEXT = "Usage: /announce [room] <text>. Examples: /announce dinner's ready, /announce all alarm tripped."
REPLY_OK = "Announced via {{ speaker }}: {{ message }}"


AUTOMATION_CONFIG = {
    "alias": "Telegram /announce command",
    "description": (
        "Telegram /announce [room] <text> -> Alexa Media plays a chime "
        f"then speaks <text> (default target: notify.{DEFAULT_ALEXA_SUFFIX}, "
        "type=announce). Managed by create_telegram_announce_command.py."
    ),
    "mode": "queued",
    "max": 10,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/announce"},
        }
    ],
    "variables": {
        "speaker": SPEAKER_TEMPLATE,
        "message": MESSAGE_TEMPLATE,
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
                    "action": "notify.{{ speaker }}",
                    "data": {
                        "message": "{{ message }}",
                        "data": {"type": TTS_TYPE},
                    },
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
    print(f"Saving automation '{AUTOMATION_ID}' to {url} (default speaker: notify.{DEFAULT_ALEXA_SUFFIX}) ...")
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
