#!/usr/bin/env python3
"""
Smart AC scheduler -- decides which ACs run based on solar / battery /
indoor temps / time of day. Runs as a systemd service on pi-sf,
evaluates every N minutes, talks to HA via REST.

Design recap (see README 2026-06-30):

  PRIMARY GOAL : battery reaches `soc_target_at_dark` by sunset.
  SECONDARY   : run extras from `day_priority` for comfort.

Modes (in priority order, mutually exclusive):
  NIGHT          : after sunset - evening_offset -> only night_min
  DEFICIT        : battery is discharging during the day -> only night_min
                   ("we only produce as much as we consume" -- if battery is
                    discharging, solar isn't covering load; no room for extras)
  CHARGE_BEHIND  : charging, but projected charge by dark < kwh-to-full
                   -> only night_min
  ON_TRACK       : charging fast enough -> add ONE extra (cooling-needed)
  SURPLUS        : SoC >= soc_target_at_dark -> add ALL cooling-needed extras

Per-AC cooling-needed check:
  - For rooms with a sensor (`indoor_sensor_for_room`): indoor > comfort_target.
  - Unsensored rooms: outdoor > `unsensored_assume_hot_above_outdoor_f`.

Hysteresis:
  - Don't turn an AC ON unless it's been OFF for >= min_off_minutes
  - Don't turn an AC OFF unless it's been ON for >= min_on_minutes
  - If user manually toggled an AC, hold our hand off it for
    manual_override_minutes.

Safety:
  - Master kill-switch via input_boolean (config `enabled_entity`).
    If off, scheduler logs decisions but doesn't apply them.

Status output:
  - After each evaluation we POST sensor.smart_ac_status to HA so the
    /smart_ac Telegram command (and the dashboard tile) can read the
    current mode, target set, and per-AC reasoning.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request


# Persistent decision log: one JSON object per tick, append-only with rotation.
# Override with SMART_AC_DECISIONS_LOG env var. Follow with
# `tail -F $SMART_AC_DECISIONS_LOG` (jsonl-style, jq-friendly).
DECISIONS_LOG = pathlib.Path(os.environ.get(
    "SMART_AC_DECISIONS_LOG",
    str(pathlib.Path.home() / "smart_ac" / "decisions.log"),
))
_decisions_logger = logging.getLogger("smart_ac.decisions")
_decisions_logger.propagate = False
if not _decisions_logger.handlers:
    try:
        DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        _h = logging.handlers.RotatingFileHandler(
            str(DECISIONS_LOG), maxBytes=10 * 1024 * 1024, backupCount=5
        )
        _h.setFormatter(logging.Formatter("%(message)s"))
        _decisions_logger.addHandler(_h)
        _decisions_logger.setLevel(logging.INFO)
    except OSError:
        # Read-only filesystem or non-writable path (e.g. under pytest
        # from CI). Downgrade to a stderr handler so the module still
        # imports cleanly.
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(message)s"))
        _decisions_logger.addHandler(_h)
        _decisions_logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------- helpers


def load_config(path: pathlib.Path) -> dict:
    with path.open() as f:
        return json.load(f)


def ha_get(cfg: dict, path: str) -> dict | list:
    req = urllib.request.Request(
        f"{cfg['ha_url'].rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def ha_call(cfg: dict, domain: str, service: str, body: dict) -> None:
    url = f"{cfg['ha_url'].rstrip('/')}/api/services/{domain}/{service}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def ha_set_state(cfg: dict, entity_id: str, state: str, attrs: dict) -> None:
    url = f"{cfg['ha_url'].rstrip('/')}/api/states/{entity_id}"
    body = {"state": state, "attributes": attrs}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def parse_dt(s: str) -> dt.datetime:
    # HA timestamps include timezone info: "2026-06-30T20:36:53.123456+00:00"
    return dt.datetime.fromisoformat(s)


# ---------------------------------------------------------------------- state


class Snapshot:
    """In-memory state at one evaluation tick."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        states = {s["entity_id"]: s for s in ha_get(cfg, "/api/states")}

        def f(eid: str, default: float = 0.0) -> float:
            try:
                return float(states[eid]["state"])
            except (KeyError, ValueError):
                return default

        self.now = dt.datetime.now(dt.timezone.utc)

        sun = states.get("sun.sun", {}).get("attributes", {})
        self.sunrise = parse_dt(sun["next_rising"]) if "next_rising" in sun else self.now
        self.sunset = parse_dt(sun["next_setting"]) if "next_setting" in sun else self.now
        # If next_rising is tomorrow morning, we're after sunrise today --
        # subtract a day so "sunrise" means today's, not tomorrow's.
        if self.sunrise > self.now:
            self.sunrise -= dt.timedelta(days=1)
        # Same for sunset
        if self.sunset < self.now:
            self.sunset += dt.timedelta(days=1)

        self.soc = f(cfg["soc_sensor"])
        self.battery_power_w = f(cfg["battery_power_sensor"])
        self.pv_power_w = f(cfg["pv_power_sensor"])
        self.load_w = f(cfg["load_sensor"])
        self.outdoor_f = f(cfg["outdoor_sensor"])

        self.indoor_f: dict[str, float] = {}
        for room, eid in cfg["indoor_sensor_for_room"].items():
            self.indoor_f[room] = f(eid)

        enabled_state = states.get(cfg["enabled_entity"], {}).get("state", "on")
        self.enabled = enabled_state == "on"

        notify_state = states.get(cfg.get("notify_entity_toggle", ""), {}).get("state", "off")
        self.notify_telegram = notify_state == "on"

        unocc_state = states.get(cfg.get("unoccupied_entity", ""), {}).get("state", "off")
        self.unoccupied = unocc_state == "on"

        # Optional mode toggles (see setup_smart_ac_modes.py). All default
        # off; the scheduler falls back to normal behaviour if the helpers
        # don't exist.
        self.party_mode = states.get("input_boolean.smart_ac_party_mode", {}).get("state") == "on"
        self.nap_mode = states.get("input_boolean.smart_ac_nap_mode", {}).get("state") == "on"
        self.vacation_mode = states.get("input_boolean.smart_ac_vacation_mode", {}).get("state") == "on"

        # AC current states + when each last changed
        all_rooms = sorted(set(cfg["night_min_acs"]) | set(cfg["day_priority"]))
        self.ac_on: dict[str, bool] = {}
        self.ac_last_changed: dict[str, dt.datetime] = {}
        for r in all_rooms:
            s = states.get(f"input_boolean.ac_{r}", {})
            self.ac_on[r] = s.get("state") == "on"
            try:
                self.ac_last_changed[r] = parse_dt(s["last_changed"])
            except (KeyError, ValueError):
                self.ac_last_changed[r] = self.now - dt.timedelta(days=1)

        # User-set override_until (from input_datetime.ac_<room>_override_until).
        # These take precedence over the auto-detected manual_override_until.
        # A value in the future = user has pinned this room's state.
        # A missing/past value = no override in effect.
        self.explicit_override_until: dict[str, dt.datetime | None] = {}
        for r in all_rooms:
            eid = f"input_datetime.ac_{r}_override_until"
            state = states.get(eid, {}).get("state")
            if state and state not in ("unknown", "unavailable"):
                try:
                    # HA input_datetime state is "YYYY-MM-DD HH:MM:SS" in
                    # HA's local timezone; astimezone(None) makes it aware.
                    naive = dt.datetime.strptime(state, "%Y-%m-%d %H:%M:%S")
                    self.explicit_override_until[r] = naive.astimezone()
                except Exception:
                    self.explicit_override_until[r] = None
            else:
                self.explicit_override_until[r] = None


