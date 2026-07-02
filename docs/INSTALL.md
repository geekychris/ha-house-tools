# Install (from scratch or after a wipe)

This document walks through a complete reinstall. For the in-place use guide, see [USAGE.md](USAGE.md); for the architecture, [DESIGN.md](DESIGN.md).

There is also a driver script — [`install_all.py`](../install_all.py) — that runs every scriptable step in order. The manual steps it can't do are flagged below with **[MANUAL]**.

---

## 0. Prerequisites

### Hardware

- **HA host:** A Raspberry Pi (4 or 5) with HA OS installed. SD card or NVMe.
- **pi-sf:** A second Raspberry Pi (5 is current; 4 also works) with Debian 12+ installed, connected to a monitor with HDMI audio, on the same WiFi as HA host. SSH enabled.
- **Zigbee coordinator:** SONOFF Dongle-LMG21 (or compatible) plugged into HA host's USB.
- **Sensors & switches:** YoLink water depth sensor, Zbeacon TH01 temperature sensors, MOES TS0012 dual-switches (already paired in your Zigbee network), Tuya TS004F side-table remote.

### Accounts

- Amazon (Alexa) account with the SmartLife → Alexa skill linked (so Alexa can control the B-Air ACs).
- SmartLife account containing the ACs as smart-home devices.
- Telegram account.
- GitHub account (optional, only if you want to push this repo somewhere remote).

### Software prerequisites on your laptop

- Python 3.11+
- `git`, `ssh`, `scp`, `curl`, `pip install websockets` (only needed for the few WebSocket scripts).

---

## 1. Bring up Home Assistant

1. Flash HA OS onto SD/NVMe per Home Assistant's instructions: https://www.home-assistant.io/installation/raspberrypi
2. Boot. Initial setup wizard. Connect to `<your-wifi-ssid>` WiFi.
3. Create a user account.
4. Settings → Integrations: add **Z-Wave JS** if you have Z-Wave devices and **Zigbee Home Automation (ZHA)** if you have Zigbee.
5. Pair Zigbee devices: HA → Settings → ZHA → Add Device. Pair each MOES TS0012, the TH01 sensors, the TS004F remote, and the YoLink water sensor.
6. Settings → Profile → Long-Lived Access Tokens → Create token. Save it to `~/code/claude_world/homeassistant/token.txt` on your laptop (one line, no whitespace).
7. Settings → Areas: create `living_room`, `master_bedroom`, `kyle_room`, `front_patio`, `office`, `bar_area`. Assign devices to these.

### HA add-ons (manual, install via UI)

- **VLC** (community add-on by `frenck`/`community-hass-io-addons`)
- **Advanced SSH & Web Terminal** (community add-on by `frenck`) — paste your SSH public key into the addon's `authorized_keys` config (Configuration tab → save → restart)
- **File Editor** (official)

### HACS install

If not already installed: follow https://hacs.xyz/docs/setup/download for the curl-bash installer.

Install these HACS integrations:
- **Alexa Media Player** (frenck/alexa_media_player) — link your Amazon account when prompted
- **ZHA Toolkit** (mdeweerd/zha-toolkit)

---

## 2. Bring up pi-sf

1. Flash Debian 12 (or later) on an SD/NVMe per https://www.raspberrypi.com/software/operating-systems/
2. Boot. Through the initial setup: enable SSH, set username `chris` and a strong password, connect to `<your-wifi-ssid>`.
3. From your laptop: `ssh-copy-id $PI_HOST` so no password is needed for subsequent SSH/scp.
4. Sanity check audio: `aplay -l` should show `vc4hdmi0` and/or `vc4hdmi1`. The monitor with speakers should be on `vc4hdmi1` (or whichever the audio routing knob in [`pi_sf/tts_speaker.py`](../pi_sf/tts_speaker.py) is set to; edit if your wiring differs).

---

## 3. Telegram bot

**[MANUAL]** Do these once-only steps in Telegram:

