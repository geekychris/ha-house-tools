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

        # Occupancy source. Prefer positive-polarity `occupied_entity`
        # (ON = house is occupied); fall back to legacy `unoccupied_entity`
        # (ON = house is unoccupied) if the positive one isn't configured.
        # Both defaults degrade to "occupied" when the state is missing.
        occ_eid = cfg.get("occupied_entity")
        if occ_eid:
            occ_state = states.get(occ_eid, {}).get("state", "on")
            self.unoccupied = occ_state == "off"
        else:
            unocc_state = states.get(cfg.get("unoccupied_entity", ""), {}).get("state", "off")
            self.unoccupied = unocc_state == "on"

        # Optional mode toggles (see setup_smart_ac_modes.py). All default
        # off; the scheduler falls back to normal behaviour if the helpers
        # don't exist.
        self.party_mode = states.get("input_boolean.smart_ac_party_mode", {}).get("state") == "on"
        self.nap_mode = states.get("input_boolean.smart_ac_nap_mode", {}).get("state") == "on"
        self.vacation_mode = states.get("input_boolean.smart_ac_vacation_mode", {}).get("state") == "on"

        # Charge boost: user says "for the next N hours, favor charging over
        # comfort." Backed by input_datetime.smart_ac_charge_boost_until;
        # any future value = active. Set via /charge_boost Telegram command
        # or the setup_smart_ac_charge_boost.py helper. When active, the
        # scheduler drops to day_min_acs only (no extras) regardless of
        # DAY sub-mode, and prepends "CHARGE_BOOST" to the mode label.
        self.charge_boost_until: dt.datetime | None = None
        cb_state = states.get(
            "input_datetime.smart_ac_charge_boost_until", {}
        ).get("state")
        if cb_state and cb_state not in ("unknown", "unavailable"):
            try:
                naive = dt.datetime.strptime(cb_state, "%Y-%m-%d %H:%M:%S")
                self.charge_boost_until = naive.astimezone()
            except Exception:
                pass
        self.charge_boost = (
            self.charge_boost_until is not None
            and self.now < self.charge_boost_until
        )

        # Weather signals for decision annotation. All optional -- if the
        # weather integration isn't installed the reason strings just skip
        # the annotation. Values come from openmeteo.py.
        def _fnum(eid: str) -> float | None:
            v = states.get(eid, {}).get("state")
            if not v or v in ("unknown", "unavailable"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        self.weather_cloud_now = _fnum("sensor.weather_cloud_now")
        self.weather_expected_pv_w = _fnum("sensor.weather_expected_pv_now")
        self.weather_tomorrow_pv_kwh = _fnum("sensor.weather_tomorrow_pv_kwh")

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
        # Queued follow-up observations. Each entry describes an action
        # (or set of actions) taken on a prior tick, along with the
        # pre-action Snapshot dict. Next tick that runs OBSERVATION_LAG_MIN
        # after the action captures an "after" snapshot, computes deltas,
        # writes one line to observations.jsonl, and drops the entry.
        # See main tick loop.
        self.pending_observations: list[dict] = []

    @classmethod
    def load(cls) -> "SchedulerState":
        s = cls()
        if cls.PATH.is_file():
            try:
                d = json.loads(cls.PATH.read_text())
                s.last_action_at = d.get("last_action_at", {})
                s.manual_override_until = d.get("manual_override_until", {})
                s.pending_observations = d.get("pending_observations", [])
            except Exception:
                pass
        return s

    def save(self) -> None:
        self.PATH.write_text(json.dumps({
            "last_action_at": self.last_action_at,
            "manual_override_until": self.manual_override_until,
            "pending_observations": self.pending_observations,
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


def _weather_note(snap: Snapshot) -> str | None:
    """Return a short annotation about weather impact on PV, or None if
    nothing notable. Read from openmeteo.py-published sensors:
      - sensor.weather_cloud_now   (percent)
      - sensor.weather_tomorrow_pv_kwh
    Skips silently if either is absent."""
    parts: list[str] = []
    cloud = getattr(snap, "weather_cloud_now", None)
    if cloud is not None and cloud >= 60:
        parts.append(f"cloudy {int(cloud)}%")
    tmw = getattr(snap, "weather_tomorrow_pv_kwh", None)
    if tmw is not None and tmw < 30:
        parts.append(f"low-forecast tmw {int(tmw)}kWh")
    if not parts:
        return None
    return "weather: " + ", ".join(parts)


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
    if getattr(snap, "vacation_mode", False):
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
    if getattr(snap, "party_mode", False):
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
    # day_min_acs: which ACs are forced ON during the DAY period. Defaults
    # to night_min_acs so existing configs keep their behaviour. Set to a
    # different list (or []) to have a separate day-time baseline -- lets
    # you say "at night keep master+kyle+guest, but during the day let
    # the scheduler decide who runs based on day_priority alone."
    day_min = list(cfg.get("day_min_acs", night_min))
    return {
        "night_min": night_min,
        "day_min": day_min,
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
    # day_min may differ from night_min (see effective_params). If not set
    # in cfg it falls back to night_min so nothing changes for existing
    # configs.
    day_min = eff.get("day_min", night_min)

    # Universe = ALL rooms we might toggle (config defaults to occupied set so
    # we never lose track of an AC even in unoccupied mode).
    all_rooms = sorted(
        set(cfg["night_min_acs"]) | set(cfg["day_priority"])
        | set(night_min) | set(day_min) | set(priority)
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
        target = set(day_min)
        for r in priority:
            reasons[r] = "skipped (DEFICIT: battery is discharging during the day)"
    elif mode == "CHARGE_BEHIND":
        target = set(day_min)
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
        target = set(day_min)
        for r in priority:
            if r in target:
                continue
            if needs(r) and len(target) < len(day_min) + 1:
                target.add(r)
                reasons[r] = "added (ON_TRACK: 1-extra slot, room needs cooling)"
            else:
                reasons[r] = (
                    f"skipped (ON_TRACK: room temp <= {comfort_target_f}F)"
                    if not needs(r)
                    else "skipped (ON_TRACK: 1-extra slot already taken)"
                )
    else:  # SURPLUS
        target = set(day_min)
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

    # CHARGE_BOOST: user has asked "for the next N hours, favor charging."
    # Override target back to day_min only (no extras), regardless of the
    # mode we just computed. Skip during NIGHT / EVENING -- the family
    # comfort case dominates when the sun is already gone.
    if snap.charge_boost and mode not in ("NIGHT", "EVENING"):
        old_mode = mode
        mode = f"CHARGE_BOOST({old_mode})"
        boost_until_hm = snap.charge_boost_until.astimezone().strftime("%H:%M")
        skipped_note = (
            f"skipped (CHARGE_BOOST until {boost_until_hm}: user asked to favor charging)"
        )
        target = set(day_min)
        for r in all_rooms:
            if r not in day_min:
                reasons[r] = skipped_note

    # night_min reasons (and unoccupied label hint)
    for r in night_min:
        if r not in reasons:
            reasons[r] = f"night_min ({occ_label.lower()})"
    # day_min reasons that survived (during DAY sub-modes)
    for r in day_min:
        if r not in reasons:
            reasons[r] = f"day_min ({occ_label.lower()})"
    # Rooms outside any mode list (shouldn't happen, but log)
    for r in all_rooms:
        if r not in reasons:
            reasons[r] = "skipped (not in active priority or day_min/night_min)"

    # Weather annotation. If the forecast says PV is / will be materially
    # depressed, prepend a note to every reason so the report and Telegram
    # replies mention it. Purely informational -- decisions themselves
    # already account for actual live PV via the mode computation.
    weather_note = _weather_note(snap)
    if weather_note:
        for r in list(reasons.keys()):
            reasons[r] = f"[{weather_note}] {reasons[r]}"

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
        "ac_on": dict(snap.ac_on),
        "enabled": snap.enabled,
        "unoccupied": snap.unoccupied,
        "target": {r: on for r, on in target.items()},
        "target_on": sorted([r for r, on in target.items() if on]),
        "actions": [f"{r}:{a}" for r, a in actions],
        "reasons": reasons,
    }
    _decisions_logger.info(json.dumps(rec, default=str))
    # Mirror to SQLite for long-term queries. Best effort -- a DB failure
    # doesn't affect the tick.
    conn = _stats_conn()
    if conn is not None:
        try:
            _stats.insert_decision(conn, rec)  # type: ignore
        except Exception as e:
            logging.warning("stats.insert_decision failed: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


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


OBSERVATIONS_LOG = pathlib.Path(os.environ.get(
    "SMART_AC_OBSERVATIONS_LOG",
    str(pathlib.Path.home() / "smart_ac" / "observations.jsonl"),
))
OBSERVATION_LAG_MIN = int(os.environ.get("OBSERVATION_LAG_MIN", "3"))
OBSERVATION_MAX_AGE_MIN = int(os.environ.get("OBSERVATION_MAX_AGE_MIN", "20"))


# Optional SQLite mirror. If stats.py can't be imported (e.g. running an
# old checkout where the module doesn't exist), gracefully fall back to
# JSONL-only. Every DB write is best-effort and wrapped in try/except so
# a bad DB never breaks the tick loop.
try:
    from . import stats as _stats  # type: ignore
except (ImportError, ValueError):
    try:
        import stats as _stats  # type: ignore
    except ImportError:
        _stats = None  # type: ignore


def _stats_conn():
    if _stats is None:
        return None
    try:
        conn = _stats.connect()
        _stats.init(conn)
        return conn
    except Exception as e:
        logging.warning("stats DB unavailable: %s", e)
        return None


def snap_to_dict(snap: Snapshot, mode: str | None = None) -> dict:
    """Compact serialisable snapshot for observation records."""
    return {
        "ts": snap.now.astimezone(dt.timezone.utc).isoformat(),
        "load_w": int(snap.load_w),
        "pv_power_w": int(snap.pv_power_w),
        "battery_power_w": int(snap.battery_power_w),
        "soc": round(snap.soc, 1),
        "outdoor_f": round(snap.outdoor_f, 1),
        "indoor_f": {k: round(v, 1) for k, v in snap.indoor_f.items()},
        "ac_on": dict(snap.ac_on),
        "mode": mode,
    }


def calibration_expected_delta(actions: list[tuple[str, str]], cfg: dict) -> dict:
    """Return the expected load delta (W) if the calibration values held
    perfectly. Positive delta = load should have gone UP.
    Pulled from sensor.smart_ac_calibration if available, else uses
    ac_power_estimate_w as fallback."""
    default_w = float(cfg.get("ac_power_estimate_w", 1000))
    per_room_w = {}
    try:
        calib = ha_get(cfg, "/api/states/sensor.smart_ac_calibration")
        results = ((calib or {}).get("attributes") or {}).get("results") or {}
        for room, info in results.items():
            if isinstance(info, dict) and info.get("note") == "ok":
                per_room_w[room] = int(info.get("delta_w", default_w))
    except Exception:
        pass

    per_action_expected: list[dict] = []
    total_delta = 0
    for room, svc in actions:
        w = per_room_w.get(room, int(default_w))
        source = "measured" if room in per_room_w else "default"
        signed = w if svc == "turn_on" else -w
        total_delta += signed
        per_action_expected.append({
            "room": room, "action": svc,
            "expected_delta_w": signed,
            "watts_source": source,
        })
    return {
        "total_expected_delta_w": total_delta,
        "per_action": per_action_expected,
    }


def enqueue_observation(
    sched: SchedulerState,
    snap: Snapshot,
    mode: str,
    reasons: dict[str, str],
    actions: list[tuple[str, str]],
    cfg: dict,
) -> None:
    """Record the pre-action state + action list so a later tick can
    measure the after-state and compute deltas. Called from the tick
    loop only when actions actually fired."""
    if not actions:
        return
    entry = {
        "ts_action": snap.now.astimezone(dt.timezone.utc).isoformat(),
        "actions": [
            {"room": r, "action": s, "reason": reasons.get(r, "?"), "mode": mode}
            for (r, s) in actions
        ],
        "before": snap_to_dict(snap, mode),
        "expected": calibration_expected_delta(actions, cfg),
    }
    sched.pending_observations.append(entry)


def process_observations(sched: SchedulerState, snap: Snapshot, mode: str) -> None:
    """Consume any pending observations that are old enough to measure.
    For each, compute deltas against the current snapshot, write one
    JSON line to observations.jsonl, and drop the pending entry.
    Also drops entries older than OBSERVATION_MAX_AGE_MIN as stale
    (e.g. after a long outage), rather than measuring against an
    unrelated snapshot."""
    if not sched.pending_observations:
        return
    now = snap.now
    still_pending: list[dict] = []
    OBSERVATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    for entry in sched.pending_observations:
        try:
            ts_action = parse_dt(entry["ts_action"])
        except Exception:
            continue
        age_min = (now - ts_action).total_seconds() / 60
        if age_min < OBSERVATION_LAG_MIN:
            still_pending.append(entry)
            continue
        if age_min > OBSERVATION_MAX_AGE_MIN:
            logging.info("dropping stale observation (age %.1fm > %d): actions=%s",
                         age_min, OBSERVATION_MAX_AGE_MIN,
                         [(a["room"], a["action"]) for a in entry["actions"]])
            continue

        before = entry["before"]
        after = snap_to_dict(snap, mode)
        deltas = {
            "load_w": after["load_w"] - before["load_w"],
            "battery_power_w": after["battery_power_w"] - before["battery_power_w"],
            "pv_power_w": after["pv_power_w"] - before["pv_power_w"],
            "soc": round(after["soc"] - before["soc"], 2),
            "outdoor_f": round(after["outdoor_f"] - before["outdoor_f"], 1),
            "indoor_f": {
                r: round(after["indoor_f"].get(r, 0) - before["indoor_f"].get(r, 0), 1)
                for r in set(before["indoor_f"]) | set(after["indoor_f"])
            },
        }
        expected_delta = entry["expected"].get("total_expected_delta_w", 0)
        obs = {
            "ts_observed": now.astimezone(dt.timezone.utc).isoformat(),
            "age_min": round(age_min, 1),
            "actions": entry["actions"],
            "before": before,
            "after": after,
            "deltas": deltas,
            "expected": entry["expected"],
            "delta_vs_expected_w": deltas["load_w"] - expected_delta,
        }
        try:
            with OBSERVATIONS_LOG.open("a") as f:
                f.write(json.dumps(obs, default=str) + "\n")
            logging.info(
                "observation: %s -> load %+dW (expected %+dW, diff %+dW), soc %+.2f%%",
                ", ".join(f"{a['action']}:{a['room']}" for a in entry["actions"]),
                deltas["load_w"],
                expected_delta,
                deltas["load_w"] - expected_delta,
                deltas["soc"],
            )
        except Exception as e:
            logging.warning("failed to write observation: %s", e)
        # Mirror to SQLite. Best effort.
        conn = _stats_conn()
        if conn is not None:
            try:
                _stats.insert_observation(conn, obs)  # type: ignore
            except Exception as e:
                logging.warning("stats.insert_observation failed: %s", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    sched.pending_observations = still_pending


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
            # Consume any pending observations from prior ticks BEFORE we
            # act again. Uses the fresh snapshot as the "after" state.
            # We pass in the mode from decide() below via a re-check, but
            # for observation timing what matters is `now` and the
            # pre-computed mode label; we approximate with "?" here since
            # decide() runs next -- the observation record already captured
            # the mode at action-time in `before`.
            process_observations(sched, snap, mode="?")
            target, mode, reasons = decide(snap, sched, cfg)
            actions: list[tuple[str, str]] = []
            if snap.enabled:
                actions = apply_targets(target, snap, sched, cfg)
            else:
                logging.info("disabled (preview mode); not applying actions")
            # Queue this tick's actions for observation on the next tick.
            enqueue_observation(sched, snap, mode, reasons, actions, cfg)
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
