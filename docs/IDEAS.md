# Future feature ideas

Sketch pad for features I've floated or thought about but not yet built. Each
one has a brief rationale + rough sketch. Pick anything from here and I'll
scope + implement.

## Observability / reporting

### Weekly retrospective

**Why.** The daily report is fine for "what happened yesterday" but doesn't
surface week-over-week trends: was this Monday's SoC trajectory typical? Are
we averaging more DEFICIT mode this week vs last?

**Sketch.** Add `smart-ac-retrospective-weekly.timer` that fires Monday at
00:45. `retrospective.py --week` walks the last 7 daily reports (or generates
them if missing) and produces a summary:
- Total kWh from battery each day
- Average SoC peak / trough per day
- Total minutes per mode per day (small table)
- Notable events (mode changes, unplanned discharges)

### SoC trend + mode timeline chart

**Why.** Text tables are informative but a chart tells you at a glance whether
today is trending like normal or something's off.

**Sketch.** Add `/chart` to web.py that renders an SVG (stdlib xml.etree) with:
- X axis: time of day
- Y axis: SoC%
- Line: SoC over time from decisions.log
- Colored bands underneath: which mode was active in each 5-min slot

Zero JS. All server-side SVG.

### "How much did I save today" running dollar figure  ✅ PARTIAL