# ----------------------------------------------------------------- scheduler state


class SchedulerState:
    """Persists across ticks. Tracks what WE commanded vs manual user toggles."""

    # Override with SMART_AC_STATE_PATH env var; systemd unit ships a sensible default.
    PATH = pathlib.Path(os.environ.get(
        "SMART_AC_STATE_PATH",
        str(pathlib.Path.home() / "smart_ac_state.json"),
    ))

    def __init__(self):
        self.last_action_at: dict[str, str] = {}  # ISO datetime
        self.manual_override_until: dict[str, str] = {}

    @classmethod
    def load(cls) -> "SchedulerState":
        s = cls()
        if cls.PATH.is_file():
            try:
                d = json.loads(cls.PATH.read_text())
                s.last_action_at = d.get("last_action_at", {})
                s.manual_override_until = d.get("manual_override_until", {})
            except Exception:
                pass
        return s

    def save(self) -> None:
        self.PATH.write_text(json.dumps({
            "last_action_at": self.last_action_at,
            "manual_override_until": self.manual_override_until,
        }, indent=2))

    def get_dt(self, dct: dict[str, str], room: str) -> dt.datetime | None:
        v = dct.get(room)
        return parse_dt(v) if v else None


# ----------------------------------------------------------------- decision logic


def room_needs_cooling(
    room: str, snap: Snapshot, comfort_target_f: float, outdoor_hot_threshold_f: float
) -> bool:
    indoor = snap.indoor_f.get(room)
    if indoor is not None:
        return indoor > comfort_target_f
    return snap.outdoor_f > outdoor_hot_threshold_f


