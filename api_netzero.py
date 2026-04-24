import requests
import config
from logger import log_netzero

def get_powerwall_stats() -> dict:
    url = f"{config.NETZERO_BASE_URL}/{config.NETZERO_SITE_ID}/config"
    r   = requests.get(
        url,
        headers={"Authorization": f"Bearer {config.NETZERO_API_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    live = data["live_status"]

    solar_kw   = round(live["solar_power"]   / 1000, 2)
    home_kw    = round(live["load_power"]    / 1000, 2)
    grid_kw    = round(live["grid_power"]    / 1000, 2)
    battery_kw = round(live["battery_power"] / 1000, 2)
    battery_pct = round(
        live.get("percentage_charged") or data.get("percentage_charged", 0), 1
    )

    solar_surplus_kw = round(solar_kw - home_kw, 2)
    self_powered_pct = round(
        max(0, min(100, (1 - max(0, grid_kw) / max(home_kw, 0.01)) * 100)), 1
    ) if home_kw > 0 else 100.0

    log_netzero.debug(
        f"solar={solar_kw}kW | home={home_kw}kW | surplus={solar_surplus_kw}kW | "
        f"battery={battery_kw}kW ({battery_pct}%) | grid={grid_kw}kW | "
        f"self_powered={self_powered_pct}% | "
        f"island={live.get('island_status')} | storm={live.get('storm_mode_active')} | "
        f"data_ts={live.get('timestamp')}"
    )

    return {
        "battery_pct":      battery_pct,
        "solar_kw":         solar_kw,
        "home_kw":          home_kw,
        "grid_kw":          grid_kw,
        "battery_kw":       battery_kw,
        "solar_surplus_kw": solar_surplus_kw,
        "self_powered_pct": self_powered_pct,
        "island_mode":      live.get("island_status", "on_grid"),
        "storm_mode":       live.get("storm_mode_active", False),
        "data_ts":          live.get("timestamp", ""),
    }
