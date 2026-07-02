#!/usr/bin/env python3
"""
Set up the Home Assistant Telegram Bot integration end-to-end via the
config-flow REST API. Replaces the older YAML-paste workflow.

----------------------------------------------------------------------------
WHY THIS SCRIPT EXISTS
----------------------------------------------------------------------------

In HA 2025/2026 the `telegram_bot:` YAML config was removed and the
integration moved to a config-flow + notify-entity architecture. The
service `telegram_bot.send_message` no longer takes a literal chat_id;
it requires a notify entity (created by an "allowed chat" sub-entry of
the integration's config entry). Doing this through the UI is two
separate steps (add integration, then add allowed-chat sub-entry); this
script does both via REST so the setup is reproducible from this repo.

----------------------------------------------------------------------------
ONE-TIME PREREQUISITES (off-HA, can't be scripted)
----------------------------------------------------------------------------

1. In Telegram, talk to **@BotFather** -> `/newbot` -> give it a name
   and a username ending in "bot". BotFather returns an HTTP API token
   like `1234567890:AAH...`. Save it as TELEGRAM_BOT_TOKEN.

2. In Telegram, send your new bot any message (a direct chat is
   simplest; group chats also work but trigger bot-privacy quirks).
   Then visit `https://api.telegram.org/bot<token>/getUpdates` in a
   browser and pull `chat.id` from the JSON. That's TELEGRAM_CHAT_ID
   (positive integer for direct chats, negative for groups).

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    TELEGRAM_BOT_TOKEN="1234567890:AAH..." \\
    TELEGRAM_CHAT_ID="6278589329" \\
    python3 setup_telegram_bot.py

The script is idempotent:
  - If a telegram_bot config entry already exists, it reuses it.
  - If an allowed_chat_ids sub-entry for the given chat_id already
    exists, it reuses it.
Safe to run any number of times.

At the end it prints the resulting notify entity ID -- copy that into
`create_telegram_status_command.py` / `create_telegram_battery_alert.py`
(constant `TELEGRAM_NOTIFY_ENTITY`), or pass it via env var when running
those scripts.

----------------------------------------------------------------------------
TO REMOVE
----------------------------------------------------------------------------

UI: Settings -> Devices & Services -> Telegram Bot -> three-dot menu ->
Delete. Or via REST: DELETE /api/config/config_entries/entry/<entry_id>.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def _load_token() -> str:
    if HA_TOKEN:
        return HA_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def _load_bot_token() -> str:
    if BOT_TOKEN:
        return BOT_TOKEN
    fallback = pathlib.Path(__file__).resolve().parent / "bot_token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("TELEGRAM_BOT_TOKEN env var (or bot_token.txt sibling file) is required.")


def _req(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {_load_token()}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {method} {path}: {e.read().decode('utf-8', errors='replace')}")


def _ws(commands: list[dict]) -> list[dict]:
    """Minimal WS round-trip for endpoints not exposed via REST."""
    # Avoid the websockets dep here; do it inline so this script stays stdlib-only.
    # For the few WS-only calls (entity registry queries), shell out via a tiny
    # inline asyncio implementation using the `websocket` style with raw sockets
    # would be ugly. Instead, use REST-only endpoints when possible. The entity
    # discovery below uses /api/states which is REST.
    raise NotImplementedError


def ensure_config_entry(bot_token: str) -> tuple[str, str]:
    """Create-or-find the telegram_bot config entry. Returns (entry_id, title)."""
    # Reuse if exists.
    entries = _req("GET", "/api/config/config_entries/entry?domain=telegram_bot")
    for e in entries:
        if e.get("domain") == "telegram_bot":
            print(f"Found existing telegram_bot config entry: {e['entry_id']} ({e['title']})")
            return e["entry_id"], e["title"]

    print("No existing telegram_bot config entry. Starting config flow ...")
    # Step 1: start the flow.
    flow = _req(
        "POST",
        "/api/config/config_entries/flow",
        {"handler": "telegram_bot", "show_advanced_options": False},
    )
    # The first step asks which platform (broadcast / polling / webhooks).
    if flow.get("type") == "menu":
        flow = _req(
            "POST",
            f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"next_step_id": "polling"},
        )
    elif flow.get("type") == "form" and flow.get("step_id") == "user":
        # Some HA versions present platform as a form select rather than a menu.
        flow = _req(
            "POST",
            f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"platform": "polling"},
        )

    if flow.get("type") != "form":
        sys.exit(f"Unexpected flow state after platform select: {flow}")

    # Step 2: submit the bot token (+ optional parse_mode/proxy fields).
    submit = {"api_key": bot_token}
    # Heuristic: include sensible defaults if the schema asks for them.
    for field in flow.get("data_schema", []) or []:
        name = field.get("name")
        if name == "parse_mode" and "default" not in field:
            submit.setdefault("parse_mode", "html")
    result = _req(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        submit,
    )
    if result.get("type") != "create_entry":
        sys.exit(f"Config flow did not create entry: {result}")

    entry_id = result["result"]["entry_id"]
    title = result["result"]["title"]
    print(f"Created telegram_bot config entry: {entry_id} ({title})")
    return entry_id, title


def ensure_chat_subentry(entry_id: str, chat_id: int) -> str:
    """Create-or-find an allowed_chat_ids sub-entry for the given chat_id. Returns subentry_id."""
    # No documented REST endpoint to list subentries, so we use the same approach
    # the UI does: start the subentry flow. If unique_id already exists, HA
    # returns an abort. Safe and idempotent.
    flow = _req(
        "POST",
        "/api/config/config_entries/subentries/flow",
        {
            "handler": [entry_id, "allowed_chat_ids"],
            "show_advanced_options": False,
        },
    )
    if flow.get("type") != "form":
        sys.exit(f"Unexpected subentry flow state: {flow}")

    result = _req(
        "POST",
        f"/api/config/config_entries/subentries/flow/{flow['flow_id']}",
        {"chat_id": chat_id},
    )
    if result.get("type") == "abort":
        # Already exists.
        print(f"allowed_chat_ids subentry for chat {chat_id} already exists ({result.get('reason')})")
        return ""
    if result.get("type") != "create_entry":
        sys.exit(f"Subentry flow did not create entry: {result}")
    print(f"Created allowed_chat_ids subentry: {result.get('title')} (unique_id={result.get('unique_id')})")
    return result.get("unique_id") or ""


def find_notify_entity(bot_title: str) -> str | None:
    """Return the notify.* entity_id created by the telegram_bot integration.

    HA names the entity after the bot title + the chat name (e.g.
    `notify.living_room_homeassistantxyz11_chris_collins`), so we match on
    the bot title (which is the config entry's `title` field, i.e. the bot's
    first_name from Telegram).
    """
    needle = bot_title.lower().replace(" ", "_")
    states = _req("GET", "/api/states")
    candidates = [
        s["entity_id"] for s in states
        if s["entity_id"].startswith("notify.") and needle in s["entity_id"].lower()
    ]
    return candidates[0] if candidates else None


if __name__ == "__main__":
    bot_token = _load_bot_token()
    if not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID env var is required (positive int for direct chat).")
    chat_id_int = int(CHAT_ID)

    entry_id, bot_title = ensure_config_entry(bot_token)
    ensure_chat_subentry(entry_id, chat_id_int)

    entity_id = find_notify_entity(bot_title)
    if entity_id:
        print()
        print(f"Notify entity: {entity_id}")
        print(f"Set TELEGRAM_NOTIFY_ENTITY={entity_id} when running the create_telegram_*.py scripts.")
    else:
        print()
        print("Could not find the new notify entity in /api/states yet -- it may need a few seconds.")
        print("Check Developer Tools -> States -> filter 'notify.' once HA has registered it.")
