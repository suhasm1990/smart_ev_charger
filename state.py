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
