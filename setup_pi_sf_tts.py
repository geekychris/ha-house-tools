#!/usr/bin/env python3
"""
Deploy the Pi-speaker TTS HTTP server to $PI_HOST.

----------------------------------------------------------------------------
WHAT THIS DOES
----------------------------------------------------------------------------

The HA host (a Pi 4 running HA OS) has no audio output -- HDMI display
isn't connected, no USB DAC, no 3.5mm. The living-room Pi 5 (`pi-sf`)
has working HDMI audio via the monitor's speakers and is reachable on
the LAN. This script deploys a tiny Python HTTP TTS server to pi-sf
so HA can POST text and have it spoken aloud through the Pi 5's audio.

Architecture:
    Telegram /sayhere -> HA automation -> rest_command.pi_sf_say
        -> HTTP POST http://pi.example.local:5006/say
            -> Python server fetches Google Translate TTS mp3
                -> pipes to ffplay -> Pi 5 HDMI audio -> speakers

The server itself (pi_sf/tts_speaker.py) is ~50 lines using only the
Python stdlib + ffplay (already installed on most Debian/Pi setups).
No system TTS engine install needed; voice comes from Google's free
public translate_tts endpoint. Easy to swap for Piper later (install
Piper, edit the script's URL fetch -> shell-out-to-piper, redeploy).

The server runs as a systemd service (pi_sf/tts-speaker.service) so it
auto-starts on boot and survives reboots.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

Pre-reqs:
- SSH key access to $PI_HOST (no password prompt).
- `ffplay` already on pi-sf (it is by default on Debian with VLC/ffmpeg).

    python3 setup_pi_sf_tts.py

The script copies tts_speaker.py and tts-speaker.service to chris's
home dir on pi-sf, then prints the one sudo command you need to run
yourself on pi-sf to install the systemd unit (sudo can't be done
non-interactively from this Mac without storing the password).

Re-runs are safe: scp overwrites, the systemd command is idempotent.

----------------------------------------------------------------------------
TEST
----------------------------------------------------------------------------

After running this + the sudo step:

    curl http://pi.example.local:5006/healthz       # -> "ok"
    curl -X POST -H "Content-Type: application/json" \\
         -d '{"text":"hello"}' http://pi.example.local:5006/say
"""

import pathlib
import subprocess
import sys


PI_SF_HOST = os.environ.get("PI_HOST", "user@pi.example.local")
HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "pi_sf" / "tts_speaker.py"
UNIT = HERE / "pi_sf" / "tts-speaker.service"


def scp(local: pathlib.Path, remote: str) -> None:
    print(f"scp {local} -> {PI_SF_HOST}:{remote}")
    subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", str(local), f"{PI_SF_HOST}:{remote}"],
        check=True,
    )


def main() -> None:
    for f in (SCRIPT, UNIT):
        if not f.is_file():
            sys.exit(f"Missing source file: {f}")
    scp(SCRIPT, "/home/chris/tts_speaker.py")
    scp(UNIT, "/home/chris/tts-speaker.service")
    print()
    print("Files staged. Now ssh into pi-sf and run (it'll prompt for sudo):")
    print()
    print("  sudo install -m 644 /home/chris/tts-speaker.service /etc/systemd/system/ \\")
    print("  && sudo systemctl daemon-reload \\")
    print("  && sudo systemctl enable --now tts-speaker \\")
    print("  && systemctl status tts-speaker --no-pager")
    print()
    print("Verify with:")
    print("  curl http://pi.example.local:5006/healthz")
    print("  curl -X POST -H 'Content-Type: application/json' \\")
    print("       -d '{\"text\":\"hello\"}' http://pi.example.local:5006/say")


if __name__ == "__main__":
    main()
