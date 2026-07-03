# Programming model

How the code in this repo talks to Home Assistant, and how to write more
of it. For architectural rationale (why scripts vs custom_components),
see [DESIGN.md](DESIGN.md). For a house-configurator's guide, see
[USAGE.md](USAGE.md).

## The big idea

Almost nothing in this repo runs *inside* Home Assistant. HA is treated
as a state store + rules engine + event bus, accessed over its two
network APIs (REST + WebSocket). Every piece of code is either:

- A **client** that hits HA's APIs to configure it or read/write state.
- A **daemon** that treats HA as a database while making its own decisions.
- An **HA-native artifact** (automation, input helper, sensor) that we
  **author in Python and then upload** via HA's REST API.

Contrast this with a typical Home Assistant custom component, which
lives inside HA's asyncio loop, ships with `manifest.json` +
`config_flow.py`, and gets managed via HACS. Nothing here uses that
model. The tradeoffs of the choice are covered in DESIGN.md; this doc
is about how the substrate we chose actually works.

## The five substrates

Every file in the repo is one of five things:

| # | Substrate | Where it runs | What it produces |
|---|---|---|---|
| 1 | Idempotent config script | Your laptop or CI | HA-side state (automations, input helpers, dashboards) |
| 2 | Long-running daemon | A separate Pi | Decisions expressed as writes to HA-native entities |
| 3 | HA automation authored in Python | HA | Event-triggered actions running in HA's own automation engine |
| 4 | Input helper | HA | User-facing state (booleans, datetimes, numbers) |
| 5 | HA sensor entity | HA (populated from outside) | Read-only state exposed to dashboards, cards, and other consumers |

The rest of this doc explains each substrate in detail.

---

## Substrate 1: Idempotent config scripts

**Location.** Root of the repo. Filenames:

- `create_<thing>_automation.py` or `create_<thing>_command.py` — creates a
  HA automation.
- `setup_<thing>.py` — creates HA input helpers, statistics sensors, config
  entries, or dashboards.
- `apply_<thing>.py` — one-shot service calls (e.g. `apply_tuya_magic_fix.py`).

**Programming model.** Each script:

1. Reads `HA_URL` + `HA_TOKEN` from env (or `token.txt` fallback).
2. Constructs a JSON payload describing the desired HA state.
3. `POST`s (or WebSocket-sends) that payload to a well-known HA API endpoint.
4. Exits.

Nothing is stored anywhere locally — HA persists the change.

**Example: a Telegram command automation.**

```python
# create_telegram_help_command.py — condensed
import json, os, urllib.request

HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]

AUTOMATION_ID = "telegram_help_command"   # stable ID -> re-runs overwrite

AUTOMATION_CONFIG = {
    "alias": "Telegram /help command",
    "mode": "single",
    "triggers": [{
        "trigger": "event",
        "event_type": "telegram_command",
        "event_data": {"command": "/help"},
    }],
    "actions": [{
        "action": "notify.send_message",
        "target": {"entity_id": "notify.living_room_..."},
        "data": {"message": HELP_TEXT},
    }],
}

req = urllib.request.Request(
    f"{HA_URL}/api/config/automation/config/{AUTOMATION_ID}",
    data=json.dumps(AUTOMATION_CONFIG).encode(),
    headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    },
    method="POST",
)
urllib.request.urlopen(req).read()
```

**What HA does with this.**

- POST body is validated against HA's automation schema.
- HA writes to `automations.yaml` (in `/config/`).
- HA fires an internal `automation_reloaded` event.
- The new automation is live within a second, indexed by the stable ID.

The **stable ID** in the URL (`telegram_help_command`) is what makes this
idempotent. Re-running the script replaces the previous automation at
that ID; different scripts pick different IDs and don't collide.

**Why REST over YAML paste.** `automations.yaml` is an HA-native format
you could `scp` and reload. We POST via the REST API instead because it
returns a proper error if the schema is wrong, and because it doesn't
require SSH access to `/config/`.

**WS variant for a small class of scripts.** Some HA state can only be
written over the WebSocket API — notably `input_boolean/create`,
`input_datetime/create`, and `lovelace/config/save`. Those scripts
(`setup_ac_input_booleans.py`, `create_energy_dashboard.py`) use
`websockets` instead of `urllib`, but the shape is the same: connect,
auth, send one command, wait for the matching reply, exit.