Nightly retrospective now includes a **per-AC estimated energy + cost** table
(runtime × per-AC watts × `$0.30/kWh`, watts source labelled
`measured`/`default`). See [SMART_AC.md § Retrospective](SMART_AC.md#retrospective).

Still missing: live "$ saved today" figure that would compare actual vs a
counterfactual naive schedule. The current numbers are just cost-of-cooling,
not savings-vs-alternative.

### Predicted next-day capacity

**Why.** Answering "should I run all six ACs tomorrow?" before tomorrow starts.

**Sketch.** Read past 7 days of `pv_total_power`, compute the hourly max curve.
On evenings, publish a `sensor.smart_ac_forecast` with expected tomorrow-SoC
trajectory. Simple linear model; upgrade to Solcast integration later.

### Anomaly detection

**Why.** "Panel is unusually low today at 2pm despite clear sky" is a real
failure mode we currently only catch via the manual battery-alert rules.

**Sketch.** Track a rolling 14-day median of PV at each hour. If today's PV at
this hour is <50% of that median AND outdoor temp is normal, push a Telegram
alert with a "check the panels" suggestion.

### "How much power does the master AC actually use" over time

**Why.** `calibrate.py` gives one number; `retrospective.py` gives a per-day
estimate; but the AC's efficiency drifts (dirty filters, refrigerant, dust).
Track it.

**Sketch.** Store per-AC estimated_w daily. Chart over weeks/months.

---

## Control

### Manual override with specific end time  ✅ IMPLEMENTED

Full grammar:
`/override <room> [on|off] until <HH:MM|+Nh|+Nm>`
`/override <room> [on|off] for <Nh|Nm>`
`/override <room> clear`
`/override list`

Three interchangeable UIs (all read/write the same
`input_datetime.ac_<room>_override_until` helpers): Telegram, HA dashboard
datetime pickers, and `pi-sf:5010/overrides`. See
[SMART_AC.md § Overrides](SMART_AC.md#overrides).

### Scheduled overrides (recurring)

**Why.** "Always run the bar AC 20:00-22:00 on Friday nights."

**Sketch.** New config file section:

```json
"scheduled_overrides": [
  {"room": "bar_area", "days": ["fri"], "start": "20:00", "end": "22:00", "state": "on"}
]
```

Scheduler on each tick checks if any scheduled override applies to now and
takes precedence over normal decisions.

### "Nap" preset

**Why.** Push a single button to run master AC hard for an hour then resume
normal.

**Sketch.** `/nap` in Telegram → sets a 60-min override on master → reply
"master AC pinned ON for 60 min." Dashboard button too.

### Party mode

**Why.** Different priorities when hosting.

**Sketch.** New `input_boolean.party_mode`. When ON:
- night_min = [master, living, dining] (guests using common areas)
- comfort_target lower (say 75°F)
- max cap ignored
Toggle-once, revert-when-off.

### Emergency SoC threshold

**Why.** Configurable "below X% SoC, force all non-essential ACs off regardless
of mode."

**Sketch.** New `emergency_soc_threshold` in config (default 30). When SoC
drops below it, target = `night_min` only, regardless of DAY mode.

### Physical override button

**Why.** A Zigbee button somewhere in the house that toggles "all ACs off" for
easy manual control without pulling out a phone.

**Sketch.** Add a Zigbee button (like the existing TS004F). Automation on
`zha_event` from that button → HA service to turn off `smart_ac_enabled` +
all `input_boolean.ac_*` off. Second press to re-enable.

---

## Intelligence / adaptation

### Adaptive comfort target

**Why.** Fixed 78°F is a guess. What if the family "actually" wants 76°F?

**Sketch.** Track how often HUMAN actions turn ACs on when the room is
already at target. If humans are frequently overriding the scheduler because
they think 78°F is warm, gradually lower the target. Ceiling of 74°F,
gradient of 0.5°F per week of consistent overrides.

### Weather forecast integration

**Why.** "Tomorrow's forecast is cloudy" should preemptively pull back today's
extras so we don't end the day at 60% SoC.

**Sketch.** Install Solcast HACS integration (or Open-Meteo). Read
`sensor.solcast_pv_forecast_tomorrow` in `soc_target_at_dark` calc: if
tomorrow's forecast is <20 kWh, treat today's target as 105% instead of 100%
(to give a buffer).

### Auto-detect bedtime_hour

**Why.** Bedtime varies. Manually configured hour drifts as the family's
schedule changes.

**Sketch.** Track when `switch.master_side_light` (or bedside lamps) get
turned off consistently. Take the median over the last 14 days. Suggest
updating `bedtime_hour` if the observation is >30 min away from the config.

### Presence-aware per-room comfort

**Why.** If nobody's in the office at 3pm, don't waste an AC on it.

**Sketch.** Add a `presence_source_for_room` config: which HA sensor indicates
presence per room. If sensor exists and reports "not_home" for the room, skip
adding it as an extra even if temp says needed.

### Learn AC minimum on-time

**Why.** ACs have a compressor cycle. 15 min hardcoded is a guess.

**Sketch.** Track how long each AC actually runs when the scheduler leaves it
on. If master runs 45 min average before scheduler-initiated turn-off, adjust
its `min_on_minutes` per-AC.

---

## Integration

### `sensor.smart_ac_target_count`

**Why.** So HA automations elsewhere (energy dashboard) can react.
E.g., "if smart_ac.target_count > 3 AND SoC < 60%, send a low-battery warning
sooner than the existing 15-min rule."

**Sketch.** Trivial — scheduler already publishes `target_on` as an attribute;
just add `target_count = len(target_on)` to `sensor.smart_ac_status`.

### Grafana / Prometheus metrics

**Why.** External dashboards + long-term time-series analysis.

**Sketch.** Add `/metrics` to `web.py` returning Prometheus text format for
every attribute in the three sensors. `node_exporter` on pi-sf, Grafana on
another machine.

### Voice control via HA Voice / Assist

**Why.** "Alexa, override master AC for 3 hours" instead of typing in Telegram.

**Sketch.** Add an HA Assist intent script that parses "override <room> for
<duration>" and calls `input_datetime.set_datetime` +
`input_boolean.turn_on/off` (same primitives as the Telegram `/override`
automation). Requires HA's Voice Assistant to be configured — separate project.

### Export decisions.log to a spreadsheet

**Why.** Some people want raw data in Numbers/Excel.

**Sketch.** Add `/decisions.csv` to `web.py` that streams a CSV version of the
last N ticks.

### Multi-house shared control

**Why.** You have 3 houses. Maybe smart_ac principles apply to all of them.

**Sketch.** Package the whole `smart_ac/` directory as a reusable component.
Config per-house. Central dashboard federating status across houses.

Very much a "phase 2" idea.

---

## Comfort automations that use smart_ac data

### Sleep-window ramp-down

**Why.** Cool the bedroom hard right before bed, then let it warm slightly
overnight to save battery.

**Sketch.** Automation: at `bedtime_hour - 30 min`, force master AC ON. At
`bedtime_hour + 60 min`, allow scheduler to let it drop to `night_min` behavior.

### Wake-up preheat

**Why.** Have the master bedroom feel comfortable when the alarm goes off
instead of hot.

**Sketch.** Automation: at `sunrise - 20 min` (or a fixed morning time), turn
on master AC. Scheduler resumes at `sunrise + morning_offset`.

### Family-in-kitchen detection

**Why.** If everyone's cooking dinner, kitchen needs cooling but scheduler
doesn't know.

**Sketch.** Add a motion sensor to the kitchen. When motion for >5 min AND
outdoor > 80°F, trigger an auto-override on dining AC for 90 min via the same
override endpoint.

---

## Housekeeping

### Rotate decisions.log more actively

**Why.** Currently 10MB × 5 backups = 50MB retained. Might want to compress
older ones or push to a longer-term store.

**Sketch.** Post-rotate hook that gzips + moves to `~/smart_ac/archive/`.

### Automatic config validation

**Why.** A typo in `smart_ac.json` currently crashes the service on restart.

**Sketch.** Add a JSON schema. `smart_ac.py --check-config` validates. Service
refuses to start with a validation error visible in the journal instead of a
Python traceback.

### Health check endpoint that HA can monitor

**Why.** So the main HA dashboard shows "pi-sf smart_ac is healthy" or "down."

**Sketch.** Add `/healthz` to web.py (already there for /healthz) plus a HA
`rest` sensor that polls it every 5 min. Add a dashboard tile.

---

## Nice-to-have UI polish

### Dark/light toggle

Existing UI is dark. Add a switch.

### Mobile-optimized dashboard

Existing UI works on mobile but the tables can be cramped.

### Time-range selector on `/decisions`

Show more than the last 30 ticks. Date picker.

### Report diff view

Side-by-side comparison of two days' reports.
