import csv
import os
import requests
from datetime import datetime

import state
import config
from logger import log_csv
from tou import get_tou_period, get_tou_rate, is_expensive_period, is_in_night_blackout, is_weekend

CSV_HEADERS = [
    "timestamp", "date", "time", "day_of_week", "is_weekend",
    "tou_period", "tou_rate_per_kwh", "is_expensive",
    "solar_kw", "home_kw", "solar_surplus_kw", "battery_kw", "grid_kw", "battery_pct", "self_powered_pct",
    "threshold_battery_start", "threshold_battery_stop", "threshold_solar_start", "threshold_solar_stop",
    "charger_state", "action", "reason",
    "session_active_minutes", "session_count_today", "session_stop_reason",
    "is_night_blackout", "manual_mode", "island_mode", "storm_mode",
    "est_grid_cost_this_minute", "charge_window_start_hour", "charge_window_end_hour"
]

def get_session_minutes() -> float:
    if state.charge_session_start is None:
        return 0.0
    return round((datetime.now(config.TZ) - state.charge_session_start).total_seconds() / 60, 1)

def log_to_csv(stats: dict, action: str, reason: str, now: datetime):
    tou    = get_tou_period(now)
    rate   = get_tou_rate(now)
    grid   = stats["grid_kw"]

    est_cost = round(max(0, grid) * rate / 60, 6)

    row = [
        now.isoformat(),
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        now.strftime("%A"),
        is_weekend(now),
        tou,
        rate,
        is_expensive_period(now),
        stats["solar_kw"],
        stats["home_kw"],
        stats["solar_surplus_kw"],
        stats["battery_kw"],
        stats["grid_kw"],
        stats["battery_pct"],
        stats["self_powered_pct"],
        config.BATTERY_START_PCT,
        config.BATTERY_STOP_PCT,
        0.0,
        0.0,
        state.charger_state,
        action,
        reason,
        get_session_minutes(),
        state.session_count_today,
        state.session_stop_reason or "",
        is_in_night_blackout(now),
        state.manual_mode,
        stats["island_mode"],
        stats["storm_mode"],
        est_cost,
        config.ALLOWED_CHARGE_START_HOUR,
        config.ALLOWED_CHARGE_END_HOUR,
    ]

    file_exists = os.path.exists(config.CSV_LOG_FILE)
    os.makedirs(os.path.dirname(config.CSV_LOG_FILE) or ".", exist_ok=True)
    with open(config.CSV_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADERS)
        writer.writerow(row)

    try:
        from sheets_db import append_log_row
        append_log_row(row)
    except Exception as e:
        log_csv.error(f"Failed to push row to Google Sheets: {e}")

    if est_cost > 0:
        log_csv.debug(
            f"Grid draw logged | grid={grid}kW | rate=${rate}/kWh | "
            f"est_cost_this_min=${est_cost:.5f} | tou={tou}"
        )
