# Design

System architecture for the off-grid San Felipe house. Use this when you need to *understand* what's wired to what; use [USAGE.md](USAGE.md) when you want to *use* a feature; use [INSTALL.md](INSTALL.md) when you want to *reinstall* from scratch.

## Physical context

- Off-grid house in San Felipe (Baja). No utility power.
- **Solar:** EG4 15kW inverter, three 280Ah LFP packs (~43 kWh total at 51.2V nominal).
- **Home Assistant host:** Raspberry Pi 4 running HA OS. No monitor, no audio hardware, no analog output. Reachable as `homeassistant.local` / `192.168.x.200`.
- **Living-room "pi-sf":** Raspberry Pi 5 running Debian Trixie. HDMI monitor with working speakers (HDMI 1 / vc4hdmi1). Reachable as `pi.example.local`. This is where audio + the smart scheduler live.
- **Zigbee:** Sonoff Dongle-LMG21 coordinator on the HA host. All Zigbee devices route through it.
- **Z-Wave:** Z-Wave JS integration on HA. Plug-in outlets, a wireless siren, a couple of door/water sensors.
- **WiFi:** Single SSID `<your-wifi-ssid>` covering everything (HA, pi-sf, all smart devices including the SmartLife air conditioners).

## Service topology

```
                  +-------------------+
                  | iPhone (Telegram, |
                  |   SmartLife app)  |
                  +---------+---------+
                            |
       Telegram cloud       |
       Amazon Alexa cloud   |     ssh/scp
       SmartLife/Tuya cloud |       v
       Google TTS public    |
                            |
        +-------------------+----------------------------------------+
        |                                                            |
        v                                                            v
+-------+-------+                                          +---------+---------+
| HA host (Pi 4)|<-- LAN (192.168.7.x) ------------------->|  pi-sf (Pi 5)     |
| HA OS         |                                          |  Debian Trixie    |
| - core        |                                          |                   |
| - HACS (AMP,  |                                          |  systemd services:|
|   ZHA Toolkit)|                                          |  - tts-speaker    |
| - add-ons:    |                                          |    (port 5006)    |
|   - SSH       |                                          |  - smart-ac       |
|   - VLC       |                                          |    (decisions+    |
|     (unused)  |                                          |     logbook)      |
| - Zigbee      |                                          |                   |
| - Z-Wave JS   |                                          +-------------------+
| - Telegram bot|
| - YoLink      |
| - EG4 monitor |
+-------+-------+
        |
        | Zigbee
        v
+-------+-------+
|  Zigbee mesh  |
| MOES TS0012   |
| TS004F remote |
| Zbeacon TH01  |
| YoLink ws     |
+---------------+
```

## Software components (everything in this repo deploys one of these)

### 1. Energy monitoring (EG4 + custom Lovelace)

| Thing | Where | How |
|---|---|---|
| EG4 polling | HA host (`eg4_web_monitor` integration) | Polls EG4 cloud, exposes ~250 sensors |
| Energy dashboard prefs | HA built-in Energy page | [`setup_energy_prefs.py`](../setup_energy_prefs.py) registers lifetime kWh sensors as grid / solar / battery sources |
| Custom Lovelace dashboard | HA Lovelace at `dashboard-energy` | [`create_energy_dashboard.py`](../create_energy_dashboard.py) overwrites the dashboard config wholesale on each run |

**Sensors that matter** for downstream logic (all under `sensor.sna_us_15k_53562j0683_*` unless noted):
- `state_of_charge` — SoC %, used by smart_ac
- `battery_power` — W, **positive when charging**, negative when discharging
- `battery_status` — "Charging" / "Discharging" / "Idle" string
- `pv_total_power` — W produced (but throttles to match load when battery full)
- `living_room_eg4_total_load_power` — **the** house load number (consumption_power is unreliable; eps_power is close but lower; total_load_power matches the EG4 app)
- `yield`, `load_energy` — today's kWh totals
- `*_lifetime` variants — lifetime totals

