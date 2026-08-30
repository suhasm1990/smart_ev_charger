"""NetZero Energy API client for live Tesla Powerwall telemetry."""
import time

import requests

from core import config
from reporting.logger import log_netzero

_SOLAR_NOISE_FLOOR_KW = 0.05  # Inverter idle noise reads as a few watts.

# Worst case ≈ 3×10s requests + 3s backoff = 33s. That fits the 45s cycle
# budget only because the ChargePoint calls in the same cycle rarely fail at
# the same time — keep both budgets in mind when changing either.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 2.0)

_session = requests.Session()


def _fetch() -> dict:
    # A pooled session keeps the TLS connection warm between 15-minute cycles.
    response = _session.get(
        f"{config.NETZERO_BASE_URL}/{config.NETZERO_SITE_ID}/config",
        headers={"Authorization": f"Bearer {config.NETZERO_API_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_powerwall_stats() -> dict:
    """Fetches live solar, home, grid, and battery figures for the site.

    Transient failures (network errors, 5xx) are retried so one blip does not
    cost a whole control cycle; 4xx responses raise immediately.
    """
    if not config.NETZERO_SITE_ID or not config.NETZERO_API_TOKEN:
        raise ValueError("NETZERO_SITE_ID and NETZERO_API_TOKEN must be configured.")

    for attempt in range(_MAX_ATTEMPTS):
        try:
            data = _fetch()
            break
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status >= 500
            if not retryable or attempt == _MAX_ATTEMPTS - 1:
                raise
            log_netzero.warning(f"NetZero request failed (attempt {attempt + 1}/{_MAX_ATTEMPTS}): {e}")
            time.sleep(_BACKOFF_SECONDS[attempt])

    live = data.get("live_status")
    if not isinstance(live, dict):
        raise ValueError("NetZero response is missing 'live_status' — API schema may have changed")
    missing = [k for k in ("solar_power", "load_power", "grid_power", "battery_power") if k not in live]
    if missing:
        raise ValueError(f"NetZero live_status is missing fields: {', '.join(missing)}")

    solar_kw = max(0.0, round(live["solar_power"] / 1000, 2))
    if solar_kw < _SOLAR_NOISE_FLOOR_KW:
        solar_kw = 0.0
    home_kw = max(0.0, round(live["load_power"] / 1000, 2))
    grid_kw = round(live["grid_power"] / 1000, 2)

    stats = {
        "battery_pct":      round(live.get("percentage_charged") or data.get("percentage_charged", 0), 1),
        "solar_kw":         solar_kw,
        "home_kw":          home_kw,
        "grid_kw":          grid_kw,
        "battery_kw":       round(live["battery_power"] / 1000, 2),
        "solar_surplus_kw": round(solar_kw - home_kw, 2),
        "grid_export_kw":   round(max(0.0, -grid_kw), 2),
        "self_powered_pct": round(max(0.0, min(100.0, (1 - max(0.0, grid_kw) / home_kw) * 100)), 1) if home_kw > 0 else 100.0,
        "island_mode":      live.get("island_status", "on_grid"),
        "storm_mode":       live.get("storm_mode_active", False),
        "data_ts":          live.get("timestamp", ""),
    }

    log_netzero.debug(
        f"solar={stats['solar_kw']}kW | home={stats['home_kw']}kW | "
        f"surplus={stats['solar_surplus_kw']}kW | grid={stats['grid_kw']}kW | "
        f"battery={stats['battery_kw']}kW ({stats['battery_pct']}%) | "
        f"self_powered={stats['self_powered_pct']}% | island={stats['island_mode']} | "
        f"storm={stats['storm_mode']}"
    )
    return stats
