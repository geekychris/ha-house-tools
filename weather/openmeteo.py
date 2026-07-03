#!/usr/bin/env python3
"""
Open-Meteo weather fetcher.

Runs as a systemd timer (see weather.timer / weather.service). Every 30
min it fetches the hourly forecast for a configured lat/lon from the
free Open-Meteo API, computes a small number of derived values useful
to smart_ac (expected PV, cloud cover next hour, tomorrow max temp,
tomorrow expected PV), and pushes them to HA as sensor entities.

Downstream consumers:
  * anomaly.py -- compares live PV to expected_pv_now to flag
    underperformance.
  * smart_ac.py -- reads sensor.weather_tomorrow_pv_kwh to nudge
    soc_target_at_dark up on cloudy days.
  * dashboards / Telegram -- surface the forecast as human-readable text.

Config: reads weather.json (sibling file). Copy weather.example.json to
weather.json, set your lat/lon + panel wattage.

Env:
  HA_URL, HA_TOKEN -- as in smart_ac.env.
  WEATHER_CONFIG -- override path to weather.json.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "weather.json"


def load_config() -> dict:
    p = pathlib.Path(os.environ.get("WEATHER_CONFIG", DEFAULT_CFG))
    if not p.is_file():
        sys.exit(f"weather.json not found at {p}. Copy weather.example.json to weather.json and fill it in.")
    return json.loads(p.read_text())


def ha_set_state(cfg: dict, entity_id: str, state, attrs: dict) -> None:
    url = f"{os.environ['HA_URL'].rstrip('/')}/api/states/{entity_id}"
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
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def fetch_forecast(cfg: dict) -> dict:
    """Fetch the next ~48 h of hourly forecast + tomorrow's daily summary."""
    lat = cfg["latitude"]
    lon = cfg["longitude"]
    tz = cfg.get("timezone", "auto")
    hourly = ",".join([
        "cloud_cover",
        "shortwave_radiation",       # W/m^2 -- proxy for PV production
        "temperature_2m",
        "apparent_temperature",
        "precipitation",
    ])
    daily = ",".join([
        "temperature_2m_max",
        "temperature_2m_min",
        "shortwave_radiation_sum",   # MJ/m^2 -- total daily radiation
        "cloud_cover_mean",
        "precipitation_sum",
    ])
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&timezone={tz}"
        f"&hourly={hourly}&daily={daily}"
        f"&temperature_unit=fahrenheit&forecast_days=3"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _idx_at_hour(hourly_times: list[str], target_hour_iso: str) -> int | None:
    """Find the index of target_hour_iso (or the closest hour) in hourly.time."""
    for i, t in enumerate(hourly_times):
        if t.startswith(target_hour_iso):
            return i
    return None