def effective_params(snap: Snapshot, cfg: dict) -> dict:
    """Returns the in-effect knobs for this tick.

    Mode precedence (first match wins):
      1. vacation_mode   -- stricter than unoccupied: tighter max, larger
         comfort target. Setup via setup_smart_ac_modes.py.
      2. unoccupied      -- the classic "away" mode. Toggle:
         input_boolean.house_unoccupied.
      3. party_mode      -- comfortable common areas + relaxed comfort.
         Toggle: input_boolean.smart_ac_party_mode.
      4. normal          -- the default.

    Nap mode is orthogonal: it's applied AFTER decide() picks a target,
    forcibly adding cfg['nap_room'] (default 'master') to the ON list."""
    if snap.vacation_mode:
        priority = list(cfg["day_priority"])
        if priority:
            shift = snap.now.timetuple().tm_yday % len(priority)
            priority = priority[shift:] + priority[:shift]
        night_min = list(cfg.get("vacation_night_min_acs", []))
        return {
            "night_min": night_min,
            "evening_min": list(cfg.get("vacation_evening_min_acs", night_min)),
            "priority": priority,
            "comfort_target_f": cfg.get("vacation_comfort_target_f", 84),
            "outdoor_hot": cfg.get(
                "vacation_unsensored_assume_hot_above_outdoor_f", 92
            ),
            "max_total": cfg.get("vacation_max_acs_total", 1),
            "evening_extras": list(cfg.get("vacation_evening_extra_required", [])),
            "mode_label": "VACATION",
        }
    if snap.unoccupied:
        priority = list(cfg["day_priority"])
        if priority:
            shift = snap.now.timetuple().tm_yday % len(priority)
            priority = priority[shift:] + priority[:shift]
        night_min = list(cfg.get("unoccupied_night_min_acs", []))
        return {
            "night_min": night_min,
            "evening_min": list(cfg.get("unoccupied_evening_min_acs", night_min)),
            "priority": priority,
            "comfort_target_f": cfg.get("unoccupied_comfort_target_f", cfg["comfort_target_f"]),
            "outdoor_hot": cfg.get(
                "unoccupied_unsensored_assume_hot_above_outdoor_f",
                cfg["unsensored_assume_hot_above_outdoor_f"],
            ),
            "max_total": cfg.get("unoccupied_max_acs_total"),
            "evening_extras": list(cfg.get("unoccupied_evening_extra_required", [])),
            "mode_label": "UNOCC",
        }
    if snap.party_mode:
        # Party mode: always run common areas + a lower comfort target so
        # rooms feel actively cool. Bedrooms too, since we're not filtering
        # out night_min.
        night_min = list(cfg.get("party_night_min_acs",
                                 cfg["night_min_acs"] + ["living", "dining"]))
        return {
            "night_min": night_min,
            "evening_min": list(cfg.get("party_evening_min_acs", night_min)),
            "priority": list(cfg["day_priority"]),
            "comfort_target_f": cfg.get("party_comfort_target_f", 75),
            "outdoor_hot": cfg["unsensored_assume_hot_above_outdoor_f"],
            "max_total": None,
            "evening_extras": list(cfg.get("party_evening_extra_required", night_min)),
            "mode_label": "PARTY",
        }
    night_min = list(cfg["night_min_acs"])
    return {
        "night_min": night_min,
        # evening_min falls back to night_min if not explicitly configured, so
        # existing setups keep their behaviour. Explicit evening_min lets you
        # run FEWER bedrooms during the "family up but sun gone" window.
        "evening_min": list(cfg.get("evening_min_acs", night_min)),
        "priority": list(cfg["day_priority"]),
        "comfort_target_f": cfg["comfort_target_f"],
        "outdoor_hot": cfg["unsensored_assume_hot_above_outdoor_f"],
        "max_total": None,
        "evening_extras": list(cfg.get("evening_extra_required", [])),
        "mode_label": "OCC",
    }


