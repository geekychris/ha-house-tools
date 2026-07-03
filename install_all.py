#!/usr/bin/env python3
"""
install_all.py — single-command rebuild of every script-driven piece.

Run order matches dependency order: helpers / statistics sensors first,
then automations that depend on them, then dashboard last (so it can
reference everything that exists).

See docs/INSTALL.md for the full procedure including the manual steps
this script CAN'T do (Alexa routine creation, configuration.yaml YAML
pastes, HACS integration installs, etc.).

This script is idempotent: every constituent script is itself idempotent
(stable IDs, "skip if exists" checks). Re-running install_all.py is the
intended workflow after `git pull` or after any script edit.

Required env vars:
    HA_TOKEN          long-lived access token (also read from token.txt)
    TELEGRAM_CHAT_ID  positive int for direct chat with the bot
    TELEGRAM_BOT_TOKEN  Telegram bot API token (also read from bot_token.txt)

Optional env vars:
    HA_URL            defaults to http://homeassistant.local:8123 for REST,
                      ws://homeassistant.local:8123/api/websocket for WS
    SKIP_PI_SF        if set to "1", skip the pi-sf deployments (use when
                      pi-sf is offline)
    SKIP_TUYA_MAGIC   if set to "1", skip the per-device ZHA Toolkit calls
"""

import os
import pathlib
import subprocess
import sys


HERE = pathlib.Path(__file__).resolve().parent

# Default URL pair if not overridden via env
DEFAULT_HA_REST = "http://homeassistant.local:8123"
DEFAULT_HA_WS = "ws://homeassistant.local:8123/api/websocket"

# IEEEs for the MOES TS0012 devices that need the tuya_magic unlock.
# Update this list if you add/remove ACs of that model.
TS0012_IEEES = [
    "a4:c1:38:a3:92:17:04:ec",  # Sconce Lights (living_room)
    "a4:c1:38:7f:01:eb:62:04",  # Kyle Light/Fan
    "a4:c1:38:23:85:cd:ac:80",  # Master Light/Fan
]


def have_env(name: str) -> bool:
    return bool(os.environ.get(name))


def ensure_token(env_var: str, fallback_filename: str, label: str) -> str:
    if have_env(env_var):
        return os.environ[env_var]
    fallback = HERE / fallback_filename
    if fallback.is_file():
        os.environ[env_var] = fallback.read_text().strip()
        return os.environ[env_var]
    sys.exit(f"{label} is required (env var {env_var} or sibling file {fallback_filename}).")


def run(label: str, *cmd: str, env_extra: dict | None = None, allow_fail: bool = False) -> bool:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    print()
    print(f"┌── {label}")
    print(f"│   $ {' '.join(cmd)}")
    print("│")
    try:
        result = subprocess.run(cmd, env=env, cwd=str(HERE))
    except FileNotFoundError as e:
        print(f"│   FAILED to spawn: {e}")
        return False
    ok = result.returncode == 0
    print(f"└── {'OK' if ok else f'FAILED (rc={result.returncode})'}")
    if not ok and not allow_fail:
        sys.exit(f"Step '{label}' failed; aborting. Fix the issue and re-run install_all.py.")
    return ok


def py(script: str) -> tuple[str, ...]:
    return (sys.executable, str(HERE / script))


