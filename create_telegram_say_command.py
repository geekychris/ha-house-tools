#!/usr/bin/env python3
"""
Create / overwrite the "/say" Telegram command automation.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

`/say [room] <text>` from Telegram -> Alexa speaks <text> aloud.
- First word is treated as a target room IF it matches an Alexa device
  (e.g. `/say lab the wifi is down` -> plays on the Lab Echo only).
- If the first word doesn't match a device, the entire text is spoken
  on the default device (DEFAULT_ALEXA_SUFFIX below).
- `/say all <text>` -> broadcasts to every Alexa via `alexa_media_everywhere`.

`/say` is the no-chime variant (`type: tts`). Use `/announce` (the
sibling automation in create_telegram_announce_command.py) for the
attention-grabbing chime + voice variant.

After running this script Telegram doesn't yet KNOW about `/say` for
autocomplete; run `set_telegram_bot_commands.py` to register it.

----------------------------------------------------------------------------
WHY THIS USES THE LEGACY SERVICE PATTERN
----------------------------------------------------------------------------

The Alexa Media Player HACS integration registers each Alexa as a
DOMAIN+SERVICE pair (`notify.alexa_media_christopher_s_2nd_echo`,
`notify.alexa_media_lab`, ...), NOT as a notify ENTITY. So we can't use
the modern `notify.send_message + entity_id` pattern that the Telegram
bot uses. Instead we build the service name dynamically at runtime and
call it via a templated `action:` value (HA supports template service
names). The `data.type` field selects "tts" (just speak) vs "announce"
(chime then speak).

----------------------------------------------------------------------------
PICKING A DEFAULT
----------------------------------------------------------------------------

DEFAULT_ALEXA_SUFFIX below is the bare-`/say` target. "everywhere"
broadcasts to all speakers, which is loud and startling. Change to a
specific room (e.g. `"alexa_media_lab"` or `"alexa_media_christopher_s_2nd_echo"`)
and re-run this script to lock that in. Override via env at run time
with TELEGRAM_TTS_DEFAULT.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    [TELEGRAM_TTS_DEFAULT=alexa_media_lab] \\
    python3 create_telegram_say_command.py

Idempotent: AUTOMATION_ID is stable.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")

AUTOMATION_ID = "telegram_say_command"
TELEGRAM_NOTIFY_ENTITY = os.environ.get(
    "TELEGRAM_NOTIFY_ENTITY",
    "notify.living_room_homeassistantxyz11_chris_collins",
)

# Default Alexa speaker suffix. Will be called as `notify.{this}`. "everywhere"
# is loud; change to a specific room once you know which Alexa you want as default.
DEFAULT_ALEXA_SUFFIX = os.environ.get("TELEGRAM_TTS_DEFAULT", "alexa_media_everywhere")

# TTS type: "tts" = just speak; "announce" = chime then speak. /say uses tts.
TTS_TYPE = "tts"

# Shared match logic: figures out whether arg0 names a known Alexa device.
# Result is stored in `ns.found` (the alexa_media_<x> suffix, or empty string).
# Both SPEAKER_TEMPLATE and MESSAGE_TEMPLATE include this block so they can
# share the verdict without depending on cross-variable evaluation order.
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

REPLY_NO_TEXT = "Usage: /say [room] <text>. Examples: /say hello, /say lab dinner's ready, /say all alarm."
REPLY_OK = "Said via {{ speaker }}: {{ message }}"


AUTOMATION_CONFIG = {
    "alias": "Telegram /say command",
    "description": (
        "Telegram /say [room] <text> -> Alexa Media speaks <text> aloud "
        f"(default target: notify.{DEFAULT_ALEXA_SUFFIX}, type=tts, no chime). "
        "Managed by create_telegram_say_command.py."
    ),
    "mode": "queued",
    "max": 10,
    "triggers": [
        {
            "trigger": "event",
            "event_type": "telegram_command",
            "event_data": {"command": "/say"},
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
