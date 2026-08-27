"""NetZero Energy API client for live Tesla Powerwall telemetry."""
import requests

from core import config
from reporting.logger import log_netzero

_SOLAR_NOISE_FLOOR_KW = 0.05  # Inverter idle noise reads as a few watts.

_session = requests.Session()


def get_powerwall_stats() -> dict:
    """Fetches live solar, home, grid, and battery figures for the site."""
    if not config.NETZERO_SITE_ID or not config.NETZERO_API_TOKEN:
        raise ValueError("NETZERO_SITE_ID and NETZERO_API_TOKEN must be configured.")

    # A pooled session keeps the TLS connection warm between 15-minute cycles.
    response = _session.get(
        f"{config.NETZERO_BASE_URL}/{config.NETZERO_SITE_ID}/config",
        headers={"Authorization": f"Bearer {config.NETZERO_API_TOKEN}"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    live = data["live_status"]

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