def main() -> None:
    print("install_all.py — rebuilding every script-driven piece, in order.")
    print()

    ensure_token("HA_TOKEN", "token.txt", "HA long-lived access token")
    ensure_token("TELEGRAM_BOT_TOKEN", "bot_token.txt", "Telegram bot API token")
    if not have_env("TELEGRAM_CHAT_ID"):
        sys.exit("TELEGRAM_CHAT_ID env var is required (positive integer; see docs/INSTALL.md § 3).")
    os.environ.setdefault("HA_URL", DEFAULT_HA_REST)
    rest_url = os.environ["HA_URL"]
    ws_url = rest_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    print(f"HA REST: {rest_url}")
    print(f"HA WS:   {ws_url}")

    skip_pi_sf = os.environ.get("SKIP_PI_SF") == "1"
    skip_tuya_magic = os.environ.get("SKIP_TUYA_MAGIC") == "1"

    # ── Phase 1: Telegram bot integration -------------------------------------
    # Has to be first because nearly everything else replies via the notify
    # entity it creates.
    run(
        "Phase 1: Telegram bot integration (config-flow REST)",
        *py("setup_telegram_bot.py"),
    )

    # ── Phase 2: HA-side input_booleans, helpers, statistics sensors ---------
    run(
        "Phase 2.1: AC input_boolean helpers (per-AC toggles)",
        *py("setup_ac_input_booleans.py"),
        env_extra={"HA_URL": ws_url},
    )
    run(
        "Phase 2.2: Smart AC input_boolean helpers (scheduler enable / notify / unoccupied)",
        *py("setup_smart_ac_input_boolean.py"),
        env_extra={"HA_URL": ws_url},
    )
    run(
        "Phase 2.3: Water statistics helpers (max-depth windows)",
        *py("setup_water_statistics_sensors.py"),
    )
    run(
        "Phase 2.4: Energy dashboard prefs (built-in Energy page)",
        *py("setup_energy_prefs.py"),
        env_extra={"HA_URL": ws_url},
    )
    run(
        "Phase 2.5: AC override input_datetime helpers (per-room end-time pickers)",
        *py("setup_ac_override_input_datetimes.py"),
        env_extra={"HA_URL": ws_url},
    )
    run(
        "Phase 2.6: Smart AC mode toggles (party / nap / vacation)",
        *py("setup_smart_ac_modes.py"),
        env_extra={"HA_URL": ws_url},
    )
    run(
        "Phase 2.7: Smart AC charge-boost helpers",
        *py("setup_smart_ac_charge_boost.py"),
        env_extra={"HA_URL": ws_url},
    )

    # ── Phase 3: Automations ---------------------------------------------------
    automation_scripts = [
        "create_side_table_automation.py",
        "create_telegram_help_command.py",
        "create_telegram_status_command.py",
        "create_telegram_on_command.py",
        "create_telegram_off_command.py",
        "create_telegram_water_command.py",
        "create_telegram_say_command.py",
        "create_telegram_announce_command.py",
        "create_telegram_sayhere_command.py",
        "create_telegram_ac_command.py",
        "create_telegram_smart_ac_command.py",
        "create_telegram_smart_ac_report_command.py",
        "create_telegram_smart_ac_weekly_command.py",
        "create_telegram_override_command.py",
        "create_telegram_charge_boost_command.py",
        "create_telegram_pump_command.py",
        "create_telegram_battery_alert.py",
        "create_ac_toggle_automations.py",
        "create_smart_ac_nap_mode_automation.py",
        "create_sleep_window_automations.py",
        # Solar-surplus + Z-Wave notification logger are optional; they land
        # here so a fresh install has the code but only fires when the
        # relevant HACS integrations / hardware are present.
        "create_solar_surplus_automation.py",
        "create_zwave_notification_logger_automation.py",
    ]
    for s in automation_scripts:
        run(f"Phase 3: {s}", *py(s))

    # ── Phase 3.5: Weather (Open-Meteo) service on pi-sf ---------------------
    if not skip_pi_sf:
        run(
            "Phase 3.5: Deploy Open-Meteo weather fetcher to pi (scp only)",
            *py("setup_weather.py"),
        )

    # ── Phase 4: BotFather command autocomplete --------------------------------
    run(
        "Phase 4: Register Telegram slash-command autocomplete (setMyCommands)",
        *py("set_telegram_bot_commands.py"),
    )

    # ── Phase 5: Custom Lovelace dashboard (overwrites in place) --------------
    run(
        "Phase 5: Energy dashboard (Lovelace)",
        *py("create_energy_dashboard.py"),
        env_extra={"HA_URL": ws_url},
    )

    # ── Phase 6: pi-sf services (scp only; sudo step is manual) ---------------
    if skip_pi_sf:
        print("\n[SKIP] Phase 6 skipped (SKIP_PI_SF=1)")
    else:
        run(
            "Phase 6.1: Deploy tts_speaker to pi-sf (scp only)",
            *py("setup_pi_sf_tts.py"),
        )
        run(
            "Phase 6.2: Deploy smart_ac to pi-sf (scp only)",
            *py("setup_smart_ac.py"),
        )

    # ── Phase 7: ZHA Toolkit per-device fixes ---------------------------------
    if skip_tuya_magic:
        print("\n[SKIP] Phase 7 skipped (SKIP_TUYA_MAGIC=1)")
    else:
        for ieee in TS0012_IEEES:
            run(
                f"Phase 7: tuya_magic on {ieee}",
                *py("apply_tuya_magic_fix.py"),
                ieee,
                allow_fail=True,  # don't abort all if one device is offline
            )

    # ── Done -------------------------------------------------------------------
    print()
    print("═" * 70)
    print("install_all.py finished the scripted phases. Manual steps still needed:")
    print()
    print("  A. SSH to pi-sf and install the systemd units (Phase 6.1 + 6.2):")
    print("       sudo install -m 644 /home/chris/tts-speaker.service /etc/systemd/system/ \\")
    print("       && sudo systemctl daemon-reload \\")
    print("       && sudo systemctl enable --now tts-speaker")
    print()
    print("       sudo install -m 644 /home/chris/smart_ac/smart-ac.service /etc/systemd/system/ \\")
    print("       && sudo systemctl daemon-reload \\")
    print("       && sudo systemctl enable --now smart-ac")
    print()
    print("  B. Confirm the rest_command YAML block is in /config/configuration.yaml")
    print("     (see docs/INSTALL.md § 4) and reload via Developer Tools -> YAML -> 'REST commands'.")
    print()
    print("  C. Confirm the 12 Alexa routines exist (ac on master, ac off master, ...)")
    print("     in the Alexa mobile app. (docs/INSTALL.md § 6)")
    print()
    print("  D. (First install only) restart HA fully after the YAML paste.")
    print()
    print("Verify per docs/INSTALL.md § 11.")
    print("═" * 70)


if __name__ == "__main__":
    main()