### 2. Water monitoring

YoLink ultrasonic depth sensor (`sensor.water_depth_sensor_distance`) reports feet of water in the tank. Tank holds approximately 350 gallons per foot of depth (configurable).

- **Stat sensors** (auto-rolling windows): 24h / 3d / 7d max-depth, created by [`setup_water_statistics_sensors.py`](../setup_water_statistics_sensors.py).
- **Telegram `/water` command** reads the max-in-window minus current depth to estimate "gallons used since last refill" per window.

### 3. ZHA-related fixes

| Thing | Why | Script |
|---|---|---|
| MOES TS0012 dual-switch unlock | Per-channel reporting broken on these no-neutral devices; both channels toggle together until tuya_magic is run | [`apply_tuya_magic_fix.py`](../apply_tuya_magic_fix.py) |
| Side-table TS004F button → master light | Wireless remote on the bedside, button 1 toggles `light.master_light_light` | [`create_side_table_automation.py`](../create_side_table_automation.py) |

### 4. Telegram bot

Setup [`setup_telegram_bot.py`](../setup_telegram_bot.py) creates the Telegram Bot integration via HA's config-flow REST + adds the allowed-chat sub-entry. Produces a notify entity like `notify.living_room_homeassistantxyz11_chris_collins`.

Commands (each a separate automation, each idempotent):
| Cmd | Script | What |
|---|---|---|
| `/status` | [`create_telegram_status_command.py`](../create_telegram_status_command.py) | Snapshot: power/temps/water/on-list |
| `/on <name>`, `/off <name>` | [`create_telegram_on_command.py`](../create_telegram_on_command.py), [`create_telegram_off_command.py`](../create_telegram_off_command.py) | Fuzzy on/off any light/switch |
| `/water` | [`create_telegram_water_command.py`](../create_telegram_water_command.py) | Tank depth + usage estimates |
| `/say`, `/announce` | [`create_telegram_say_command.py`](../create_telegram_say_command.py), [`create_telegram_announce_command.py`](../create_telegram_announce_command.py) | Alexa TTS |
| `/sayhere` | [`create_telegram_sayhere_command.py`](../create_telegram_sayhere_command.py) | Pi-sf-local TTS |
| `/ac on \| off <room>` | [`create_telegram_ac_command.py`](../create_telegram_ac_command.py) | Fires Alexa routines |
| `/smart_ac` | [`create_telegram_smart_ac_command.py`](../create_telegram_smart_ac_command.py) | Scheduler status snapshot |

Plus a proactive alert ([`create_telegram_battery_alert.py`](../create_telegram_battery_alert.py)) for the "low solar + high battery draw during daylight" condition.

[`set_telegram_bot_commands.py`](../set_telegram_bot_commands.py) registers the slash-command autocomplete list with BotFather.

### 5. TTS pipeline (pi-sf-local audio)

The HA host has no audio output (no monitor, no USB DAC, no 3.5mm jack on the Pi 4 model used; HDMI never enumerated). Audio is offloaded to pi-sf:

```
Telegram /sayhere <text>
  -> HA automation telegram_sayhere_command
    -> rest_command.pi_sf_say (HTTP POST)
      -> tts-speaker.service on pi-sf (port 5006)
        -> fetch Google Translate TTS mp3
          -> ffplay with SDL_AUDIODRIVER=alsa, AUDIODEV=plughw:CARD=vc4hdmi1
            -> HDMI 1 audio out -> monitor speakers
```

Files: [`pi_sf/tts_speaker.py`](../pi_sf/tts_speaker.py), [`pi_sf/tts-speaker.service`](../pi_sf/tts-speaker.service), deployed by [`setup_pi_sf_tts.py`](../setup_pi_sf_tts.py).

One YAML paste needed in HA's `configuration.yaml` (rest_command is YAML-only):
```yaml
rest_command:
  pi_sf_say:
    url: "http://pi.example.local:5006/say"
    method: POST
    content_type: "application/json"
    payload: '{"text": "{{ message }}"}'
    timeout: 30
```

