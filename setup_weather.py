#!/usr/bin/env python3
"""
Deploy the Open-Meteo weather fetcher to a Raspberry Pi.

Same pattern as setup_smart_ac.py -- scp the code + config + systemd
units into a home directory on the target Pi, write HA_TOKEN + HA_URL
into a sibling `weather.env` so systemd's EnvironmentFile= can read
them without us baking secrets into the unit.

Sources (in this repo):
    weather/openmeteo.py
    weather/weather.example.json   -- copy to weather.json on the pi and edit
    weather/weather.service
    weather/weather.timer

Targets (on pi):
    /home/chris/weather/openmeteo.py
    /home/chris/weather/weather.example.json
    /home/chris/weather/weather.env         (HA_TOKEN=... ; gitignored)
    /etc/systemd/system/weather.{service,timer}  (after the manual sudo step)

Re-runs are safe: scp overwrites, systemd install is idempotent. Uses
sudo -n to restart weather.timer if the credential is cached; falls back
to printing a manual command otherwise.

USAGE:
    HA_URL=http://ha.example.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    PI_HOST=chris@pi.example.local \\
    python3 setup_weather.py
"""

import os
import pathlib
import subprocess
import sys


HERE = pathlib.Path(__file__).resolve().parent
SRC_DIR = HERE / "weather"
REMOTE_DIR = "/home/chris/weather"
PI_HOST = os.environ.get("PI_HOST", "chris@pi.example.local")


def _load_token() -> str:
    if "HA_TOKEN" in os.environ:
        return os.environ["HA_TOKEN"]
    fallback = HERE / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


def run(*args: str) -> None:
    print("$", " ".join(args))
    subprocess.run(args, check=True)


def restart_service() -> None:
    """Restart weather.timer so freshly-scp'd code takes effect."""
    cmd = "sudo -n systemctl restart weather.timer"
    print(f"\n$ ssh {PI_HOST} '{cmd}'")
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_HOST, cmd],
        capture_output=True,
    )
    if proc.returncode == 0:
        print("Restarted: weather.timer")
        return
    print("(sudo -n failed. Run this on the pi:)")
    print("    sudo systemctl restart weather.timer")


def main() -> None:
    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    token = _load_token()

    run("ssh", "-o", "BatchMode=yes", PI_HOST, f"mkdir -p {REMOTE_DIR}")

    source_files = ("openmeteo.py", "weather.example.json",
                    "weather.service", "weather.timer")
    for fname in source_files:
        src = SRC_DIR / fname
        if not src.is_file():
            sys.exit(f"Missing source file: {src}")
        run("scp", "-q", "-o", "BatchMode=yes", str(src),
            f"{PI_HOST}:{REMOTE_DIR}/{fname}")

    env_body = f"HA_TOKEN={token}\nHA_URL={ha_url}\n"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_HOST,
         f"cat > {REMOTE_DIR}/weather.env && chmod 600 {REMOTE_DIR}/weather.env"],
        input=env_body.encode(), check=True,
    )

    restart_service()

    print()
    print("If this is a fresh install, do the one-time systemd install on the pi:")
    print(f"  cp {REMOTE_DIR}/weather.example.json {REMOTE_DIR}/weather.json  # edit lat/lon")
    print(f"  sudo install -m 644 {REMOTE_DIR}/weather.service /etc/systemd/system/ \\")
    print(f"  && sudo install -m 644 {REMOTE_DIR}/weather.timer /etc/systemd/system/ \\")
    print("  && sudo systemctl daemon-reload \\")
    print("  && sudo systemctl enable --now weather.timer \\")
    print("  && systemctl status weather.timer --no-pager")


if __name__ == "__main__":
    main()