def in_evening_period(snap: Snapshot, effective_end: dt.datetime, cfg: dict) -> bool:
    """True when we're past the solar day's end but before the configured
    bedtime_hour (local). Used to keep family-area ACs running when the
    house is still occupied but the sun is gone."""
    if "bedtime_hour" not in cfg:
        return False
    local_now = snap.now.astimezone()
    bt = local_now.replace(
        hour=cfg["bedtime_hour"], minute=0, second=0, microsecond=0
    )
    # Roll forward if bedtime_hour is a "next morning" time (e.g. 1am for late
    # nights): when bedtime_hour < 12 we assume the user means tomorrow morning.
    if bt < local_now and cfg["bedtime_hour"] < 12:
        bt = bt + dt.timedelta(days=1)
    return local_now > effective_end and local_now < bt


def battery_kwh(cfg: dict) -> float:
    return cfg["battery_ah"] * cfg["battery_nominal_v"] / 1000.0


def decide(snap: Snapshot, sched: SchedulerState, cfg: dict) -> tuple[
    dict[str, bool], str, dict[str, str]
]:
    """Returns (target_on_off_per_room, mode_string, per_room_reason)."""
    eff = effective_params(snap, cfg)
    night_min = eff["night_min"]
    evening_min = eff["evening_min"]
    priority = eff["priority"]
    comfort_target_f = eff["comfort_target_f"]
    outdoor_hot = eff["outdoor_hot"]
    max_total = eff["max_total"]
    occ_label = eff["mode_label"]
    evening_extras = eff["evening_extras"]

    # Universe = ALL rooms we might toggle (config defaults to occupied set so
    # we never lose track of an AC even in unoccupied mode).
    all_rooms = sorted(
        set(cfg["night_min_acs"]) | set(cfg["day_priority"])
        | set(night_min) | set(priority)
    )

    morning = dt.timedelta(minutes=cfg["sun_offset_morning_min"])
    evening = dt.timedelta(minutes=cfg["sun_offset_evening_min"])
    effective_start = snap.sunrise + morning
    effective_end = snap.sunset - evening

    reasons: dict[str, str] = {}

    # Mode determination. EVENING is a special "night-like" mode: past the
    # solar day but before bedtime, so still occupied, so we keep
    # family-area extras going on top of night_min.
    is_evening = in_evening_period(snap, effective_end, cfg)
    if snap.now < effective_start or (snap.now > effective_end and not is_evening):
        mode = "NIGHT"
    elif is_evening:
        mode = "EVENING"
    elif snap.soc >= cfg["soc_target_at_dark"]:
        mode = "SURPLUS"
    elif snap.battery_power_w <= 0:
        mode = "DEFICIT"
    else:
        time_until_dark_h = (effective_end - snap.now).total_seconds() / 3600
        kwh_needed = (cfg["soc_target_at_dark"] - snap.soc) * battery_kwh(cfg) / 100
        kwh_projected = (snap.battery_power_w / 1000) * time_until_dark_h
        mode = "ON_TRACK" if kwh_projected >= kwh_needed else "CHARGE_BEHIND"

    # Build target set based on mode
    def needs(r):
        return room_needs_cooling(r, snap, comfort_target_f, outdoor_hot)

    if mode == "NIGHT":
        target = set(night_min)
        for r in priority:
            reasons[r] = "skipped (NIGHT: after bedtime / before sunrise)"
    elif mode == "DEFICIT":
        target = set(night_min)
        for r in priority:
            reasons[r] = "skipped (DEFICIT: battery is discharging during the day)"
    elif mode == "CHARGE_BEHIND":
        target = set(night_min)
        for r in priority:
            reasons[r] = "skipped (CHARGE_BEHIND: won't reach SoC target by sunset)"
    elif mode == "EVENING":
        # Family still up but solar gone. Target = evening_min + evening_extras.
        # evening_min is usually smaller than night_min (only master bedroom
        # required, not kyle/guest, since kids/guests are up in family areas).
        # night_min re-engages when we cross bedtime_hour into NIGHT.
        target = set(evening_min) | set(evening_extras)
        for r in evening_min:
            if r not in reasons:
                reasons[r] = "added (EVENING: in evening_min_acs)"
        for r in evening_extras:
            reasons[r] = "added (EVENING: in evening_extra_required)"
        # Bedrooms in night_min but NOT in evening_min get an explicit skip
        # reason so the report says why kyle / guest are off during evening.
        for r in night_min:
            if r not in target:
                reasons[r] = "skipped (EVENING: not in evening_min_acs; only required after bedtime)"
        for r in priority:
            if r in target:
                continue
            reasons[r] = "skipped (EVENING: past solar day, not in evening_extra_required)"
    elif mode == "ON_TRACK":
        target = set(night_min)
        for r in priority:
            if r in target:
                continue
            if needs(r) and len(target) < len(night_min) + 1:
                target.add(r)
                reasons[r] = "added (ON_TRACK: 1-extra slot, room needs cooling)"
            else:
                reasons[r] = (
                    f"skipped (ON_TRACK: room temp <= {comfort_target_f}F)"
                    if not needs(r)
                    else "skipped (ON_TRACK: 1-extra slot already taken)"
                )
    else:  # SURPLUS
        target = set(night_min)
        for r in priority:
            if needs(r):
                target.add(r)
                reasons[r] = "added (SURPLUS: SoC at target, room needs cooling)"
            else:
                reasons[r] = (
                    f"skipped (SURPLUS: room temp <= {comfort_target_f}F)"
                )

    # Hard cap (unoccupied mode): drop lowest-priority extras until under cap.
    # night_min stays no matter what (configurable, usually empty in unoccupied).
    if max_total is not None and len(target) > max_total:
        keep = list(night_min)
        for r in priority:
            if r in target and len(keep) < max_total:
                keep.append(r)
                continue
            if r in target:
                target.discard(r)
                reasons[r] = f"skipped (unoccupied cap {max_total} reached)"
        target = set(keep)

    # night_min reasons (and unoccupied label hint)
    for r in night_min:
        if r not in reasons:
            reasons[r] = f"night_min ({occ_label.lower()})"
    # Rooms outside any mode list (shouldn't happen, but log)
    for r in all_rooms:
        if r not in reasons:
            reasons[r] = "skipped (not in active priority or night_min)"

    # Cold-start baseline of last_action_at so future ticks can distinguish
    # our writes from external ones. We no longer WRITE to
    # manual_override_until (auto-detect removed) but keep the last_action_at
    # tracking so we don't accidentally reintroduce the auto-pin behaviour.
    for r in all_rooms:
        if sched.get_dt(sched.last_action_at, r) is None:
            sched.last_action_at[r] = snap.now.isoformat()

    final: dict[str, bool] = {}
    for r in all_rooms:
        # Only explicit user overrides (via input_datetime) hold. The old
        # "auto-detect a manual toggle and pin for 30 min" heuristic was
        # removed -- it fought with the explicit override system (setting an
        # /override on a room ALSO flipped the input_boolean, and the auto
        # detection saw that flip as a raw user action, extending the pin
        # by 30 min past the requested time). Users who want a 30-min hold
        # can use /override <room> for 30m.
        explicit = snap.explicit_override_until.get(r)
        if explicit and snap.now < explicit:
            final[r] = snap.ac_on[r]
            reasons[r] = (
                f"user override until {explicit.astimezone().strftime('%H:%M')}"
            )
            continue
        final[r] = r in target

    return final, mode, reasons


