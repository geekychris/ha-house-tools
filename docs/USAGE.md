# Usage

How to use the house's smart bits once everything's running. For *what's underneath*, see [DESIGN.md](DESIGN.md). For *first-time setup*, see [INSTALL.md](INSTALL.md).

## At a glance

- **Daily-driver UI:** the Home Assistant dashboard at `dashboard-energy`.
- **Remote control:** Telegram bot `@homeassistantxyz11bot`.
- **Voice (existing):** any Alexa Echo in the house.

## The dashboard (HA → Energy)

Open HA, click the **dashboard-energy** dashboard. Sections, top to bottom:

| Section | What it shows |
|---|---|
| **Right Now** | Current solar / battery / home / grid / EPS readings + battery status |
| **Off-Grid Savings** | $ saved today + lifetime (vs $0.30/kWh utility rate) |
| **Water Tank** | Depth in ft + approximate gallons |
| **Air Conditioners** | Toggles for each of 6 ACs + scheduler toggles + status card + **active overrides + override datetime pickers** + logbook |
| **Battery** | SoC gauge + 24h history + detailed cell/pack info |
| **Solar - PV by String** | 24h chart of total + per-string production |
| **Power Flows** | 24h chart of solar / home / battery / grid in / grid out |
| **Energy Today** | Today's kWh totals |
| **Lifetime** | Lifetime kWh totals |

### Air Conditioners section detail

Three "global" toggles at the top:

- **Smart scheduler** — master kill-switch for the Smart AC scheduler. When OFF, scheduler still evaluates and logs decisions but does NOT change any AC state. Use to fully take manual control.
- **Notify on Telegram** — when ON, scheduler pushes a Telegram message on every mode change and every AC turn_on/turn_off. Use when you want to watch the scheduler live without dashboard / SSH.
- **House unoccupied** — when ON, scheduler runs at most 2 ACs and rotates which ones daily. See [Smart AC scheduler § Modes](#modes).

Below them, six per-AC toggles (Master, Guest, Dining, Living, Office, Kyle). Tap one to turn that AC on/off. **Note:** manual toggles no longer auto-lock — the scheduler will re-evaluate on its next tick (up to 5 min later). If you want the change to *hold*, use the override widgets below or `/override <room> ...` in Telegram.

Below those are the **override widgets**:

- **Active overrides** — markdown card listing every room whose
  `input_datetime.ac_<room>_override_until` is currently in the future. Shows
  `_none_` when nothing's pinned.
- **Set / edit overrides** — six datetime picker rows (one per room). Tap →
  pick a future date + time → save. Any future value pins the room; a past
  value (or 1970-01-01) clears the override. The same helpers are read/written
  by Telegram `/override` and the pi-sf web UI — pick any surface, they all
  agree.

Below that, a **Smart AC status** markdown card showing:
- Current mode (NIGHT / DEFICIT / CHARGE_BEHIND / ON_TRACK / SURPLUS)
- Live inputs: SoC%, battery_power, solar, load, outdoor temp, indoor temps
- Target ON list + Target OFF list
- Per-room reasoning ("master: night_min", "office: skipped (room cool)")
- Actions taken in the last tick
- Last evaluated timestamp

Below that, a **logbook card** showing the last 24h of changes to any AC-related entity.

## Telegram bot

Open `@homeassistantxyz11bot` in Telegram. Type `/` to see autocomplete.

### Status / queries

- **`/status`** — Single-message snapshot: current load/solar/battery/grid, battery SoC + time-to-empty if discharging, temps+humidity per room, today's solar yield + load, water tank depth+gallons, last alert time, and the list of all currently-on lights + switches.
- **`/water`** — Tank depth + estimated gallons used over 24h / 3d / 7d (peak depth in window minus current).
- **`/smart_ac`** — Smart AC scheduler's current decision: mode, inputs, target sets, per-room reasoning, last actions.

### Control — lights and switches

- **`/on <name>`** / **`/off <name>`** — Substring-matches `<name>` against entity friendly names, area names, or entity IDs. Examples:
  - `/off bar` → Bar Light
  - `/off all` (or just `/off`) → everything (lights + non-virtual switches) off
  - `/off master` → everything in master_bedroom
  - `/on sconce` → both sconces
- The reply lists what got matched (first 8 + "+N more" if longer).
- `/on` with no argument is rejected with a usage hint (auto-"all" too dangerous).

### Control — air conditioners

- **`/ac on <room>`** / **`/ac off <room>`** — Fires the matching Alexa routine. Rooms: `master`, `guest`, `dining`, `living`, `office`, `kyle`.
- No `/ac on all` / `/ac off all` because this Alexa account is shared with other houses and "all" would clobber ACs there too. Per-AC only.
- Reply: `Sent: ac on master to Alexa.`

### Overrides (pin an AC on/off for a window)

Scheduler makes decisions every 5 min. If you want to keep an AC in a specific
state past that, set an override. Storage is a single HA `input_datetime`
per room — Telegram, HA dashboard, and pi-sf web UI all read/write the same
thing.

```
/override <room> on until 23:00       -- turn on + pin until 11pm
/override <room> off until 15:30      -- turn off + pin until 3:30pm
/override <room> on for 2h            -- turn on + pin for 2 hours
/override <room> off for 30m          -- turn off + pin for 30 min
/override <room> until 23:00          -- pin current state (no flip)
/override <room> for 2h               -- pin current state for 2 hours
/override <room> clear                -- remove override
/override list                        -- show active overrides
```

Time forms: `HH:MM` (today or rolls to tomorrow if past), `+2h` / `2h`,
`+30m` / `30m`. Rooms: `master`, `guest`, `dining`, `living`, `office`, `kyle`.

Reply confirms with the parsed target time, e.g.
`Override set: living ON + pinned until 2026-06-30 23:00:00.`

For a click-through UI, see the dashboard's **Active overrides** and
**Set / edit overrides** cards. For a full listing in a table view (across
all rooms with future/past status), browse to
`http://pi.example.local:5010/overrides`.

### Voice / announcements

- **`/say <text>`** — Speak `<text>` on the default Alexa (no chime). `/say <room> <text>` to target a specific room (matches `notify.alexa_media_*` suffix). `/say all <text>` broadcasts via `alexa_media_everywhere`.
- **`/announce <text>`** — Same as `/say` but with the Alexa attention chime.
- **`/sayhere <text>`** — Speak via the **pi-sf living room monitor's speakers** (Google TTS over HTTP, played through the Pi 5's HDMI audio).

