#!/usr/bin/env python3
"""
Deploy the Smart AC scheduler to $PI_HOST.

Same pattern as setup_pi_sf_tts.py: scp the script + config + systemd
unit into chris's home dir on pi-sf, then print the one sudo command
needed to install the systemd unit. The HA token goes into a sibling
env file (`smart_ac.env`) so the systemd unit can EnvironmentFile= it
without us baking it into the unit.

Sources (in this repo):
    smart_ac/smart_ac.py        the engine
    smart_ac/smart_ac.json      runtime config (tunable, no code edit needed)
    smart_ac/smart-ac.service   systemd unit

Targets (on pi-sf):
    /home/chris/smart_ac/smart_ac.py
    /home/chris/smart_ac/smart_ac.json
    /home/chris/smart_ac/smart_ac.env       (HA_TOKEN=... ; gitignored on Mac)
    /home/chris/smart_ac/tts-speaker.service staged
    /etc/systemd/system/smart-ac.service    (after the manual sudo step)

Re-runs are safe: scp overwrites, systemd install is idempotent. Restarts
`smart-ac.service` and `smart-ac-web.service` at the end so the freshly-scp'd
code actually runs -- without that, systemd keeps executing the previous
in-memory copy. Uses `sudo -n`; if that fails (no sudo timestamp cache and
no NOPASSWD entry), prints a manual restart command.

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------

    HA_URL=http://homeassistant.local:8123 \\
    HA_TOKEN=eyJhbG... \\
    python3 setup_smart_ac.py

It will scp 3 files to pi-sf and print the sudo one-liner.
"""

import os
import pathlib
import subprocess
import sys


PI_SF_HOST = os.environ.get("PI_HOST", "user@pi.example.local")
HERE = pathlib.Path(__file__).resolve().parent
SRC_DIR = HERE / "smart_ac"
REMOTE_DIR = "/home/chris/smart_ac"


def _load_token() -> str:
    """Prefer a pi-sf-specific token file if it exists (so pi-sf uses a
    dedicated HA token separate from the one the Mac uses). Falls back to
    HA_TOKEN env var, then token.txt."""
    pi_sf_specific = HERE / "pi_sf_ha_token.txt"
    if pi_sf_specific.is_file():
        return pi_sf_specific.read_text().strip()
    if "HA_TOKEN" in os.environ:
        return os.environ["HA_TOKEN"]
    fallback = HERE / "token.txt"
    if fallback.is_file():
        return fallback.read_text().strip()
    sys.exit("HA_TOKEN env var (or token.txt sibling file) is required.")


SERVICES_TO_RESTART = ("smart-ac", "smart-ac-web")


def run(*args: str) -> None:
    print("$", " ".join(args))
    subprocess.run(args, check=True)


def restart_services() -> None:
    """Restart the smart-ac units on pi-sf so they pick up freshly-scp'd code.
    Attempts sudo -n first (works within the sudo timestamp cache window, or
    if a NOPASSWD sudoers entry is installed for these units). If that fails,
    prints a clear copy-paste command so the user can restart manually.

    Without this, a naive `python3 setup_smart_ac.py` leaves the running
    service on the OLD in-memory code, which has bitten us before (auto-detect
    heuristic kept running for an hour after removal from source)."""
    cmd = "sudo -n systemctl restart " + " ".join(SERVICES_TO_RESTART)
    print(f"\n$ ssh {PI_SF_HOST} '{cmd}'")
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_SF_HOST, cmd],
        capture_output=True,
    )
    if proc.returncode == 0:
        print(f"Restarted: {', '.join(SERVICES_TO_RESTART)}")
        return
    print("(sudo -n failed -- password prompt or no NOPASSWD entry.")
    print(" Run this on pi-sf yourself so services pick up the new code:)")
    print(f"    sudo systemctl restart {' '.join(SERVICES_TO_RESTART)}")
    print(" One-time optional: add NOPASSWD for these two units so re-deploys")
    print(" auto-restart. Run once on pi-sf:")
    lines = [
        f"chris ALL=(ALL) NOPASSWD: /bin/systemctl restart {s}.service"
        for s in SERVICES_TO_RESTART
    ]
    print("    (echo '" + "' ; echo '".join(lines) + "') | sudo tee "
          "/etc/sudoers.d/smart-ac-restart >/dev/null && "
          "sudo chmod 440 /etc/sudoers.d/smart-ac-restart")