# ----------------------------------------------------------------- apply


def apply_targets(
    target: dict[str, bool], snap: Snapshot, sched: SchedulerState, cfg: dict
) -> list[tuple[str, str]]:
    """Issue input_boolean.turn_on/off for rooms where target differs from current,
    subject to hysteresis. Returns list of (room, action) for log."""
    actions = []
    for room, want_on in target.items():
        currently_on = snap.ac_on[room]
        if want_on == currently_on:
            continue
        last = snap.ac_last_changed.get(room, snap.now - dt.timedelta(days=1))
        elapsed_min = (snap.now - last).total_seconds() / 60
        if want_on and elapsed_min < cfg["min_off_minutes"]:
            continue  # too soon to turn on
        if not want_on and elapsed_min < cfg["min_on_minutes"]:
            continue  # too soon to turn off
        svc = "turn_on" if want_on else "turn_off"
        ha_call(cfg, "input_boolean", svc, {"entity_id": f"input_boolean.ac_{room}"})
        sched.last_action_at[room] = snap.now.isoformat()
        actions.append((room, svc))
    return actions


# ----------------------------------------------------------------- status sensor


def log_decision(
    snap: Snapshot,
    mode: str,
    target: dict[str, bool],
    actions: list[tuple[str, str]],
    reasons: dict[str, str],
) -> None:
    """Append one JSON record to decisions.log (rotates at 10MB)."""
    rec = {
        "ts": snap.now.isoformat(),
        "mode": mode,
        "soc": round(snap.soc, 1),
        "battery_power_w": round(snap.battery_power_w),
        "pv_power_w": round(snap.pv_power_w),
        "load_w": round(snap.load_w),
        "outdoor_f": round(snap.outdoor_f, 1),
        "indoor_f": {k: round(v, 1) for k, v in snap.indoor_f.items()},
        "enabled": snap.enabled,
        "target_on": sorted([r for r, on in target.items() if on]),
        "actions": [f"{r}:{a}" for r, a in actions],
        "reasons": reasons,
    }
    _decisions_logger.info(json.dumps(rec, default=str))