## Substrate 2: Long-running daemons

**Location.** `smart_ac/*.py`, `weather/*.py`.

**Programming model.** A plain Python `while True:` loop (or systemd
timer firing a one-shot Python script) that:

1. Reads state from HA via REST (`GET /api/states`, `/api/history/...`).
2. Makes decisions in pure Python.
3. Writes decisions back to HA — either as service calls
   (`input_boolean.turn_on`) or as state pushes (`POST /api/states/<eid>`
   to update a sensor).
4. Sleeps until the next tick.

**Example: the smart_ac scheduler.**

```python
# smart_ac/smart_ac.py — pseudo-code
def tick():
    states = ha_get("/api/states")            # snapshot everything at once
    snap = Snapshot(states, cfg)              # parse into a typed struct
    sched = SchedulerState.load()             # persistent state on disk
    target, mode, reasons = decide(snap, sched, cfg)
    actions = apply_targets(target, snap, sched, cfg)   # hits input_boolean.turn_on/off
    sched.save()
    update_status_sensor(snap, mode, target, reasons, actions)  # POST /api/states/sensor.smart_ac_status
    log_decision(...)                         # append to decisions.log

def main():
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("tick failed")
        time.sleep(cfg["evaluation_interval_minutes"] * 60)
```

**How HA sees the daemon.**

