class State:
    IDLE     = "IDLE"
    CHARGING = "CHARGING"
    WAITING  = "WAITING"

charger_state        = State.IDLE
charge_session_start = None
session_stop_reason  = None
active_session       = None
cp_client            = None
manual_mode          = False
manual_mode_set_at   = None
prev_manual_mode     = False

session_count_today  = 0
grid_draw_count      = 0

last_surplus_alert_date = None
last_grid_export_alert_date = None
consecutive_api_failures = 0
last_api_failure_alert_time = None

# Manual Mode Guardrails
manual_guard_stop_battery_pct = None  # e.g. 30.0
manual_guard_stop_at_hour = None      # e.g. 16
manual_guard_stop_time = None         # datetime when charge must stop

def clear_manual_guards():
    global manual_guard_stop_battery_pct, manual_guard_stop_at_hour, manual_guard_stop_time
    manual_guard_stop_battery_pct = None
    manual_guard_stop_at_hour = None
    manual_guard_stop_time = None

def get_session_minutes() -> float:
    if charge_session_start is None:
        return 0.0
    from datetime import datetime
    from core import config
    return max(0.0, round((datetime.now(config.TZ) - charge_session_start).total_seconds() / 60, 1))

