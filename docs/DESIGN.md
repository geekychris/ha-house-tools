# Design

Architecture and rationale for **ha-house-tools**. If you want to *use* a
feature, see [USAGE.md](USAGE.md); if you want to *install*, see
[INSTALL.md](INSTALL.md); if you want the smart_ac reference, see
[SMART_AC.md](SMART_AC.md).

## What this repo solves

Home Assistant configuration drifts. You click something in the UI six
months ago, then rebuild from a backup and can't quite remember why the
telegram bot was saying that thing, or why the water-tank helper's
sliding window was 3 days not 7, or how the master-bedroom scene switch
was wired to the closet light. Multiply that by dozens of automations
and the "reinstall" story becomes "reverse-engineer live state."

This repo takes the opposite approach: **every reversible HA configuration
change is a script**. Running the scripts in order against a fresh HA
instance reproduces the live state exactly. UI clicks are for
exploration; scripts are the source of truth.

On top of the config scripts, two long-running pieces:

- **smart_ac** — a solar-aware AC scheduler that runs every 5 minutes and
  decides which of a house's air conditioners should be on. Reads
  battery SoC / solar surplus / room + outdoor temps / time of day.
  Publishes decisions to a `sensor.smart_ac_status` entity.
- **pi_tts** — a tiny HTTP-to-audio bridge. If your HA host has no audio
  hardware (typical for a Pi 4 HA install), point HA at a spare Pi with
  speakers via `rest_command` and this daemon plays it.

## Architecture at a glance

```
                        +---------------------+
                        |  Home Assistant     |
                        |  (any HA install)   |
      config scripts    |                     |
      run once each     |  - REST API         |
    +----------------> |  - WebSocket API    |
    |                   |  - config entries   |
    |                   |  - automations      |
    |                   |  - input helpers    |
    |                   +----------+----------+
    |                              ^  ^
    |                              |  |
    |                       reads  |  | flips input_boolean.ac_<room>
    |                       states |  | sets input_datetime overrides
    |                              |  |
    |                        +-----+--+------+
    |                        |  smart_ac     |     +--------------+
    |                        |  daemon       |     | pi_tts       |
    |                        |  (5-min tick, |     | (HTTP → aud) |
    |                        |   systemd)    |     |              |
    |                        +---------------+     +--------------+
    |                              ^                     ^
    |                              |                     |
    |                              | reads decisions.log | rest_command
    |                              | over local :5010    | from HA
    |                              |                     |
    +------ SolarSage (optional consumer, separate repo) --------+
                     — https://github.com/geekychris/solarsage
```

Everything in the box on the left is *this repo*. HA lives to the right
of every dependency arrow — it's the state authority, we're the
provisioner + a couple of daemons.

## Repo layout

```
ha-house-tools/
├── README.md              # start here
├── LICENSE                # MIT
├── CONTRIBUTING.md        # how to add a script / write a test
├── install_all.py         # driver that runs every setup_* script in order
├── set_telegram_bot_commands.py   # register slash commands with Telegram
├── setup_*.py             # one-time HA config: creates entities, dashboards, integrations
├── create_*.py            # per-feature automations (telegram commands, scene switches, alerts)
├── apply_tuya_magic_fix.py # a one-off Tuya no-neutral switch fix
├── smart_ac/              # long-running daemon
│   ├── smart_ac.py             # tick loop
│   ├── smart_ac.example.json   # config template — copy + edit
│   ├── calibrate.py            # measure per-AC watts once
│   ├── retrospective.py        # nightly runtime + cost report
│   ├── web.py                  # local :5010 override UI
│   ├── smart-ac.service
│   ├── smart-ac-retrospective.service
│   ├── smart-ac-retrospective.timer
│   └── smart-ac-web.service
├── pi_tts/                # HTTP → speaker
│   ├── tts_speaker.py
│   └── tts-speaker.service
├── docs/                  # DESIGN + USAGE + INSTALL + SMART_AC + openapi.yaml
└── tests/                 # pytest — pure-function coverage of smart_ac decisions
```

## Design principles

### Idempotent scripts

Every `create_*` / `setup_*` script has the same shape:

```python
1. Read HA_URL + HA_TOKEN from env (or a sibling token.txt).
2. Ask HA: "does the thing I'm about to make exist?"
3. If yes → update it in place; if no → create it.
4. Never leave partial state on failure.
```

Running any script twice is a no-op. `install_all.py` runs them in
order, so a fresh HA + a token gets you to steady state in one command.

### Config, not code, for house-specific values

House-specific facts — room slugs, entity IDs, wattages, chat IDs — go
in `smart_ac/smart_ac.json` (gitignored) or `config.json`. The scripts
read env vars + config; hardcoded values are limited to placeholder
examples in `*.example.json`.