def main() -> None:
    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    token = _load_token()

    # Make remote dir
    run("ssh", "-o", "BatchMode=yes", PI_SF_HOST, f"mkdir -p {REMOTE_DIR}")

    # scp all source files. Adding new file? Add it here.
    source_files = (
        "smart_ac.py",
        "smart_ac.json",
        "smart-ac.service",
        "retrospective.py",
        "calibrate.py",
        "smart-ac-retrospective.service",
        "smart-ac-retrospective.timer",
        "weekly.py",
        "smart-ac-weekly.service",
        "smart-ac-weekly.timer",
        "web.py",
        "smart-ac-web.service",
        "stats.py",
        "analyze.py",
    )
    for fname in source_files:
        src = SRC_DIR / fname
        if not src.is_file():
            sys.exit(f"Missing source file: {src}")
        run("scp", "-q", "-o", "BatchMode=yes", str(src),
            f"{PI_SF_HOST}:{REMOTE_DIR}/{fname}")

    # Write the env file directly via ssh -- contains the token, don't scp from disk
    env_body = f"HA_TOKEN={token}\nHA_URL={ha_url}\n"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_SF_HOST,
         f"cat > {REMOTE_DIR}/smart_ac.env && chmod 600 {REMOTE_DIR}/smart_ac.env"],
        input=env_body.encode(), check=True,
    )

    restart_services()

    print()
    print("Files staged. Run this one line on pi-sf (it'll prompt for sudo):")
    print()
    print(f"  sudo install -m 644 {REMOTE_DIR}/smart-ac.service "
          f"/etc/systemd/system/ \\")
    print("  && sudo systemctl daemon-reload \\")
    print("  && sudo systemctl enable --now smart-ac \\")
    print("  && systemctl status smart-ac --no-pager")
    print()
    print("To watch live decisions:")
    print("  journalctl -u smart-ac -f")
    print()
    print("To restart after editing smart_ac.json:")
    print("  sudo systemctl restart smart-ac")
    print()
    print("Also install the retrospective timer (daily analysis):")
    print(f"  sudo install -m 644 {REMOTE_DIR}/smart-ac-retrospective.service "
          f"/etc/systemd/system/ \\")
    print(f"  && sudo install -m 644 {REMOTE_DIR}/smart-ac-retrospective.timer "
          "/etc/systemd/system/ \\")
    print("  && sudo systemctl daemon-reload \\")
    print("  && sudo systemctl enable --now smart-ac-retrospective.timer \\")
    print("  && systemctl status smart-ac-retrospective.timer --no-pager")
    print()
    print("To run retrospective / calibration on demand:")
    print(f"  ssh $PI_HOST \\")
    print(f"    'cd /home/chris/smart_ac && . smart_ac.env && python3 retrospective.py'")
    print(f"  ssh $PI_HOST \\")
    print(f"    'cd /home/chris/smart_ac && . smart_ac.env && python3 calibrate.py'")
    print()
    print("Also install the browsable web UI (dashboard + reports + calibrate):")
    print(f"  sudo install -m 644 {REMOTE_DIR}/smart-ac-web.service "
          "/etc/systemd/system/ \\")
    print("  && sudo systemctl daemon-reload \\")
    print("  && sudo systemctl enable --now smart-ac-web \\")
    print("  && systemctl status smart-ac-web --no-pager")
    print()
    print("Then open  http://pi.example.local:5010  in any browser.")


if __name__ == "__main__":
    main()