def push_logbook(cfg: dict, message: str, entity_id: str | None = None) -> None:
    """Push a human-readable entry to HA's logbook (visible in HA UI -> Logbook).
    Used for mode transitions and actions so the logbook shows meaningful events
    without being spammed every tick."""
    body: dict = {"name": "Smart AC", "message": message}
    if entity_id:
        body["entity_id"] = entity_id
    try:
        ha_call(cfg, "logbook", "log", body)
    except Exception as e:
        logging.warning("logbook push failed: %s", e)


def push_telegram(cfg: dict, message: str) -> None:
    """Send a Telegram message via notify.send_message. Caller decides
    when -- this is just the wire."""
    target = cfg.get("notify_target")
    if not target:
        return
    try:
        ha_call(cfg, "notify", "send_message", {
            "entity_id": target,
            "message": message,
        })
    except Exception as e:
        logging.warning("telegram push failed: %s", e)


def publish_status(
    snap: Snapshot,
    mode: str,
    target: dict[str, bool],
    actions: list[tuple[str, str]],
    reasons: dict[str, str],
    cfg: dict,
) -> None:
    state_attrs = {
        "mode": mode,
        "soc": round(snap.soc, 1),
        "battery_power_w": round(snap.battery_power_w),
        "pv_power_w": round(snap.pv_power_w),
        "load_w": round(snap.load_w),
        "outdoor_f": round(snap.outdoor_f, 1),
        "indoor_f": {k: round(v, 1) for k, v in snap.indoor_f.items()},
        "enabled": snap.enabled,
        "unoccupied": snap.unoccupied,
        "notify_telegram": snap.notify_telegram,
        "target_on": sorted([r for r, on in target.items() if on]),
        "target_off": sorted([r for r, on in target.items() if not on]),
        "reasons": reasons,
        "actions_this_tick": [f"{r}: {a}" for r, a in actions],
        "last_decision_at": snap.now.isoformat(),
        "sunrise": snap.sunrise.isoformat(),
        "sunset": snap.sunset.isoformat(),
        "friendly_name": "Smart AC status",
        "icon": "mdi:air-conditioner",
    }
    ha_set_state(cfg, cfg["status_sensor_entity"], mode, state_attrs)


