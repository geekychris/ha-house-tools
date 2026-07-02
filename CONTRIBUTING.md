# Contributing

## Ground rules

- **Every reversible HA change is a script.** If you find yourself doing something in the HA UI more than once, write a script that does it and commit it here. That way a fresh HA can be reproduced from this repo alone.
- **Scripts are idempotent.** Running twice must be safe — check for the resource first, create only if missing.
- **Secrets never in git.** `token.txt`, `bot_token.txt`, `*.env`, `secrets.yaml`, `credentials.json` are already in `.gitignore`; keep them out.
- **House-specific values go in `config.json`** (also gitignored), not in the scripts themselves. If you spot a hardcoded room name / entity ID that should be a config knob, that's fair game for a PR.

## Development

No build, no test suite yet — scripts are stdlib + optionally `websockets`. Run any script directly:

```bash
HA_URL=http://ha.example.local:8123 HA_TOKEN=eyJ... python3 create_telegram_status_command.py
```

## Adding a new Telegram slash command

Model on any of the existing `create_telegram_*_command.py`. Each creates one HA automation triggered by `telegram_command`, replies via `telegram_bot.send_message`. After adding, remember to run `set_telegram_bot_commands.py` so the new command shows up in Telegram's autocomplete.

## Adding a new HA config script

Pattern:

```python
#!/usr/bin/env python3
"""One-line summary. What this creates, why."""
import os, urllib.request, json

HA_URL   = os.environ["HA_URL"]
HA_TOKEN = os.environ.get("HA_TOKEN") or open("token.txt").read().strip()

# ...idempotent check-then-create logic...
```

The first line of the docstring should be a full sentence — `install_all.py` (if you extend it) picks that up as the step description.

## Smart AC extensions

The scheduler is in `smart_ac/smart_ac.py`. Config lives in `smart_ac.json` (see `smart_ac.example.json`). Adding a new room: add its slug + settings to config, ensure `input_boolean.ac_<slug>` + `input_datetime.ac_<slug>_override_until` exist in HA (see `setup_ac_input_booleans.py` / `setup_ac_override_input_datetimes.py`).

## Consumer expectations

At least one consumer — [SolarSage](https://github.com/geekychris/solarsage) — reads `sensor.smart_ac_*` and writes to `input_boolean.ac_<room>` / `input_datetime.ac_<room>_override_until`. Renaming or removing those entities is a breaking change; if you do it, open a PR against SolarSage too so both repos land the rename together.