### 6. AC control bridge (no direct Tuya integration)

The B-Air ACs are in the SmartLife (not Tuya) account. Bridging to HA via Tuya cloud failed (account-can't-be-merged, IoT Cloud QR walls). MitM extraction blocked by SSL pinning. So the chosen path is via **Alexa routines**:

```
input_boolean.ac_<room>
  -> (state change) automation ac_toggle_<room>
    -> media_player.play_media with content_type=routine, content_id="ac on|off <room>"
      -> Alexa cloud -> SmartLife skill -> AC

/ac on|off <room> in Telegram
  -> automation telegram_ac_command
    -> same media_player.play_media call
```

The Alexa routines must exist with names matching exactly: `ac on master`, `ac off master`, ..., `ac on kyle`, `ac off kyle`. Created manually in the Alexa app (no public API for routine creation).

Files: [`setup_ac_input_booleans.py`](../setup_ac_input_booleans.py), [`create_ac_toggle_automations.py`](../create_ac_toggle_automations.py), [`create_telegram_ac_command.py`](../create_telegram_ac_command.py).

### 7. Smart AC scheduler

A Python service on pi-sf that decides which ACs run based on solar/battery/temp/time. The whole `smart_ac/` directory + several setup scripts. See [USAGE.md § Smart AC](USAGE.md#smart-ac-scheduler) for behaviour. Source: [`smart_ac/smart_ac.py`](../smart_ac/smart_ac.py).

Status published to `sensor.smart_ac_status` (POST to HA REST). Decisions logged three ways:
1. `journalctl -u smart-ac` — every tick (high-volume INFO)
2. `/home/chris/smart_ac/decisions.log` — rotating JSON lines, full inputs+output per tick
3. HA Logbook entries — only on mode change or actions (low-volume, human-readable)
4. (Optional) Telegram messages when `input_boolean.smart_ac_notify_telegram` is on — same gating as logbook

## Data flows (worked examples)

### A. Manually turning on the bar light via Telegram

```
1. You type /on bar in Telegram chat with @homeassistantxyz11bot
2. Telegram delivers to HA's running telegram_bot integration
3. HA fires telegram_command event {command:"/on", args:["bar"]}
4. automation.telegram_on_command picks up the event
5. Jinja walks states.light + states.switch, fuzzy-matches "bar" -> switch.bar_light
6. Calls homeassistant.turn_on(switch.bar_light)
7. Z-Wave JS sends the command to the in-wall switch
8. Light turns on; HA state updates; HA logbook gets an entry
9. The automation's reply action calls notify.send_message -> Telegram "On (1): Bar Light"
```

### B. Smart AC adds an extra during SURPLUS

```
1. Every 5 min, smart_ac.py wakes up
2. Snapshot reads /api/states: SoC=100, battery_power=0, pv=4900, load=4900,
   outdoor=89F, indoor.living=80F, indoor.master=79F, all ACs off
3. effective_params() picks night_min=[master,kyle,guest], priority=[living,dining,office]
   (occupied mode -> day_priority as configured, no rotation)
4. Mode decision: SoC >= 100 -> SURPLUS
5. Iterate priority: living (needs cooling) ADD, dining (unsensored, outdoor 89>80) ADD,
   office (unsensored, 89>80) ADD
6. Apply hysteresis: all 6 ACs have been off >10min -> issue 6 calls to input_boolean.turn_on
7. Each input_boolean.ac_<r> state change triggers automation.ac_toggle_<r>
8. That automation fires media_player.play_media routine "ac on <r>"
9. Alexa routine activates SmartLife AC
10. Scheduler writes sensor.smart_ac_status with mode=SURPLUS, target=[all 6], reasons=...
11. journalctl logs one INFO line; decisions.log gets one JSON record; HA logbook gets
    "Mode XYZ -> SURPLUS" and per-action "ON <room> (SURPLUS, occ)" lines
12. If smart_ac_notify_telegram is on, Telegram gets the same set of short messages
```

### C. Battery alert fires

```
1. battery_power drops below -500W (drawing from battery)
2. After 15 minutes sustained, automation.telegram_battery_low_solar_alert evaluates
3. Conditions: pv_total_power < 1000W AND time between 10:00-16:00 -> both pass
4. Action 1: notify.send_message via telegram entity -> Telegram alert
5. Action 2: rest_command.pi_sf_say with a short voice line -> pi-sf TTS service
   -> ffplay -> monitor speakers -> "Alert: solar low and drawing from battery, 850 watts solar, battery at 67 percent"
```

## Key design decisions + their tradeoffs

| Decision | Rationale | Alternative considered | Why not |
|---|---|---|---|
| All HA config changes via scripts (not UI clicks) | Reproducibility, version control | Manual UI workflow | Scripts are the source of truth; can rebuild from scratch in <1h |
| One script per automation (mostly) | Easy to grep, easy to delete, no superscript with conditional includes | Single mega-script | Mega-script becomes unreadable + risky to modify |
| Telegram bot integration via UI config-flow API | HA 2026 removed YAML support | Continue with YAML | YAML telegram_bot still LOADS but `telegram_bot.send_message` fails because it requires a notify entity, which only the UI flow creates |
| TTS offloaded to pi-sf via HTTP | HA host has no audio output | Try harder to enable HDMI audio on HA host | The Pi 4 HDMI sink doesn't enumerate; would require config.txt edits which the SSH addon can't do |
| AC control via Alexa routines | SmartLife account isolated from Tuya account; direct integration blocked | Tuya IoT Cloud, MitM extraction, LocalTuya | IoT Cloud QR walls; SSL pinning blocks MitM |
| Smart AC reads battery_power, not pv_power, for surplus | EG4 throttles PV to match load when battery is at 100% | Use Solcast forecast | Solcast adds a dependency; battery dynamics are a real-time truth source |
| Smart AC uses SoC%, not voltage, for kWh math | LFP voltage curve is too flat to trust | Voltage-based kWh | LFP is flat from 20-95% SoC |
| Status published as `sensor.smart_ac_status` | One place to read for dashboard + Telegram + future automations | Multiple smaller sensors | One JSON-attr blob is easier to maintain |
| Daily-rotated priority in unoccupied mode | Spreads wear across the priority ACs over multi-day vacation | More complex hour-by-hour rotation | Day-of-year is deterministic and survives restarts; "first added" round-robin is enough |
| `last_action_at` baseline on cold start | Without it, scheduler thinks every input_boolean's last_changed is a "manual user toggle" and locks itself out | Distinguish via `context.user_id` | Both scheduler and user toggles come via REST with the same long-lived token; can't distinguish |

## Known limitations

- **No two-way AC state sync.** HA's `input_boolean.ac_*` records the last commanded state, not actual AC state. If you toggle from SmartLife app or "Alexa, turn on master AC" voice, HA's toggle drifts.
- **No fine-grained AC control.** The SmartLife → Alexa skill only exposes power on/off, not setpoint or fan speed.
- **Cloud-dependent path for AC.** HA → Alexa cloud → SmartLife cloud → AC. Any link broken = no AC control. Long-term answer is LocalTuya once local keys can be extracted, or a Broadlink IR blaster.
- **TTS depends on Google Translate.** Free public endpoint; can be rate-limited or change format. Future upgrade: install Piper on pi-sf, swap the URL fetch in `tts_speaker.py` for a Piper subprocess.
- **YAML pastes are still required for two integrations.** `rest_command:` block in `configuration.yaml`. Documented in install procedure.
- **Manual Alexa routine setup.** 12 routines for AC control. No API to automate this.

## Conventions for adding new pieces

See [CLAUDE.md](../CLAUDE.md) for the in-repo conventions (one script per automation, idempotent stable IDs, link-everything in README log entries, etc.).