def summarise(fc: dict, cfg: dict, now: dt.datetime) -> dict:
    """Reduce the raw forecast to a compact dict of sensor values.

    Uses `panel_kw` (peak nameplate DC) to convert shortwave radiation
    (W/m^2) into an expected AC output (W). Rough model:
      expected_w = radiation_w_per_m2 * panel_kw * 1000 / 1000 * eta
                 = radiation_w_per_m2 * panel_kw * eta
    where eta is a system efficiency factor (default 0.75). The
    coefficient is intentionally simple; anomaly detection cares about
    the *ratio* of actual to expected, not absolute accuracy.
    """
    hourly = fc.get("hourly", {}) or {}
    times: list[str] = hourly.get("time", []) or []
    cloud: list[float] = hourly.get("cloud_cover", []) or []
    rad: list[float] = hourly.get("shortwave_radiation", []) or []
    temp: list[float] = hourly.get("temperature_2m", []) or []

    panel_kw = float(cfg.get("panel_kw", 15.0))
    eta = float(cfg.get("system_efficiency", 0.75))

    now_hour = now.strftime("%Y-%m-%dT%H:00")
    i_now = _idx_at_hour(times, now_hour)
    if i_now is None:
        i_now = 0

    def val(a: list[float], i: int, default=0):
        return a[i] if 0 <= i < len(a) else default

    cloud_now = val(cloud, i_now)
    rad_now = val(rad, i_now)
    temp_now = val(temp, i_now)
    exp_w_now = round(rad_now * panel_kw * eta)

    # Next-hour lookahead
    cloud_next = val(cloud, i_now + 1)
    rad_next = val(rad, i_now + 1)
    exp_w_next = round(rad_next * panel_kw * eta)

    # Tomorrow (day index 1 in daily arrays)
    daily = fc.get("daily", {}) or {}
    d_times = daily.get("time", []) or []
    d_tmax = daily.get("temperature_2m_max", []) or []
    d_tmin = daily.get("temperature_2m_min", []) or []
    d_rad_sum = daily.get("shortwave_radiation_sum", []) or []
    d_cloud = daily.get("cloud_cover_mean", []) or []
    d_precip = daily.get("precipitation_sum", []) or []

    def d(a, i, default=0):
        return a[i] if 0 <= i < len(a) else default

    # Tomorrow's expected PV energy (kWh) = radiation_sum (MJ/m^2) * conversion
    # radiation_sum in MJ/m^2/day; convert MJ -> kWh (1 MJ = 0.2778 kWh)
    tmw_rad_mj = d(d_rad_sum, 1)
    tmw_pv_kwh = round(tmw_rad_mj * 0.2778 * panel_kw * eta, 1)

    tmw_tmax_f = d(d_tmax, 1)
    tmw_tmin_f = d(d_tmin, 1)
    tmw_cloud = d(d_cloud, 1)
    tmw_precip_mm = d(d_precip, 1)

    return {
        "cloud_now_pct": cloud_now,
        "cloud_next_hr_pct": cloud_next,
        "temp_now_f": temp_now,
        "expected_pv_w_now": exp_w_now,
        "expected_pv_w_next_hr": exp_w_next,
        "tmw_pv_kwh": tmw_pv_kwh,
        "tmw_tmax_f": tmw_tmax_f,
        "tmw_tmin_f": tmw_tmin_f,
        "tmw_cloud_pct": tmw_cloud,
        "tmw_precip_mm": tmw_precip_mm,
        "panel_kw": panel_kw,
        "system_efficiency": eta,
        "source": "open-meteo",
        "generated_at": now.astimezone(dt.timezone.utc).isoformat(),
    }


def publish(cfg: dict, s: dict) -> None:
    """Push the summary to HA as five convenient sensors."""
    ha_set_state(
        cfg,
        "sensor.weather_expected_pv_now",
        s["expected_pv_w_now"],
        {
            "unit_of_measurement": "W",
            "friendly_name": "Weather expected PV now",
            "icon": "mdi:solar-power",
            "device_class": "power",
            "cloud_now_pct": s["cloud_now_pct"],
            "cloud_next_hr_pct": s["cloud_next_hr_pct"],
            "expected_pv_w_next_hr": s["expected_pv_w_next_hr"],
            "panel_kw": s["panel_kw"],
            "system_efficiency": s["system_efficiency"],
            "source": s["source"],
            "generated_at": s["generated_at"],
        },
    )
    ha_set_state(
        cfg,
        "sensor.weather_tomorrow_pv_kwh",
        s["tmw_pv_kwh"],
        {
            "unit_of_measurement": "kWh",
            "friendly_name": "Weather tomorrow PV forecast",
            "icon": "mdi:weather-sunny",
            "tmw_tmax_f": s["tmw_tmax_f"],
            "tmw_tmin_f": s["tmw_tmin_f"],
            "tmw_cloud_pct": s["tmw_cloud_pct"],
            "tmw_precip_mm": s["tmw_precip_mm"],
            "source": s["source"],
            "generated_at": s["generated_at"],
        },
    )
    ha_set_state(
        cfg,
        "sensor.weather_cloud_now",
        s["cloud_now_pct"],
        {
            "unit_of_measurement": "%",
            "friendly_name": "Weather cloud cover now",
            "icon": "mdi:cloud",
            "source": s["source"],
            "generated_at": s["generated_at"],
        },
    )
    ha_set_state(
        cfg,
        "sensor.weather_outdoor_temp_now",
        s["temp_now_f"],
        {
            "unit_of_measurement": "°F",
            "friendly_name": "Weather outdoor temp now",
            "icon": "mdi:thermometer",
            "device_class": "temperature",
            "source": s["source"],
            "generated_at": s["generated_at"],
        },
    )


def main() -> int:
    if "HA_TOKEN" not in os.environ:
        sys.exit("HA_TOKEN env var is required.")
    if "HA_URL" not in os.environ:
        sys.exit("HA_URL env var is required.")
    cfg = load_config()
    fc = fetch_forecast(cfg)
    s = summarise(fc, cfg, dt.datetime.now().astimezone())
    publish(cfg, s)
    print(json.dumps(s, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
