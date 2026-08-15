from datetime import datetime
import config
import state
from logger import log_decision
from tou import is_in_night_blackout, is_weekend
from csv_logger import get_session_minutes

def is_in_charge_window(current_hour: int, start_hr: int, end_hr: int) -> bool:
    """Checks whether current_hour falls inside the configured charging window (supports wrapping midnight)."""
    if start_hr == 0 and end_hr == 24:
        return True
    if start_hr <= end_hr:
        return start_hr <= current_hour < end_hr
    return current_hour >= start_hr or current_hour < end_hr

def min_charge_time_met() -> bool:
    if state.charge_session_start is None:
        return True
    return get_session_minutes() >= config.MIN_CHARGE_MINUTES

def evaluate(stats: dict, now: datetime) -> tuple[str, str]:
    battery_pct   = stats["battery_pct"]
    is_plugged_in = stats.get("is_plugged_in", True)

    # ── 1. Safety Stops (Always evaluated first) ─────────────────────────────
    if not is_plugged_in:
        if state.charger_state == state.State.CHARGING:
            state.charger_state       = state.State.IDLE
            state.session_stop_reason = "Car was unplugged"
            log_decision.info(f"STOP | Unplugged | battery={battery_pct}%")
            return "stop", state.session_stop_reason
        return "hold", "Car is unplugged"

    if is_in_night_blackout(now) and not is_weekend(now):
        if state.charger_state == state.State.CHARGING:
            state.charger_state       = state.State.IDLE
            state.session_stop_reason = f"Night blackout window ({config.NIGHT_BLACKOUT_START_HOUR}PM–{config.NIGHT_BLACKOUT_END_HOUR}AM)"
            log_decision.info(f"STOP | Night blackout | battery={battery_pct}%")
            return "stop", state.session_stop_reason
        return "blackout", f"Night blackout window — no charging until {config.NIGHT_BLACKOUT_END_HOUR}:00 AM"

    low_reserve = getattr(config, "BATTERY_LOW_RESERVE_PCT", 15.0)
    if battery_pct < low_reserve:
        if state.charger_state == state.State.CHARGING:
            state.charger_state       = state.State.IDLE
            state.session_stop_reason = f"Critical low battery reserve ({battery_pct}% < {low_reserve}%)"
            log_decision.info(f"STOP | Battery critical low reserve | {battery_pct}%")
            return "stop", state.session_stop_reason
        return "hold", f"Battery reserve low ({battery_pct}% < {low_reserve}% reserve limit)"

    if battery_pct < config.BATTERY_STOP_PCT:
        if state.charger_state == state.State.CHARGING:
            if min_charge_time_met():
                state.charger_state       = state.State.IDLE
                state.session_stop_reason = f"Battery {battery_pct}% < {config.BATTERY_STOP_PCT}% safe limit"
                log_decision.info(f"STOP | Battery low | {battery_pct}% < {config.BATTERY_STOP_PCT}%")
                return "stop", state.session_stop_reason
            return "hold", f"Battery low ({battery_pct}%), but min {config.MIN_CHARGE_MINUTES}min session not met"
        return "hold", f"Battery {battery_pct}% < {config.BATTERY_STOP_PCT}% (need {config.BATTERY_START_PCT}% to restart)"

    # ── 1.5. Dynamic Time Window ─────────────────────────────────────────────
    start_hr = config.ALLOWED_CHARGE_START_HOUR
    end_hr = config.ALLOWED_CHARGE_END_HOUR
    if not is_in_charge_window(now.hour, start_hr, end_hr):
        if state.charger_state == state.State.CHARGING:
            if min_charge_time_met():
                state.charger_state       = state.State.IDLE
                state.session_stop_reason = f"Outside AI charge window ({start_hr}:00 - {end_hr}:00)"
                log_decision.info(f"STOP | Outside AI window | {start_hr}:00 - {end_hr}:00")
                return "stop", state.session_stop_reason
            return "hold", "Outside window, but min session not met"
        return "hold", f"Outside AI charge window ({start_hr}:00 - {end_hr}:00)"

    # ── 2. Core Decision Logic ────────────────────────────────────────────────
    if battery_pct >= config.BATTERY_START_PCT:
        reason = f"Battery healthy ({battery_pct}% >= {config.BATTERY_START_PCT}%)"
        if state.charger_state == state.State.CHARGING:
            log_decision.debug(f"CHARGING | {reason}")
            return "hold", reason
        # Start new session
        state.charger_state        = state.State.CHARGING
        state.charge_session_start = now
        state.session_count_today += 1
        state.session_stop_reason  = None
        log_decision.info(f"START | {reason}")
        return "start", reason

    if state.charger_state == state.State.CHARGING:
        reason = f"Continuing charge (Battery {battery_pct}% > {config.BATTERY_STOP_PCT}%)"
        log_decision.debug(f"CHARGING | {reason}")
        return "hold", reason

    # Idle waiting for battery to reach START_PCT
    return "hold", f"Idle (Battery {battery_pct}% < {config.BATTERY_START_PCT}%)"