# ----------------------------------------------------------------- main loop


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    cfg_path = pathlib.Path(os.environ.get("SMART_AC_CONFIG", here / "smart_ac.json"))
    cfg = load_config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO").upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if "HA_TOKEN" not in os.environ:
        logging.error("HA_TOKEN env var required")
        return 1

    interval_s = cfg["evaluation_interval_minutes"] * 60
    logging.info("smart_ac starting; eval every %d sec, ha_url=%s", interval_s, cfg["ha_url"])

    last_mode: str | None = None

    while True:
        try:
            # Reload state each tick so external writers (web.py's POST
            # /override endpoint that lets a user pin an AC state until a
            # specific time) can update it between ticks.
            sched = SchedulerState.load()
            snap = Snapshot(cfg)
            target, mode, reasons = decide(snap, sched, cfg)
            actions: list[tuple[str, str]] = []
            if snap.enabled:
                actions = apply_targets(target, snap, sched, cfg)
            else:
                logging.info("disabled (preview mode); not applying actions")
            sched.save()
            publish_status(snap, mode, target, actions, reasons, cfg)
            log_decision(snap, mode, target, actions, reasons)

            # HA Logbook: only on mode change or when actions taken (avoid spam).
            occ_tag = "unocc" if snap.unoccupied else "occ"
            target_str = ", ".join(sorted(r for r, on in target.items() if on)) or "(none)"
            if mode != last_mode:
                msg = f"Mode {last_mode or '?'} -> {mode} ({occ_tag}), target = {target_str}"
                push_logbook(cfg, msg, entity_id=cfg["status_sensor_entity"])
                if snap.notify_telegram:
                    push_telegram(cfg, f"Smart AC: {msg}")
                last_mode = mode
            for room, svc in actions:
                act = svc.replace("turn_", "").upper()
                # Pull the per-room reason that was already computed in
                # decide() so the log and the Telegram both explain WHY
                # this AC just changed state.
                reason = reasons.get(room, "?")
                push_logbook(
                    cfg,
                    f"{act} {room} -- {reason} (mode {mode}, {occ_tag})",
                    entity_id=f"input_boolean.ac_{room}",
                )
                if snap.notify_telegram:
                    push_telegram(
                        cfg,
                        f"Smart AC: {act} {room} -- {reason} "
                        f"(mode {mode}, {occ_tag})",
                    )

            # Pretty action list with per-room reasons in the journal too,
            # so journalctl -u smart-ac tells you the WHY immediately.
            actions_pretty = [
                f"{svc.replace('turn_', '').upper()}:{room}({reasons.get(room, '?')})"
                for room, svc in actions
            ]
            logging.info(
                "mode=%s soc=%.1f%% batt=%dW pv=%dW load=%dW outdoor=%.1fF "
                "target=%s actions=%s",
                mode, snap.soc, snap.battery_power_w, snap.pv_power_w, snap.load_w,
                snap.outdoor_f,
                sorted([r for r, on in target.items() if on]),
                actions_pretty,
            )
        except Exception:
            logging.exception("tick failed")
        time.sleep(interval_s)


if __name__ == "__main__":
    sys.exit(main())