- HA sees requests hitting its REST API from some IP (pi-sf's).
- The service calls (`input_boolean.turn_on`) look exactly like clicks
  from the UI.
- The sensor state pushes (`sensor.smart_ac_status`) create a virtual
  sensor. HA doesn't know or care whether that sensor is backed by an
  MQTT integration, a template, or (in our case) a periodic POST from
  outside.
- The two are indistinguishable from HA's perspective from a
  first-class custom component doing the same work.

**Persistent daemon state.** The scheduler needs to know "when did I
last flip room X" across restarts. We keep that in
`/home/chris/smart_ac_state.json` — plain JSON, read/write per tick.
HA is *not* the store of record for anything the scheduler considers
private state. HA holds the observable state; the daemon holds its
own internal state.

**Lifecycle.** systemd service unit + optional systemd timer:

```ini
# smart-ac.service
[Service]
Type=simple
EnvironmentFile=/home/chris/smart_ac/smart_ac.env
ExecStart=/usr/bin/python3 /home/chris/smart_ac/smart_ac.py
Restart=on-failure

# smart-ac-retrospective.timer
[Timer]
OnCalendar=*-*-* 00:30:00
Persistent=true
```

`Type=simple` for long-running loops (`smart_ac.py`); `Type=oneshot`
plus a paired timer for periodic scripts (`retrospective.py`,
`weekly.py`, `openmeteo.py`, `anomaly.py`, `fridge_monitor.py`).

## Substrate 3: HA automations authored in Python

**When to use.** For **event-driven glue** that HA needs to react to
instantly (button presses, incoming Telegram commands, state changes).
Running these in a pi-sf daemon would add HA REST round-trip latency
and would fight the event-bus semantics.

**Programming model.** Same as Substrate 1 — you build a JSON dict and
POST it. The dict IS the HA automation schema. What differs is what
you put in the dict: `triggers`, `conditions`, `actions`, and a lot of
Jinja templating.

**Example: /override.**

```python
# create_telegram_override_command.py — condensed
ROOM_TEMPLATE = "{{ trigger.event.data.args[0] | lower }}"
STATE_TEMPLATE = "{% if trigger.event.data.args[1] in ['on','off'] %}..."

AUTOMATION_CONFIG = {
    "triggers": [{
        "trigger": "event",
        "event_type": "telegram_command",
        "event_data": {"command": "/override"},
    }],
    "variables": {                    # computed once, referenced below
        "room": ROOM_TEMPLATE,
        "state": STATE_TEMPLATE,
        # ...
    },
    "actions": [{
        "choose": [                   # if/elif/elif/else
            {
                "conditions": [{"condition": "template",
                                "value_template": "{{ not room or not verb }}"}],
                "sequence": [{"action": "notify.send_message",
                              "data": {"message": REPLY_USAGE}}],
            },
            # ... more branches
        ],
        "default": [                  # the "else" branch
            {"action": "input_boolean.turn_on",
             "target": {"entity_id": "input_boolean.ac_{{ room }}"}},
            {"action": "input_datetime.set_datetime",
             "target": {"entity_id": "input_datetime.ac_{{ room }}_override_until"},
             "data": {"datetime": "{{ target_dt }}"}},
        ],
    }],
}
```

**Where the actual logic lives.** Look carefully: the Python you're
writing is just an assembly of Jinja fragments + service call
descriptors. When Telegram fires the event, HA's automation engine —
not your Python — parses the args, evaluates the templates, and runs
the actions. Your Python only ran at *config time* to install the
dict.

**Why do it in Python then?** Because writing this as raw
`automations.yaml` is unpleasant, string escaping is confusing, and
you get no help from a type checker. Writing it as a Python dict lets
you use variables, list comprehensions, and unit tests. The dict is
serialized to JSON at the end and shipped to HA verbatim.

**The Jinja templating layer.** HA automations get **two** template
contexts:

- **Trigger-scoped templates** in `variables:` are evaluated once when
  the automation fires, using `trigger.event.data` (etc.). Great for
  parsing incoming arguments.
- **Action-scoped templates** inside individual `actions:` items are
  evaluated when each action runs. Useful when you need `now()` right
  at the moment of the action.

Passing a value from the trigger scope to an action is done via the
`variables:` block, which acts like locals visible to all subsequent
templates.

**Gotcha: Telegram's Markdown parser.** `notify.send_message` sends the
`message` field with `parse_mode: Markdown` by default. An unpaired `_`
or `*` in your text (e.g. from a filename like `_ac`) breaks the whole
message with a cryptic HTTP 400 from Telegram. The repo has one hard
rule: **don't emit unpaired `_`, `*`, `[`, or `` ` `` in Telegram
replies**. See the top of `create_telegram_smart_ac_report_command.py`
for the incident report.

## Substrate 4: Input helpers

**What they are.** HA-native user-facing state:
`input_boolean.smart_ac_enabled`, `input_datetime.ac_master_override_until`,
`input_number.foo`, etc. First-class HA entities, editable in the UI,
readable via REST, subscribable via WebSocket.

**How we create them.** WS API only — the REST API doesn't expose the
creation endpoint. Each `setup_*_input_boolean*.py` script:

```python
async with websockets.connect(HA_URL, max_size=None) as ws:
    await ws.recv()   # auth_required
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    await ws.recv()   # auth_ok

    # Idempotency: list existing, skip if the name is already there
    await ws.send(json.dumps({"id": 1, "type": "input_boolean/list"}))
    existing = {r["name"] for r in (await ws.recv())["result"]}

    if "Smart AC party mode" not in existing:
        await ws.send(json.dumps({
            "id": 2,
            "type": "input_boolean/create",
            "name": "Smart AC party mode",
            "icon": "mdi:party-popper",
        }))
```

**How they're used.** The pattern the whole repo leans on:

- **Booleans as toggles**: `input_boolean.smart_ac_enabled` is the
  scheduler kill switch. The daemon reads it every tick; if `off`, it
  makes decisions but suppresses actions.
- **Datetimes as pins**: `input_datetime.ac_<room>_override_until`. Any
  future value = the room is pinned. Any past value (including the
  1970-01-01 clear sentinel) = no override. This trick lets us express
  "override until 22:00" without inventing a new state store.
- **Booleans as triggers for other automations**: `smart_ac_nap_mode`
  going on fires `create_smart_ac_nap_mode_automation.py`'s automation,
  which sets an override + waits 60m + turns the boolean back off.

**Why not just use HA's `automation` state store or its scheduler?**
Because input helpers are:
- Visible to the user (dashboard toggles).
- Editable from any client (Telegram, SolarSage, direct UI clicks).
- Easy to reason about (single source of truth per concept).

## Substrate 5: Sensor entities populated from outside

**Where they come from.** The daemons `POST /api/states/<entity_id>`
with a body like:

```json
{
  "state": "SURPLUS",
  "attributes": {
    "friendly_name": "Smart AC status",
    "icon": "mdi:air-conditioner",
    "mode": "SURPLUS",
    "target_on": ["master", "kyle", "living"],
    "reasons": {"living": "added (ON_TRACK: 1-extra slot, room needs cooling)"},
    ...
  }
}
```

**What HA does.** Treats the entity as if some integration created it.
It's queryable via REST, subscribable via WebSocket, shows up in the
entity picker, can be graphed on the dashboard, referenced from
automations, exposed to the OpenAPI spec, etc.

**Gotchas.**

- **Not persistent across HA restarts.** If HA reboots, the entity
  disappears until the daemon pushes another state. That's usually
  fine (`smart_ac.py` pushes every 5 min) but worth knowing. If you
  need persistence, use a template sensor backed by the state file
  instead.
- **Attribute size limits.** HA truncates enormous attribute dicts
  and refuses to write beyond a few hundred KB. Keep payloads small.
- **No `unique_id`** means the user can't rename via the UI. If
  renamability matters, register through an MQTT sensor + auto-
  discovery instead. For our purposes it doesn't.

## Worked example: the full override flow

Following one Telegram message through every substrate.

**Setup phase (once, when you deploy this stuff):**

1. `setup_ac_input_booleans.py` (WS) creates
   `input_boolean.ac_{master,guest,dining,living,office,kyle}`.
2. `setup_ac_override_input_datetimes.py` (WS) creates six
   `input_datetime.ac_<room>_override_until` helpers.
3. `create_telegram_override_command.py` (REST) creates the automation
   that listens for `/override` Telegram commands.
4. `create_ac_toggle_automations.py` (REST) creates six automations
   that bridge `input_boolean.ac_<room>` state changes to the matching
   Alexa routine.

**Runtime flow (every time you type `/override living on until 23:00`):**

1. **Telegram → HA.** Telegram Bot API delivers the message to HA's
   Telegram integration, which fires a `telegram_command` event on the
   HA event bus with `args=["living", "on", "until", "23:00"]`.
2. **HA event bus → automation engine.** The automation created in
   step 3 above matches on `event_type=telegram_command` and
   `event_data.command=/override`. HA schedules the automation.
3. **Automation → Jinja templates.** Trigger-scoped variables compute
   `room=living`, `state=on`, `verb=until`, `spec=23:00`,
   `target_dt=2026-07-01 23:00:00`.
4. **Automation → HA services.** The default branch calls
   `input_boolean.turn_on` for `input_boolean.ac_living`, then
   `input_datetime.set_datetime` for
   `input_datetime.ac_living_override_until`.
5. **`ac_toggle_living` automation triggers.** The
   `input_boolean.ac_living` state change fires it. That automation
   calls `media_player.play_media` on Alexa with the routine name
   `ac on living`.
6. **Alexa → SmartLife → physical AC.** Alexa runs its routine, which
   sends the SmartLife command, which turns the AC on.
7. **Confirmation Telegram reply.** The `/override` automation's final
   action sends a `notify.send_message` back to Telegram: `Override
   set: living ON + pinned until 2026-07-01 23:00:00.`
8. **Meanwhile, on pi-sf, the scheduler ticks (5 min later).**
   `smart_ac.py` reads all six `input_datetime.ac_<room>_override_until`
   values. It sees `ac_living_override_until = 2026-07-01 23:00:00`,
   which is in the future. It marks living as pinned and does not
   touch `input_boolean.ac_living`.
9. **Eventually 23:00 passes.** The next tick reads the datetime, sees
   it expired, and lets normal decision logic apply.

**Substrates involved:** 1 (setup scripts), 2 (pi-sf daemon), 3
(automations), 4 (input helpers), 5 (status sensor). All five.
Nothing is a custom_component. HA glues them all together via its
event bus and REST/WS APIs.

## Testing and deploy

**Fast iteration on a config script.** Run it locally, hit HA, watch
the response:

```bash
export HA_URL=http://ha-sf.hitorro.com:8123
export HA_TOKEN=$(cat token.txt)
python3 create_telegram_help_command.py
# OK: {"result":"ok"}
```

Then verify in HA UI → Settings → Automations & Scenes. The new
automation shows up with your alias.

**Fast iteration on a daemon.** Edit `smart_ac/smart_ac.py`, scp it
via `setup_smart_ac.py` (which also `sudo systemctl restart smart-ac`s
the running service), watch `journalctl -u smart-ac -f` on pi-sf.

Watch out for the **restart trap**: `setup_smart_ac.py` scp's the
files, but if `sudo -n` fails (timestamp expired), the running service
keeps executing the *previous* in-memory copy. This bit us in a real
incident; see the smart-ac-restart NOPASSWD sudoers snippet in
`setup_smart_ac.py`'s output.

**Fast iteration on an HA-authored automation.** Same as any config
script — re-run the Python file, HA reloads the automation. No HA
restart needed; the automation engine hot-reloads by design.

## When to reach for which substrate

- **A one-off config change.** Substrate 1 (script + POST).
- **A continuous decision loop that reads live state.** Substrate 2
  (daemon + timer).
- **A "when X happens, do Y" reaction.** Substrate 3 (HA automation).
- **User-facing state that needs to survive restarts and be editable in
  the UI.** Substrate 4 (input helper).
- **Read-only observable state that other tools should be able to
  subscribe to.** Substrate 5 (external sensor push).

Almost every new feature is a combination. Anomaly detection
(recently added) uses Substrate 2 (a `weather/anomaly.py` script under
a timer), reads Substrate 5 entities (`sensor.weather_expected_pv_now`
populated by `openmeteo.py`), and calls Substrate 3 automations only
tangentially (the notify service). The scheduler mode toggles
(also recent) use Substrate 4 (three new input_booleans) that
Substrate 2 (`smart_ac.py`) reads on each tick.

## Common gotchas

- **HA's REST API is bearer-token, LAN-only by default.** External
  reachability requires a reverse tunnel, VPN, or Nabu Casa. This
  repo assumes LAN.
- **The WS API is asyncio + JSON envelopes.** Every command gets an
  `id` field; replies come back with matching `id`. Interleave with
  care.
- **`input_datetime` uses HA-local timezone in the string but UTC in
  the `timestamp` attribute.** Templates that compare `now()` with
  `states(...)` need to convert; the safe form is
  `as_timestamp(states(entity))` — returns unix seconds regardless.
- **Automation POST body must include `alias`, `mode`, `triggers`,
  `actions`.** Missing `mode` defaults to `single`, but leaving other
  keys out returns HTTP 400.
- **Restarting HA is *not* required for automation POSTs, service
  calls, or dashboard saves.** It *is* required for `manifest.json`
  changes to custom_components (irrelevant here) and for
  `configuration.yaml` changes.
- **Rate limits.** HA's REST API doesn't rate-limit locally, but if
  your daemon polls faster than 1 Hz you're doing something wrong.
  The idiomatic cadences here are 30s (weather), 5 min (smart_ac,
  retrospective), 15 min (anomaly, fridge_monitor), daily (weekly).

## Comparison: what a proper HA custom_component would look like

If we ever wanted to package smart_ac for the general audience, the
translation would be:

| This repo | Custom component |
|---|---|
| `smart_ac.py` decision loop | `DataUpdateCoordinator` with `update_interval=timedelta(minutes=5)` |
| `sensor.smart_ac_status` state pushes | `SensorEntity` subclass whose `native_value` reads coordinator state |
| `create_telegram_*_command.py` automations | Left as user-configured automations (they're user preferences) |
| pi-sf web UI | HA's device page auto-renders entities under a `DeviceInfo` |
| `smart_ac.json` config | `config_flow.py` gathers via UI, stores in a config entry |
| pi-sf systemd unit | HA's own event loop schedules the coordinator |

Worth doing? For a general release, yes. For a house-specific tool with
one user, no — the current model is faster to iterate on, doesn't
require HA restarts for code changes, and doesn't force everything
into HA's asyncio model.

## Related docs

- [DESIGN.md](DESIGN.md) — architectural rationale + design principles.
- [USAGE.md](USAGE.md) — user-facing feature guide.
- [INSTALL.md](INSTALL.md) — from-scratch install procedure.
- [SMART_AC.md](SMART_AC.md) — deep dive on the scheduler.
- [openapi.yaml](openapi.yaml) — machine-readable API spec for downstream
  consumers like SolarSage.
