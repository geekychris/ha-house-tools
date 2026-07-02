"""Shared pytest fixtures.

Most of what smart_ac decides on can be tested without a live Home
Assistant — the interesting functions (``room_needs_cooling``,
``effective_params``, ``in_evening_period``, ``battery_kwh``, and the
big ``decide()``) are pure over their inputs.

The ``Snapshot`` class does reach out to HA in its constructor, so we
provide a ``make_snapshot`` factory that bypasses ``__init__`` and
lets each test hand-craft the fields it needs.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

# Make ``smart_ac`` importable when running pytest from the repo root.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smart_ac import smart_ac as sa  # noqa: E402


@pytest.fixture
def base_config() -> dict:
    """Minimal valid config with a 3-room house. Tweak per test."""
    return {
        "sun_offset_morning_min": 90,
        "sun_offset_evening_min": 120,
        "soc_target_at_dark": 100,
        "night_min_acs": ["master"],
        "day_priority": ["living", "office"],
        "bedtime_hour": 22,
        "ac_power_estimate_w": 1000,
        "comfort_target_f": 78,
        "unsensored_assume_hot_above_outdoor_f": 80,
        "min_on_minutes": 15,
        "min_off_minutes": 10,
        "manual_override_minutes": 30,
        "evaluation_interval_minutes": 5,
        "enabled_entity": "input_boolean.smart_ac_enabled",
        "indoor_sensor_for_room": {},
        "outdoor_sensor": "sensor.outdoor",
        "soc_sensor": "sensor.soc",
        "battery_power_sensor": "sensor.batt_p",
        "pv_power_sensor": "sensor.pv_p",
        "load_sensor": "sensor.load",
        "battery_ah": 840,
        "battery_nominal_v": 51.2,
        "status_sensor_entity": "sensor.smart_ac_status",
        "ha_url": "http://ha.example.local:8123",
    }


def make_snapshot(
    *,
    cfg: dict,
    now: dt.datetime | None = None,
    sunrise: dt.datetime | None = None,
    sunset: dt.datetime | None = None,
    soc: float = 80.0,
    battery_power_w: float = 0.0,
    pv_power_w: float = 0.0,
    load_w: float = 800.0,
    outdoor_f: float = 85.0,
    indoor_f: dict[str, float] | None = None,
    enabled: bool = True,
    notify_telegram: bool = False,
    unoccupied: bool = False,
    ac_on: dict[str, bool] | None = None,
    ac_last_changed: dict[str, dt.datetime] | None = None,
    explicit_override_until: dict[str, dt.datetime | None] | None = None,
):
    """Build a Snapshot without hitting HA. Every field has a default;
    override what your test cares about."""
    s = object.__new__(sa.Snapshot)
    s.cfg = cfg
    s.now = now or dt.datetime(2026, 7, 15, 14, 0, tzinfo=dt.timezone.utc)
    s.sunrise = sunrise or s.now.replace(hour=13)      # 06:00 local at UTC-7
    s.sunset  = sunset  or s.now.replace(hour=3) + dt.timedelta(days=1)  # 20:00 local next day
    s.soc = soc
    s.battery_power_w = battery_power_w
    s.pv_power_w = pv_power_w
    s.load_w = load_w
    s.outdoor_f = outdoor_f
    s.indoor_f = dict(indoor_f or {})
    s.enabled = enabled
    s.notify_telegram = notify_telegram
    s.unoccupied = unoccupied
    all_rooms = sorted(set(cfg["night_min_acs"]) | set(cfg["day_priority"]))
    s.ac_on = {r: False for r in all_rooms}
    if ac_on:
        s.ac_on.update(ac_on)
    s.ac_last_changed = {
        r: s.now - dt.timedelta(days=1) for r in all_rooms
    }
    if ac_last_changed:
        s.ac_last_changed.update(ac_last_changed)
    s.explicit_override_until = {r: None for r in all_rooms}
    if explicit_override_until:
        s.explicit_override_until.update(explicit_override_until)
    return s


@pytest.fixture
def snapshot_factory(base_config):
    """Convenience: `snap = snapshot_factory(soc=50, outdoor_f=95)`."""
    def make(**overrides):
        return make_snapshot(cfg=base_config, **overrides)
    return make
