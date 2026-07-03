# ha-house-tools

Idempotent Python scripts that configure a Home Assistant instance
(automations, Telegram bot commands, energy dashboards, water sensors,
input helpers) plus a solar-aware **Smart AC scheduler** daemon and a
tiny **Pi TTS speaker** for offloading text-to-speech to a room with
audio hardware.

Every script owns one HA change end-to-end via the HA REST or WS
APIs. Running them in order against a fresh HA reproduces the live
state — the repo is the source of truth, not the HA UI.

## What's here

- **Root scripts** (`create_telegram_*_command.py`, `setup_*.py`, `apply_tuya_magic_fix.py`, …) — one script per HA change. See [`docs/USAGE.md`](docs/USAGE.md) for what each does and [`docs/INSTALL.md`](docs/INSTALL.md) for the recommended run order.
- **`smart_ac/`** — 5-minute-tick scheduler that decides which of a house's air conditioners run based on battery SoC / solar surplus / indoor + outdoor temps / time of day. Publishes decisions to a `sensor.smart_ac_*` family in HA and exposes a local web UI + REST API for overrides.
- **`pi_tts/`** — a stdlib HTTP server that fetches Google Translate TTS and pipes it to `ffplay` on a Raspberry Pi with speakers. Useful when your HA host has no audio out but a spare Pi in another room does.
- **`docs/`** — architecture ([`DESIGN.md`](docs/DESIGN.md)), programming model + patterns for adding your own scripts ([`PROGRAMMING_MODEL.md`](docs/PROGRAMMING_MODEL.md)), daily usage ([`USAGE.md`](docs/USAGE.md)), reinstall procedure ([`INSTALL.md`](docs/INSTALL.md)), smart_ac reference ([`SMART_AC.md`](docs/SMART_AC.md)), OpenAPI 3 spec for the smart_ac REST subset ([`openapi.yaml`](docs/openapi.yaml)).

## Requirements

- A running Home Assistant instance you can create a Long-Lived Access Token for.
- Python 3.10+.
- `websockets` for the two WS-based scripts (`create_energy_dashboard.py`, `setup_energy_prefs.py`); REST scripts use stdlib only.

## Quick start

```bash
git clone git@github.com:geekychris/ha-house-tools.git
cd ha-house-tools
pip install websockets    # only needed for the WS-based scripts

# Every script reads HA_URL + HA_TOKEN from env (or a sibling token.txt).
export HA_URL=http://ha.example.local:8123
export HA_TOKEN=eyJ...

# Run one script:
python3 setup_telegram_bot.py

# Or run everything in order (creates all input helpers, dashboards,
# and automations that this repo owns):
python3 install_all.py
```

For **smart_ac** deployment (assumes a separate Raspberry Pi):

```bash
export PI_HOST=user@pi.example.local
python3 setup_smart_ac.py                    # scp deploys the daemon
ssh $PI_HOST 'sudo systemctl enable --now smart-ac.service'
```

## Related

- **[github.com/geekychris/solarsage](https://github.com/geekychris/solarsage)** — SolarSage dashboard, which reads the `sensor.smart_ac_*` family and `input_boolean.ac_<room>` entities this repo creates. It exposes a per-AC override UI that sets `input_datetime.ac_<room>_override_until` — same contract as [`docs/SMART_AC.md`](docs/SMART_AC.md).

## Contributing

PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md). If you're
adapting this for your own house, the interesting file is
[`smart_ac/smart_ac.example.json`](smart_ac/smart_ac.example.json)
— copy to `smart_ac.json`, fill in your room / entity IDs, and the
scheduler picks them up.

## License

MIT — see [`LICENSE`](LICENSE).