1. Find **@BotFather** in Telegram. Send `/newbot`. Pick a name and a username ending in "bot" (e.g. `housebot`). BotFather replies with an HTTP API token. Save it to `~/code/claude_world/homeassistant/bot_token.txt` (one line).
2. Find your new bot in Telegram, send it `/start` (or any message).
3. From your laptop, look up your chat ID:
   ```bash
   BOT_TOKEN=$(cat ~/code/claude_world/homeassistant/bot_token.txt)
   curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" | python3 -m json.tool
   ```
   Find your `chat.id` in the JSON — a positive integer for direct chat. Save it as TELEGRAM_CHAT_ID (you'll pass it as env var to scripts below).

---

## 4. Manual configuration.yaml addition

The HA `rest_command:` integration is YAML-only (no UI config flow). Add this block to `/config/configuration.yaml` via the File Editor add-on, then **Developer Tools → YAML → Reload "REST commands"**:

```yaml
rest_command:
  pi_sf_say:
    url: "http://pi.example.local:5006/say"
    method: POST
    content_type: "application/json"
    payload: '{"text": "{{ message }}"}'
    timeout: 30
```

---

## 5. Create the custom Lovelace dashboard

**[MANUAL]** HA UI: Settings → Dashboards → Add Dashboard → Title: anything; URL: `dashboard-energy` (the slug the scripts write to). Mode: Sections.

This dashboard's *content* will be overwritten by [`create_energy_dashboard.py`](../create_energy_dashboard.py) — you just need the slug to exist.

---

## 6. Tuya / SmartLife Alexa routines

**[MANUAL]** In the Alexa app on your phone:

For each of these 12 routines (6 ACs × on/off), tap **More → Routines → +**:

| Routine name (exact) | Smart-home action |
|---|---|
| `ac on master` | Master Bedroom AC → Power On |
| `ac off master` | Master Bedroom AC → Power Off |
| `ac on guest` | Guest Room AC → Power On |
| `ac off guest` | Guest Room AC → Power Off |
| `ac on dining` | Dining Room AC → Power On |
| `ac off dining` | Dining Room AC → Power Off |
| `ac on living` | Living Room AC → Power On |
| `ac off living` | Living Room AC → Power Off |
| `ac on office` | Office AC → Power On |
| `ac off office` | Office AC → Power Off |
| `ac on kyle` | Kyle Room AC → Power On |
| `ac off kyle` | Kyle Room AC → Power Off |

Trigger: **Voice** → type the same routine name as the trigger phrase. Save each. Name must match EXACTLY (lowercase, single spaces) for `media_player.play_media` to find it.

Verify: from a terminal:
```bash
TOKEN=$(cat ~/code/claude_world/homeassistant/token.txt)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://homeassistant.local:8123/api/services/media_player/play_media \
  -d '{"entity_id":"media_player.everywhere","media_content_type":"routine","media_content_id":"ac on master"}'
```
Should make the master bedroom AC turn on within a couple of seconds.

---

## 7. Run install_all.py

```bash
cd ~/code/claude_world/homeassistant
TELEGRAM_CHAT_ID=<your chat id> python3 install_all.py
```

This runs every script-driven setup step in dependency order. Stops if any step fails. Safe to re-run (every script is idempotent). It does NOT do the manual steps above; those have to happen first.

The script will print a progress line per step + a final "Manual steps remaining" summary if anything was skipped due to a missing prerequisite.

---

## 8. Set up pi-sf services

`install_all.py` will scp the pi-sf source files but cannot run sudo on pi-sf without a password. After it finishes, SSH to pi-sf and run:

```bash
sudo install -m 644 /home/chris/tts-speaker.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable --now tts-speaker

sudo install -m 644 /home/chris/smart_ac/smart-ac.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable --now smart-ac
```

Verify:
```bash
systemctl status tts-speaker smart-ac --no-pager
curl http://pi.example.local:5006/healthz   # should return "ok"
```

---

## 9. Apply the MOES TS0012 ZHA fix (per device)

Run [`apply_tuya_magic_fix.py`](../apply_tuya_magic_fix.py) once for each TS0012 dual switch (only run after the device is paired in ZHA):

```bash
python3 apply_tuya_magic_fix.py a4:c1:38:a3:92:17:04:ec    # Sconce Lights
python3 apply_tuya_magic_fix.py a4:c1:38:7f:01:eb:62:04    # Kyle Light/Fan
python3 apply_tuya_magic_fix.py a4:c1:38:23:85:cd:ac:80    # Master Light/Fan
```

(IEEEs are documented in the script's KNOWN_DEVICES map. Update if you have a different set of devices.)

---

## 10. Restart Home Assistant

After all the YAML changes and integration installs, do a full HA restart (not just Reload YAML) to ensure everything's coherent. Settings → System → top-right power icon → Restart.

---

## 11. Verify

Tick through this checklist. Each item is testable in a few seconds.

| Check | Expected |
|---|---|
| HA UI loads at `homeassistant.local:8123` | ✓ |
| Dashboard `dashboard-energy` shows live readings | ✓ |
| `sensor.water_depth_sensor_distance` has a numeric state | ✓ |
| `sensor.smart_ac_status` exists and has `mode` attribute | ✓ |
| Telegram bot replies to `/status` | ✓ |
| Telegram bot replies to `/sayhere hello` and you hear audio on pi-sf monitor | ✓ |
| Telegram bot replies to `/say hello` and you hear audio on Alexa | ✓ |
| Telegram `/ac on master` makes the Master Bedroom AC come on | ✓ |
| Dashboard "Air Conditioners" card shows scheduler toggles + per-AC toggles + status detail + logbook | ✓ |
| `journalctl -u smart-ac -f` on pi-sf shows a decision line every 5 min | ✓ |

If any of these fail, see [USAGE.md § Troubleshooting](USAGE.md#troubleshooting) for the most common causes.

---

## Re-applying changes after editing scripts

The whole point of the script-driven approach: any change to a `create_*.py` or `setup_*.py` script can be re-applied just by running that one script. No HA restart needed unless the change touches `configuration.yaml`.

To re-apply everything (e.g. after `git pull`-ing changes):

```bash
cd ~/code/claude_world/homeassistant
TELEGRAM_CHAT_ID=<your chat id> python3 install_all.py
```

`install_all.py` is idempotent: it skips steps that are already in the target state, runs the rest.

---

## Where each secret lives

| Secret | Where | Gitignored? |
|---|---|---|
| HA long-lived token | `~/code/claude_world/homeassistant/token.txt` | yes (`.gitignore`) |
| Telegram bot token | `~/code/claude_world/homeassistant/bot_token.txt` | yes (`.gitignore`) |
| HA secrets.yaml | `/config/secrets.yaml` on HA host | n/a (not in this repo) |
| pi-sf TTS env file | `/home/chris/smart_ac/smart_ac.env` on pi-sf (and a similar one for tts-speaker if you add auth) | n/a (not in this repo; written by `setup_smart_ac.py`) |

Tokens are NOT committed to git. The `.gitignore` excludes `token.txt`, `bot_token.txt`, `secrets.yaml`, `.env`, `*.token`.