## Smart AC scheduler

Runs on pi-sf as `smart-ac.service`. Evaluates every 5 minutes.

### Modes

| Mode | Trigger | Target ACs |
|---|---|---|
| **NIGHT** | After `bedtime_hour` (local) OR before `sunrise + sun_offset_morning_min` | Only `night_min` (master, kyle, guest by default) |
| **EVENING** | After `sunset - sun_offset_evening_min` but before `bedtime_hour` | `night_min` + `evening_extra_required` (default adds `living`) |
| **DEFICIT** | Battery is discharging during the day | Only `night_min` (don't add load) |
| **CHARGE_BEHIND** | Charging, but projected won't reach `soc_target_at_dark` by sunset at current rate | Only `night_min` |
| **ON_TRACK** | Charging fast enough to reach target by sunset | `night_min` + ONE extra from `day_priority` |
| **SURPLUS** | SoC ≥ `soc_target_at_dark` (default 100%) | `night_min` + ALL extras whose rooms need cooling |

The three "time-of-day" periods:

| Period | Boundaries | Modes possible |
|---|---|---|
| DAY | between `sunrise + morning_offset` and `sunset - evening_offset` | SURPLUS / DEFICIT / CHARGE_BEHIND / ON_TRACK |
| EVENING | between `sunset - evening_offset` and `bedtime_hour` | EVENING (always — solar gone, family still up) |
| NIGHT | between `bedtime_hour` (today) and `sunrise + morning_offset` (tomorrow) | NIGHT (always — everyone asleep) |

### Per-room "cooling needed" check

- For rooms with a temp sensor (currently `living`, `master`): indoor > `comfort_target_f` (default 78°F).
- For rooms without (`guest`, `dining`, `office`, `kyle`): outdoor > `unsensored_assume_hot_above_outdoor_f` (default 80°F).

### Hysteresis

- Won't turn on an AC unless it's been OFF for ≥ `min_off_minutes` (default 10).
- Won't turn off an AC unless it's been ON for ≥ `min_on_minutes` (default 15).
- **Manual toggles are not auto-pinned.** If you flip an AC from the dashboard,
  `/ac on|off`, or via Alexa, the scheduler will re-evaluate on the next tick
  (up to 5 min later). To *hold* a state past that, set an explicit override
  (`/override ...` or dashboard datetime picker) — see [SMART_AC.md § Overrides](SMART_AC.md#overrides).

### Unoccupied mode

When `input_boolean.house_unoccupied` is ON, the scheduler uses a different parameter set:

| Knob | Default | Behaviour |
|---|---|---|
| `unoccupied_night_min_acs` | `[]` | No ACs required overnight (let the house warm up) |
| `unoccupied_comfort_target_f` | 82°F | Higher target — only cool when really hot |
| `unoccupied_unsensored_assume_hot_above_outdoor_f` | 90°F | Higher outdoor threshold for unsensored rooms |
| `unoccupied_max_acs_total` | 2 | Hard cap on simultaneous ACs |
| (rotation) | day-of-year | `day_priority` rotated daily so each AC takes equal "first" turns |

### Notifications

When `input_boolean.smart_ac_notify_telegram` is ON, the scheduler pushes a Telegram message on:
- Mode transitions: `"Smart AC: Mode ON_TRACK -> SURPLUS (occ), target = master, kyle, guest, living"`
- Each turn_on / turn_off action: `"Smart AC: ON living (SURPLUS, occ)"`

Same gating as HA Logbook entries (don't spam every tick).

### Where to see decisions

| Surface | Detail level | How to access |
|---|---|---|
| Dashboard "Smart AC" card | Live, last-tick reasoning + action list | HA UI |
| HA Logbook | Mode changes + actions, last N days | HA UI → Logbook tab |
| Telegram `/smart_ac` | Same as dashboard, in chat | Type `/smart_ac` |
| Telegram notifications | Live stream of decisions | Toggle `smart_ac_notify_telegram` ON |
| pi-sf `journalctl -u smart-ac -f` | Every tick INFO line | SSH to pi-sf |
| pi-sf `/home/chris/smart_ac/decisions.log` | Every tick as JSON, rotating | `tail -F`, or `jq` over historical lines |

### Tuning

Edit `/home/chris/smart_ac/smart_ac.json` on pi-sf, then `sudo systemctl restart smart-ac` to apply. Knobs you'll most likely touch:

| Knob | Default | When to change |
|---|---|---|
| `sun_offset_evening_min` | 120 | If solar drops earlier/later than 2h before sunset |
| `bedtime_hour` | 22 | When EVENING period ends and full NIGHT mode kicks in (local hour). E.g., 23 for 11pm, 0 for midnight, 1 for 1am (auto-rolls to next day). |
| `evening_min_acs` | `["master"]` | Bedrooms required to run during EVENING (before bedtime). Usually smaller than `night_min_acs` because during evening people are still up in family areas, not in their bedrooms. Falls back to `night_min_acs` if this key is missing. |
| `evening_extra_required` | `["living"]` | Which family-area ACs to add during EVENING on top of `evening_min_acs`. |
| `day_priority` | `[living, dining, office]` | Reorder to change which extras come on first |
| `night_min_acs` | `[master, kyle, guest]` | Change which 3 are required at night |
| `comfort_target_f` | 78 | Lower if rooms feel warm |
| `min_on_minutes` | 15 | Raise if you see thrashing |
| `min_off_minutes` | 10 | Raise if you see thrashing |
| `soc_target_at_dark` | 100 | Drop to 95 for a 5% buffer; raise to 100 for "absolutely full" |
| `evaluation_interval_minutes` | 5 | Lower to 2-3 for faster reaction; raise to 10 for less HA REST traffic |

Unoccupied-mode equivalents (`unoccupied_*` keys) are tuned the same way.

## Common scenarios

### "I'm going away for a few days"

1. Dashboard → Air Conditioners → toggle **House unoccupied** ON.
2. (Optional) toggle **Notify on Telegram** ON so you get a stream of decisions while away.
3. Drive away. Scheduler will run at most 2 ACs at once, rotated daily.
4. On return: toggle **House unoccupied** OFF. Comfort behaviour resumes.

### "Battery looks low and it's getting cloudy"

The `telegram_battery_low_solar_alert` automation fires automatically if drawing >500W from battery AND solar <1000W during 10:00-16:00 sustained 15 min. You get a Telegram message + an Alexa voice line on pi-sf. Smart AC will be in DEFICIT or CHARGE_BEHIND mode and already pulled back to night_min only.

If you want to be more aggressive, manually turn off ACs in the dashboard AND set overrides on them (`/override <room> off for 2h`) so the scheduler doesn't flip them back on the next tick.

### "It's 4pm and battery's only at 75%"

Smart AC will be in CHARGE_BEHIND mode (projected charge < kWh-to-full). Only `night_min` ACs run. If you don't trust the scheduler and want to manually keep more comfort on, toggle **Smart scheduler** OFF and drive each AC manually.

### "We never reach 100% — I want to be conservative"

Edit `smart_ac.json` on pi-sf: `soc_target_at_dark: 95` (or whatever %). Restart service. Scheduler now treats 95% as "full enough" and goes to SURPLUS sooner.

### "Master Bedroom AC is broken — exclude it from scheduler decisions"

Edit `smart_ac.json`:
- Remove `"master"` from `night_min_acs`.
- Don't add it to `day_priority` if it wasn't.

Restart. Scheduler won't touch that input_boolean. You can still control it via dashboard or `/ac on master` manually.

### "I want all ACs on RIGHT NOW for company"

Options, roughly best-first:
- **Overrides (cleanest):** For each room, `/override <room> on for 3h`. Scheduler stays enabled, only the pinned rooms are protected, and it all clears itself at expiry.
- **Kill switch (heavy):** Toggle **Smart scheduler** OFF, then toggle each AC ON from the dashboard (or `/ac on <room>` × 6). Remember to toggle scheduler back ON afterwards or you'll wake up with everything running.

## Troubleshooting

| Symptom | Probable cause | Check / fix |
|---|---|---|
| `/sayhere` no audio | network or volume | Check pi-sf reachable from HA: `ssh root@homeassistant.local 'curl http://pi.example.local:5006/healthz'` should return `ok`. Check monitor volume / HDMI cable. |
| `/ac off master` returns "Sent" but AC doesn't react | Routine missing or wrong name | Open Alexa app → Routines. Confirm "ac off master" exists with exact spelling. Test from Alexa app directly. |
| Telegram bot doesn't reply | Bot integration broken | Settings → Devices & Services → Telegram Bot → check loaded. If unhealthy, [`setup_telegram_bot.py`](../setup_telegram_bot.py). |
| Smart AC keeps making the same wrong decision | Config wrong, or mode logic for this case | Check `journalctl -u smart-ac -n 50` on pi-sf. Look at the reasons in `sensor.smart_ac_status` attributes. Adjust `smart_ac.json` + restart. |
| Smart AC service not running | Crashed | `systemctl status smart-ac` on pi-sf. `journalctl -u smart-ac -n 100` for the error. Likely HA unreachable or schema mismatch. |
| Dashboard not updated | Browser cache | Cmd+Shift+R on the dashboard tab. |
| `tinytuya scan` shows ACs but they don't work in HA | Local keys not extracted | Out of scope for the current Alexa-routines bridge. See LocalTuya path in [DESIGN.md](DESIGN.md). |

## Emergency fallbacks

- **HA goes dark:** Alexa still works directly ("Alexa, turn on master AC"). SmartLife app still works for the ACs.
- **Telegram goes dark:** Use HA UI directly (dashboard or browser at `homeassistant.local:8123`).
- **pi-sf goes dark:** TTS (`/sayhere` and the battery alert voice line) won't work. Smart AC won't make decisions but the input_boolean toggles still work, so manual AC control via dashboard and `/ac` keep working. Energy / water / lights all unaffected.
- **Internet goes dark:** Smart AC keeps working (HA REST is local). Most lights/switches keep working (Zigbee + Z-Wave are local). ACs and Alexa stop working (cloud-dependent). TTS won't work (Google Translate is cloud).
