# Smart AC — complete reference

Everything about the smart_ac subsystem in one place: what it does, why it works
that way, how you use it, how you tune it, how you extend it. The equivalent of
a "user manual + design document" for this one feature. For the rest of the
house, see [USAGE.md](USAGE.md) / [DESIGN.md](DESIGN.md).

## Table of contents

1. [What it is](#what-it-is)
2. [Why it exists (constraints of the SF house)](#why-it-exists)
3. [Architecture](#architecture)
4. [Decision logic](#decision-logic)
5. [Config reference](#config-reference)
6. [Observability](#observability)
7. [User controls](#user-controls)
8. [Overrides](#overrides)
9. [Retrospective + calibration](#retrospective--calibration)
10. [Operations](#operations)
11. [Troubleshooting](#troubleshooting)
12. [Files](#files)

---

## What it is

A Python service running on **pi-sf** (`smart-ac.service`) that evaluates every
5 minutes and decides which of the 6 house ACs should be running. Talks to HA
via REST to read state and to write `input_boolean.ac_*` toggles (which HA
automations then bridge to Alexa routines).

## Why it exists

- Off-grid house, so battery preservation matters. Turning on 6 ACs at once
  can drain the bank fast on a cloudy afternoon.
- EG4 inverter throttles PV output to match load when the battery is at 100%,
  so we can't read "how much solar is available" from the PV sensor directly.
- The B-Air ACs are on/off-only through Alexa (no setpoint via API), so we
  can't do modulating control — just yes/no per unit.
- The family has three bedrooms + three common areas, and comfort priorities
  depend on time of day (family in the living room in the evening, everyone in
  bedrooms at night, no one home when away).

A dumb "night_min always on" set is too conservative during hot afternoons and
too permissive during cloudy days. A fixed schedule doesn't adapt to weather.
So we need a scheduler that reads state and reasons about it.

## Architecture

```
                  ┌─────────────────────────────────────┐
                  │  Home Assistant  (HA host, Pi 4)    │
                  │                                     │
                  │  sensor.sna_us_15k_*  (EG4)         │
                  │  sensor.temp_humidity_* (Zbeacon)   │
                  │  input_boolean.ac_*                 │
                  │  input_boolean.smart_ac_enabled     │
                  │  input_boolean.smart_ac_notify_...  │
                  │  input_boolean.house_unoccupied     │
                  │  sensor.smart_ac_status             │
                  │  sensor.smart_ac_retrospective      │
                  │  sensor.smart_ac_calibration        │
                  │  automation.ac_toggle_<room>        │
                  └───────────────┬─────────────────────┘
                                  │ REST
              ┌───────────────────┼────────────────────┐
              │                   │                    │
              ▼                   ▼                    ▼
         reads state       writes toggles         POST sensor states
              │                   │                    │
              │                   │                    │
              └───────────────────┼────────────────────┘
                                  │
                  ┌───────────────┴─────────────────────┐
                  │  pi-sf (Pi 5, Debian, always on)    │
                  │                                     │
                  │  smart-ac.service       (scheduler) │
                  │    -> reads /home/chris/smart_ac_state.json
                  │    -> writes decisions.log          │
                  │  smart-ac-retrospective.timer/.service (nightly)
                  │  smart-ac-web.service   (port 5010) │
                  │                                     │
                  └─────────────────────────────────────┘
                                  ▲
                                  │ Telegram commands
                                  │ (/smart_ac, /smart_ac_report,
                                  │  /override, /ac ...)
                                  ▲
                             User's phone
```

**Every 5 minutes the scheduler:**

1. Reads state from HA (REST): time, sun, SoC, battery_power, PV, load,
   temperatures, current AC states, all three scheduler toggles.
2. Consults its own state file for prior action timestamps and any pinned
   `manual_override_until` per room.
3. Decides target ON/OFF per room via [the decision logic](#decision-logic).
4. Applies changes subject to hysteresis (min_on_minutes / min_off_minutes)
   by calling `input_boolean.turn_on/off` — which chains to the existing
   automation → Alexa routine → SmartLife → AC.
5. Publishes decision to `sensor.smart_ac_status`, appends a JSON record to
   `decisions.log`, writes a HA Logbook entry (only on mode change or actions),
   optionally pushes a Telegram message (if `smart_ac_notify_telegram` is on).

## Decision logic

### Time-of-day periods

Three periods, based on HA's sun entity + `bedtime_hour` config:

| Period | Boundaries | Purpose |
|---|---|---|
| **DAY** | between `sunrise + sun_offset_morning_min` and `sunset - sun_offset_evening_min` | Solar is producing. Can be aggressive. |
| **EVENING** | between `sunset - sun_offset_evening_min` and `bedtime_hour` (local) | Family still up but solar is done. Keep family-area ACs + only the master bedroom (not all bedrooms — kids/guests are in the family area). |
| **NIGHT** | between `bedtime_hour` and `sunrise + sun_offset_morning_min` (next day) | Everyone asleep. All bedrooms as configured in `night_min_acs`. |

### Modes within each period

**DAY period** has four possible sub-modes based on battery dynamics:

| Mode | Trigger | Target |
|---|---|---|
| **SURPLUS** | `SoC >= soc_target_at_dark` | night_min + all priority extras whose rooms need cooling |
| **DEFICIT** | `battery_power <= 0` (discharging or stalled) | night_min only |
| **CHARGE_BEHIND** | Charging, but `(battery_power_w/1000) × time_until_dark < kwh_to_full` | night_min only |
| **ON_TRACK** | Charging fast enough to hit `soc_target_at_dark` by dark | night_min + ONE extra from priority |

The key insight: since the EG4 throttles PV output when demand drops (SoC=100%
would show pv=load), we don't read the PV sensor to estimate surplus. Instead we
read the **battery** — if it's charging, solar > load; if it's discharging, solar
< load. This gives us a real-time truth source that adapts to weather without
needing a separate solar forecast.

**EVENING period** has one mode:

- **EVENING**: target = `evening_min_acs` + `evening_extra_required`.
  Default: `["master"]` + `["living"]` = `[master, living]`. Just the master
  bedroom (family's main sleeping area, always maintained) plus the living
  room (family-area comfort until bedtime).

**NIGHT period** has one mode:

- **NIGHT**: target = `night_min_acs`. Default `[master, kyle, guest]`.
  All three bedrooms come back online at bedtime for sleep comfort.

### Per-room "cooling needed" gate

Regardless of the mode, we don't add an AC just because a slot is available —
we check whether the room actually needs cooling first:

- **Sensored rooms** (`living`, `master` — the two with Zbeacon TH01s):
  `indoor > comfort_target_f`. Default 78°F.
- **Unsensored rooms** (`guest`, `dining`, `office`, `kyle`):
  fall back to outdoor. Add only if `outdoor > unsensored_assume_hot_above_outdoor_f`.
  Default 80°F.

So on a 90°F afternoon with living at 79°F: living qualifies (79>78), all
unsensored rooms qualify (90>80). On a 72°F afternoon with living at 76°F:
nothing qualifies except night_min (which runs regardless of temp, since it's
the safety-minimum set).

### Hysteresis

- Don't turn ON unless the AC has been OFF for at least `min_off_minutes` (default 10).
- Don't turn OFF unless it's been ON for at least `min_on_minutes` (default 15).
- **Manual overrides are explicit only.** The old "auto-detect that a user
  toggled the input_boolean and pin for 30 min" heuristic was removed — it
  fought the explicit override system (setting `/override <room> on until <T>`
  ALSO flipped the input_boolean, which the auto-detection saw as a raw user
  action and extended the pin past `<T>`). If you want a "hold for 30 min",
  use `/override <room> for 30m` (or without on/off to pin the current state).

### Unoccupied mode

When `input_boolean.house_unoccupied` is ON, several config keys get overridden
and the priority list rotates daily:

| Occupied key | Unoccupied override | Default |
|---|---|---|
| `night_min_acs` | `unoccupied_night_min_acs` | `[]` |
| `evening_min_acs` | `unoccupied_evening_min_acs` | `[]` |
| `evening_extra_required` | `unoccupied_evening_extra_required` | `[]` |
| `comfort_target_f` | `unoccupied_comfort_target_f` | 82°F |
| `unsensored_assume_hot_above_outdoor_f` | `unoccupied_unsensored_assume_hot_above_outdoor_f` | 90°F |
| (max ACs cap) | `unoccupied_max_acs_total` | 2 |

Plus `day_priority` gets **rotated by day-of-year** so each AC takes equal turns
as the "first added" extra over a multi-day vacation.

## Config reference

Full config is `/home/chris/smart_ac/smart_ac.json` on pi-sf. Edit, then
`sudo systemctl restart smart-ac`.

**Time / mode boundaries**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sun_offset_morning_min` | int | 90 | Start solar day this many minutes after sunrise |
| `sun_offset_evening_min` | int | 120 | End solar day this many minutes before sunset |
| `bedtime_hour` | int | 22 | Local hour when EVENING ends and NIGHT begins. `<12` is interpreted as next morning (e.g. `1` = 1am tomorrow). |

**Charge target**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `soc_target_at_dark` | int | 100 | Battery SoC % we want by sunset. Drop to 95 for a small buffer. |
| `battery_ah` | int | 840 | Bank capacity in Ah, used only for the kWh trajectory calc |
| `battery_nominal_v` | float | 51.2 | LFP nominal, used only for the kWh calc |

**Room sets**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `night_min_acs` | list | `[master, kyle, guest]` | Required during NIGHT (post-bedtime) and in DEFICIT/CHARGE_BEHIND during DAY |
| `evening_min_acs` | list | `[master]` | Required during EVENING (post-sun, pre-bedtime). Usually smaller than night_min. Falls back to night_min_acs if unset. |
| `evening_extra_required` | list | `[living]` | Added to evening_min during EVENING |
| `day_priority` | list | `[living, dining, office]` | Order in which extras are added in ON_TRACK / SURPLUS |

**Comfort thresholds**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `comfort_target_f` | float | 78 | Sensored-room indoor threshold |
| `unsensored_assume_hot_above_outdoor_f` | float | 80 | Outdoor threshold for unsensored rooms |
| `ac_power_estimate_w` | int | 1000 | Not currently used in the on/off decision (kept for future) |

**Hysteresis**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `min_on_minutes` | int | 15 | Minimum on-time before we'll turn off |
| `min_off_minutes` | int | 10 | Minimum off-time before we'll turn on |
| `manual_override_minutes` | int | 30 | How long a user's manual toggle overrides scheduler decisions |

**Cadence**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `evaluation_interval_minutes` | float | 5 | How often to tick |

**Toggles**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled_entity` | str | `input_boolean.smart_ac_enabled` | Master kill-switch (default ON) |
| `notify_entity_toggle` | str | `input_boolean.smart_ac_notify_telegram` | Stream mode changes + actions to Telegram (default OFF) |
| `notify_target` | str | `notify.living_room_homeassistantxyz11_chris_collins` | Telegram notify entity |
| `unoccupied_entity` | str | `input_boolean.house_unoccupied` | Vacation mode (default OFF) |

**Sensor entity IDs** — change if you move things around in HA:

| Key | Default |
|---|---|
| `soc_sensor` | `sensor.sna_us_15k_53562j0683_state_of_charge` |
| `battery_power_sensor` | `sensor.sna_us_15k_53562j0683_battery_power` |
| `pv_power_sensor` | `sensor.sna_us_15k_53562j0683_pv_total_power` |
| `load_sensor` | `sensor.living_room_eg4_total_load_power` |
| `outdoor_sensor` | `sensor.temp_humidity_temperature_2` |
| `indoor_sensor_for_room` | `{"living": "sensor.temp_humidity_temperature", "master": "sensor.temp_humidity_temperature_3"}` |
| `status_sensor_entity` | `sensor.smart_ac_status` |

## Observability

Five ways to see what the scheduler is doing, from most-immediate to most-historical:

| Surface | Coverage | How to access |
|---|---|---|
| Web UI dashboard (`http://pi-sf:5010/`) | Live snapshot; refreshes every 60s | Any browser on LAN |
| Telegram `/smart_ac` | Same as dashboard, in chat | Any Telegram client |
| Web UI `/decisions` | Last 30 ticks in a table (with per-action reasons) | Any browser |
| Telegram notification stream | Live push on mode change + actions (when `smart_ac_notify_telegram` is on) | Any Telegram client |
| HA Logbook | Human-readable mode transitions + actions, from HA history | HA UI → Logbook |
| pi-sf `journalctl -u smart-ac -f` | INFO line per tick with reasons | SSH |
| pi-sf `~/smart_ac/decisions.log` | JSON per tick, rotating | SSH or `jq` |
| Web UI `/reports/YYYY-MM-DD` | Nightly retrospective as HTML | Any browser |
| Telegram `/smart_ac_report` | Retrospective attributes as chat message | Any Telegram client |
| Web UI `/status.json` | JSON of all three sensors | For scripting |

## User controls

The dashboard's "Air Conditioners" card and the Telegram bot together give you
five layers of control:

1. **Master kill-switch** (`smart_ac_enabled`). Off = scheduler still runs but
   doesn't apply. Use for full manual control.
2. **Per-AC toggles** on the dashboard (or `/ac on <room>` / `/ac off <room>`
   in Telegram). Immediate. Scheduler respects your manual choice for
   `manual_override_minutes` (30 min default).
3. **Explicit override with end time** — new, see [Overrides](#overrides).
4. **House unoccupied** switches modes (relaxed targets, hard cap, rotation).
5. **Notify on Telegram** streams decisions in real time.

## Overrides

Sometimes you know you want an AC on/off for a specific window — a nap, a
dinner party, a hot afternoon, etc. The scheduler exposes overrides through
six HA-native `input_datetime.ac_<room>_override_until` helpers. Any of them
in the future = that room is pinned. Anything else (past date, missing, or
the 1970-01-01 "cleared" value) = no override.

### Three interchangeable ways to set overrides

All three read/write **the same six input_datetime helpers**. Change from any
and the other two reflect it immediately.

**A. Telegram**

```
/override <room> on until <spec>     -- turn AC on + pin until <spec>
/override <room> off until <spec>    -- turn AC off + pin until <spec>
/override <room> on for <duration>   -- turn on + pin for <duration>
/override <room> off for <duration>  -- turn off + pin for <duration>
/override <room> until <spec>        -- pin CURRENT state (no flip)
/override <room> for <duration>      -- pin current state for <duration>
/override <room> clear               -- remove the override
/override list                       -- show active overrides
```

Time / duration forms (parsed in Jinja at automation time, local timezone):

| Form | Meaning |
|---|---|
| `HH:MM` | Today at HH:MM local (rolls to tomorrow if past) |
| `+2h` or `2h` | Two hours from now |
| `+30m` or `30m` | Thirty minutes from now |

Examples:

- `/override living on until 23:00` — turn living AC ON, keep it on until 11pm.
- `/override master for 2h` — pin master's current state for 2 hours.
- `/override kyle clear` — remove kyle's override.

**B. Dashboard**

On the energy dashboard, the **Air Conditioners** card has two override
subsections:

- **Active overrides** — live-computed summary. Only shows rooms whose
  input_datetime is currently in the future. If nothing is pinned:
  `_none_`.
- **Set / edit overrides** — six rows, one per room. Tap any row → HA's
  built-in datetime picker opens → pick a future date + time → save.
  Set to a past date (or 1970-01-01) to clear.

**C. Web UI (pi-sf browser dashboard)**

`http://pi.example.local:5010/overrides` — shows the active overrides in a
table and has a form to set new ones (Room + State + Until fields). Same
underlying HA input_datetime helpers.

### No rest_command YAML paste needed

The original design routed through `rest_command.smart_ac_override` +
pi-sf's `POST /override` endpoint. That's gone. Telegram now calls
`input_boolean.turn_on/off` (built-in) + `input_datetime.set_datetime`
(built-in) directly — no YAML changes to HA, no dependency on smart-ac-web
being running for the override feature to work. smart-ac-web is still
useful for the browsable /reports and /decisions views, but `/override` is
independent of it.

### One-time setup: create the input_datetime helpers

Run once against a fresh HA:

```
python3 setup_ac_override_input_datetimes.py
```

Creates six helpers via HA WS API (`input_datetime/create`). Idempotent
— safe to re-run.

## Retrospective + calibration

### Retrospective

Runs every night at 00:30 local via `smart-ac-retrospective.timer`. Analyzes
the last 24h. Produces:

- Markdown report at `~/smart_ac/reports/YYYY-MM-DD.md`
- `sensor.smart_ac_retrospective` attributes for consumption by the dashboard
  card, Telegram `/smart_ac_report`, and the web `/reports/<date>` view.

**What's in the report:**

- SoC start / peak / end for the window (local time)
- Time in each mode (minutes)
- Per-AC runtime (minutes ON)
- Per-AC estimated draw (median load-delta around single-AC transitions with no
  other AC transitioning within ±4 min)
- **Per-AC estimated energy + $ cost.** Runtime × watts × rate. Watts source is
  labelled `measured` (from the load-delta analysis, when the window contains
  isolated transitions for that room) or `default` (from
  `ac_power_estimate_w` in `smart_ac.json`, 1000 W default). Rate defaults to
  `$0.30/kWh` (matches the main dashboard's Off-Grid Savings card); override
  via `GRID_RATE_USD_PER_KWH` env at retrospective.py runtime. All figures are
  approximations — AC compressors are variable-speed and duty-cycle, so real
  numbers can differ ±30%.
- Full action timeline with per-room reasons in a table

Run on demand:
```
ssh $PI_HOST 'cd /home/chris/smart_ac && . smart_ac.env && python3 retrospective.py'
# or specify a date:
ssh $PI_HOST 'cd /home/chris/smart_ac && . smart_ac.env && python3 retrospective.py 2026-06-29'
```

### Calibration

Interactive one-shot measurement. When the house is otherwise quiet (no
cooking, no big loads coming/going), run:

```
ssh $PI_HOST 'cd /home/chris/smart_ac && . smart_ac.env && python3 calibrate.py'
```

Takes ~21 minutes. For each of the 6 ACs:
- Ensures it's OFF; waits 60s to settle; samples baseline load
- Turns ON; waits 90s to settle; samples running load
- Turns OFF; waits 60s to recover before the next AC

Reports baseline / running / delta W per AC. Updates `sensor.smart_ac_calibration`.

Best used when the retrospective's "estimated draw" doesn't have enough
samples yet (early in a fresh deployment, or for a specific AC that hasn't
had isolated transitions).

You can also trigger it from the web UI: `/calibrate` → "Start calibration".
It runs detached; the page tails `calibrate.log` while it's going.

## Operations

### Deploy code changes

```
cd ~/code/claude_world/homeassistant
python3 setup_smart_ac.py            # scp all files to pi-sf
ssh $PI_HOST 'sudo systemctl restart smart-ac smart-ac-web'
```

### Rotate the pi-sf HA token

1. HA → Profile → Security → Long-Lived Access Tokens → Create.
2. Save to `~/code/claude_world/homeassistant/pi_sf_ha_token.txt` (overwrite existing).
3. `python3 setup_smart_ac.py` (writes the new token into `smart_ac.env` on pi-sf).
4. `ssh $PI_HOST 'sudo systemctl restart smart-ac smart-ac-web smart-ac-retrospective.timer'`.

### Tune configuration

Edit `/home/chris/smart_ac/smart_ac.json` on pi-sf. Then
`sudo systemctl restart smart-ac`.

## Troubleshooting

| Symptom | Probable cause | Fix |
|---|---|---|
| Scheduler doesn't apply any actions | `smart_ac_enabled` is OFF | Toggle it ON |
| Scheduler applies actions but ACs don't respond | Alexa routine missing / bad name | Check Alexa app: routines must be named EXACTLY `ac on <room>` / `ac off <room>` |
| `/smart_ac` in Telegram shows stale state | Service crashed | `systemctl status smart-ac` on pi-sf; look at `journalctl -u smart-ac -n 50` |
| Telegram notifications don't fire even with toggle on | Toggle was OFF at time of change, or service running old code | Toggle it off/on; if still no messages, `sudo systemctl restart smart-ac` — the running process caches code |
| No decisions.log entries | Service isn't ticking | Check `systemctl status smart-ac` and the journal |
| Retrospective report is empty | Not enough data in the window | Retrospective runs at 00:30; if just deployed, wait a few days |
| Estimated per-AC draws all zero | No single-AC transitions in window | Run `calibrate.py` for a controlled measurement |
| Mode is wrong | Config not matching actual conditions | Adjust `sun_offset_*`, `bedtime_hour`, `soc_target_at_dark`. Restart. |
| Web UI shows raw markdown | Service running old code | `sudo systemctl restart smart-ac-web` |
| Override does nothing | rest_command YAML not pasted / reloaded | See [Overrides § prereq](#prereq-rest_command-in-configurationyaml-one-time) |

## Files

Everything is in either the repo or on pi-sf. On pi-sf, in `/home/chris/smart_ac/`:

| File | What |
|---|---|
| `smart_ac.py` | Decision engine (systemd `smart-ac.service`) |
| `smart_ac.json` | Runtime config (edit + restart) |
| `smart_ac.env` | HA_TOKEN + HA_URL for systemd `EnvironmentFile=` |
| `smart-ac.service` | Systemd unit for the scheduler |
| `web.py` | HTTP UI on port 5010 (systemd `smart-ac-web.service`) |
| `smart-ac-web.service` | Systemd unit for the web UI |
| `retrospective.py` | Nightly analysis + report generator |
| `smart-ac-retrospective.service` + `.timer` | Systemd timer at 00:30 |
| `calibrate.py` | Interactive per-AC power measurement |
| `reports/YYYY-MM-DD.md` | Nightly reports written by retrospective.py |
| `decisions.log` | JSON-per-tick log (rotates at 10MB × 5) |
| `calibrate.log` | stdout from an in-progress calibration |
| `../smart_ac_state.json` | Scheduler internal state (last_action_at, manual_override_until) |

In this repo, at the top level, everything that deploys smart_ac:

| File | What |
|---|---|
| `smart_ac/*` | All the pi-sf source files |
| `setup_smart_ac.py` | scp + sudo instructions |
| `setup_smart_ac_input_boolean.py` | Creates the three input_boolean toggles |
| `create_telegram_smart_ac_command.py` | `/smart_ac` reply |
| `create_telegram_smart_ac_report_command.py` | `/smart_ac_report` reply |
| `create_telegram_override_command.py` | `/override` command |
