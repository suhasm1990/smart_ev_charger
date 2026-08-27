"""Mutable runtime state shared across the daemon, bot, and reporting layers."""
from datetime import datetime

from core import config


class State:
    IDLE     = "IDLE"
    CHARGING = "CHARGING"
    WAITING  = "WAITING"


charger_state        = State.IDLE
charge_session_start = None
session_stop_reason  = None
manual_mode          = False
manual_mode_set_at   = None
prev_manual_mode     = False

session_count_today      = 0
grid_draw_count          = 0
consecutive_api_failures = 0

last_grid_export_alert_date = None
last_manual_grid_alert      = None

# Manual-mode guardrails, all optional and cleared on return to auto.
manual_guard_stop_battery_pct = None  # e.g. 30.0
manual_guard_stop_at_hour     = None  # e.g. 16
manual_guard_stop_time        = None  # datetime after which charging must stop
active_amperage               = config.DEFAULT_CHARGER_AMPERAGE


def clear_manual_guards():
    global manual_guard_stop_battery_pct, manual_guard_stop_at_hour
    global manual_guard_stop_time, active_amperage
    manual_guard_stop_battery_pct = None
    manual_guard_stop_at_hour = None
    manual_guard_stop_time = None
    active_amperage = config.DEFAULT_CHARGER_AMPERAGE


def get_session_minutes() -> float:
    """Elapsed minutes of the active charging session, or 0.0 when idle."""
    if charge_session_start is None:
        return 0.0
    return max(0.0, round((datetime.now(config.TZ) - charge_session_start).total_seconds() / 60, 1))


def charger_power_kw(amperage: int = None) -> float:
    """Charger draw in kW at the given amperage (defaults to the active limit)."""
    return round((amperage if amperage is not None else active_amperage) * config.CHARGER_VOLTAGE / 1000.0, 3)