### Reads > writes

The daemon reads a lot (states every 5 min, sensor changes on demand)
but writes are narrow: `input_boolean.ac_<room>` on/off,
`input_datetime.ac_<room>_override_until` for pins,
`sensor.smart_ac_status` for its own decision log. That means:

- If smart_ac crashes, HA still holds authoritative state.
- If HA reboots, smart_ac reconstructs state from HA on next tick.
- Manual overrides via HA UI or Telegram beat scheduler decisions —
  because they change the same `input_boolean` the scheduler reads.

### One kill switch

`input_boolean.smart_ac_enabled` — if this is `off`, the scheduler
observes but doesn't act. Toggle it from any HA surface (dashboard,
Telegram `/off smart_ac_enabled`, voice); the scheduler picks it up
on the next tick.

## Data flow: a scheduling tick

1. **06:00-ish** — cron/systemd fires the 5-minute tick.
2. **Snapshot** — read `/api/states` from HA. Extract: SoC, PV/load
   power, sun rise/set times, per-room temp sensors, per-AC input_boolean
   state + last-changed, per-AC override_until.
3. **Effective params** — figure out whether we're in normal / evening /
   unoccupied mode. Choose comfort target + priority list accordingly.
4. **Decide** — the big `decide()` function returns per-room actions:
   `keep`, `turn_on`, `turn_off`, `override_held`. Each decision has a
   reason string ("SOC 45 < morning target 60", "room 82F > target 78F",
   "held by explicit override until 22:15").
5. **Apply** — call `input_boolean.turn_on/off` for changed rooms.
   Publish the whole decision to `sensor.smart_ac_status` (attributes:
   mode, target list, per-room reason) so dashboards and Telegram can
   render it.
6. **Log** — append one JSON object to `decisions.log` (rotated at 10 MB
   × 5). Overnight, `retrospective.py` folds this into per-room minutes
   + estimated energy + $ cost and publishes to
   `sensor.smart_ac_retrospective`.

## Extension patterns

### Add a Telegram slash command

New file: `create_telegram_<name>_command.py`. Mirror any existing one.
The pattern is: create a `telegram_command` trigger, respond with
`telegram_bot.send_message`, both wrapped in an idempotent
automation-config check. Run `set_telegram_bot_commands.py` after so
the new command shows up in Telegram's autocomplete.

### Add a new AC to smart_ac

Two steps:

1. Add the room slug + wattage + priority to `smart_ac.json`.
2. Ensure the two input helpers exist:
   - `input_boolean.ac_<slug>` (see `setup_ac_input_booleans.py`)
   - `input_datetime.ac_<slug>_override_until` (see `setup_ac_override_input_datetimes.py`)
3. If you're using Alexa Media Player to actually flip the AC (via the
   bridge automations from `create_ac_toggle_automations.py`), also add
   an Alexa routine named `ac on <slug>` / `ac off <slug>` in the Alexa
   app — this is the one manual step Amazon offers no API for.

### Add a downstream consumer

Consumers read the `sensor.smart_ac_*` family and (optionally) write
overrides. See SolarSage — a separate repo — for a full example of a
consumer that surfaces smart_ac state + exposes an override UI via HA's
REST API. The contract is documented in [SMART_AC.md](SMART_AC.md).

## What this repo deliberately doesn't do

- **No HA authentication management.** HA owns tokens; you paste one
  into `token.txt` or an env var.
- **No mobile app.** HA's existing mobile app already renders
  everything smart_ac produces. If you want a bigger dashboard,
  see the SolarSage consumer.
- **No cloud dependency.** The daemon runs on your LAN; the only
  external calls are Google Translate TTS (from pi_tts) and Telegram
  (via HA's own integration).
- **No "smart training" or ML.** Decisions are legible rules over
  measurable inputs. If you want ML you can add it as another consumer
  reading `decisions.log`.

## Deployment model

Recommended two-Pi split (based on the maintainer's setup, not
required):

- **HA host** — a Raspberry Pi 4 running Home Assistant OS. Sonoff
  Zigbee coordinator + Z-Wave JS if you have Z-Wave. No audio hardware,
  no monitor. Reachable as `ha.example.local`.
- **Companion Pi** — a Raspberry Pi 5 running Debian. Has HDMI +
  speakers + a wired-in monitor if you want a wall dashboard. Runs
  `smart_ac` (5-min scheduler daemon) and `pi_tts` (HTTP-to-audio).
  Reachable as `pi.example.local`.

You *can* run smart_ac on the HA host itself (via a Docker addon or SSH
addon). The split is preferable because scheduler crashes / restarts
don't touch HA, and audio latency stays low without HA-side buffering.

Single-Pi setups just pick one of the two hosts to run both. Config +
tests are the same.
