"""Hourly solar irradiance forecast from Open-Meteo (free, no API key)."""
from datetime import datetime

import requests

from core import config
from reporting.logger import log

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PAST_DAYS = 7  # Matches the 7-day history the planner's solar averages cover.


def get_solar_forecast(now: datetime = None) -> dict | None:
    """Today's hourly irradiance and cloud cover, plus the past week's average irradiance.

    The past-week average lets the planner scale its own logged solar output by
    how much brighter or darker today is expected to be. Returns None when no
    site location is configured or the request fails, so the planner reports
    the forecast as unavailable instead of guessing.
    """
    if not (config.SITE_LATITUDE and config.SITE_LONGITUDE):
        return None
    now = now or datetime.now(config.TZ)
    try:
        response = requests.get(OPEN_METEO_URL, params={
            "latitude": config.SITE_LATITUDE,
            "longitude": config.SITE_LONGITUDE,
            "hourly": "shortwave_radiation,cloud_cover",
            "timezone": config.TZ.key,
            "past_days": PAST_DAYS,
            "forecast_days": 1,
        }, timeout=10)
        response.raise_for_status()
        hourly = response.json()["hourly"]
        rows = zip(hourly["time"], hourly["shortwave_radiation"], hourly["cloud_cover"])
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning(f"Solar forecast unavailable: {e}")
        return None

    today = now.date().isoformat()
    radiation, cloud = {}, {}
    past: dict[int, list[float]] = {}
    for stamp, rad, cover in rows:
        if rad is None:
            continue
        hour = int(stamp[11:13])
        if stamp.startswith(today):
            radiation[hour], cloud[hour] = rad, cover
        else:
            past.setdefault(hour, []).append(rad)
    if not radiation:
        return None
    return {
        "radiation_wm2": radiation,
        "cloud_cover_pct": cloud,
        "past_avg_radiation_wm2": {h: sum(v) / len(v) for h, v in past.items()},
    }
