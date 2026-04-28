from datetime import datetime
import config
import state
from logger import log_decision
from tou import is_in_night_blackout, is_weekend
from csv_logger import get_session_minutes

def min_charge_time_met() -> bool:
    if state.charge_session_start is None:
        return True
    return get_session_minutes() >= config.MIN_CHARGE_MINUTES

def evaluate(stats: dict, now: datetime) -> tuple[str, str]:
    battery_pct   = stats["battery_pct"]
    solar_kw      = stats["solar_kw"]
    home_kw       = stats["home_kw"]
    is_plugged_in = stats.get("is_plugged_in", True)
    surplus_kw    = solar_kw - home_kw

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

    if battery_pct < config.BATTERY_STOP_PCT:
        if state.charger_state == state.State.CHARGING:
            if min_charge_time_met():
                state.charger_state       = state.State.IDLE
                state.session_stop_reason = f"Battery {battery_pct}% < {config.BATTERY_STOP_PCT}% safe limit"
                log_decision.info(f"STOP | Battery low | {battery_pct}% < {config.BATTERY_STOP_PCT}%")
                return "stop", state.session_stop_reason
            return "hold", f"Battery low ({battery_pct}%), but min {config.MIN_CHARGE_MINUTES}min session not met"
        return "hold", f"Battery {battery_pct}% < {config.BATTERY_STOP_PCT}% (need {config.BATTERY_START_PCT}% to restart)"

    # ── 2. Core Logic: Should we charge? ─────────────────────────────────────
    
    should_charge = False
    reason = ""

    # Rule A: Start/Keep charging if Battery is healthy
    if battery_pct >= config.BATTERY_START_PCT:
        should_charge = True
        reason = f"Battery healthy ({battery_pct}% >= {config.BATTERY_START_PCT}%)"
    
    # Rule B: Hysteresis - If already charging, keep charging until we hit STOP_PCT (Handled in Step 1)
    elif state.charger_state == state.State.CHARGING:
        should_charge = True
        reason = f"Continuing charge (Battery {battery_pct}% > {config.BATTERY_STOP_PCT}%)"
    
    else:
        # Not charging and battery < START_PCT. Wait.
        reason = f"Idle (Battery {battery_pct}% < {config.BATTERY_START_PCT}%)"

    # ── 3. Apply Decision to State Machine ───────────────────────────────────
    
    if state.charger_state == state.State.CHARGING:
        if not should_charge:
            if min_charge_time_met():
                state.charger_state       = state.State.IDLE
                state.session_stop_reason = reason
                log_decision.info(f"STOP | {reason}")
                return "stop", state.session_stop_reason
            return "hold", f"Want to stop ({reason}), but min session not met"
            
        log_decision.debug(f"CHARGING | {reason}")
        return "hold", reason

    elif state.charger_state in [state.State.IDLE, state.State.WAITING]:
        if should_charge:
            state.charger_state        = state.State.CHARGING
            state.charge_session_start = now
            state.session_count_today += 1
            state.session_stop_reason  = None
            log_decision.info(f"START | {reason}")
            return "start", reason
            
        return "hold", reason

    return "hold", "No condition matched"
