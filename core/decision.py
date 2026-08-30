"""Pure charging decision rules evaluated once per cycle.

evaluate() only reads state and returns (action, reason); the caller applies
state transitions after the hardware confirms the action succeeded.
"""
from datetime import datetime

from core import config
from core.state import StateSnapshot, state
from core.tou import is_in_night_blackout, is_weekend
from reporting.logger import log_decision


def is_in_charge_window(hour: int, start_hr: int, end_hr: int) -> bool:
    """Whether `hour` falls inside the allowed window, which may wrap midnight."""
    if start_hr == 0 and end_hr == 24:
        return True
    if start_hr <= end_hr:
        return start_hr <= hour < end_hr
    return hour >= start_hr or hour < end_hr


def min_charge_time_met(snap: StateSnapshot) -> bool:
    """Guards against short-cycling the charger on transient dips."""
    if snap.charge_session_start is None:
        return True
    minutes = (datetime.now(config.TZ) - snap.charge_session_start).total_seconds() / 60
    return minutes >= config.MIN_CHARGE_MINUTES


def _stop(reason: str, log_label: str) -> tuple[str, str]:
    log_decision.info(f"STOP | {log_label}")
    return "stop", reason


def evaluate(stats: dict, now: datetime) -> tuple[str, str]:
    """Returns the action ('start', 'stop', 'hold', 'blackout') and its reason."""
    battery_pct = stats["battery_pct"]
    # One consistent view per decision: a concurrent bot command must not be
    # observed half-applied partway through the rule chain.
    snap = state.snapshot()
    cfg = config.snapshot()
    charging = snap.charger_state == state.State.CHARGING

    # ── 1. Safety stops, always evaluated first ─────────────────────────────
    if not stats.get("is_plugged_in", True):
        if charging:
            return _stop("Car was unplugged", f"Unplugged | battery={battery_pct}%")
        return "hold", "Car is unplugged"

    if is_in_night_blackout(now) and not is_weekend(now):
        reason = f"Night blackout window ({cfg.NIGHT_BLACKOUT_START_HOUR}:00–{cfg.NIGHT_BLACKOUT_END_HOUR}:00)"
        if charging:
            return _stop(reason, f"Night blackout | battery={battery_pct}%")
        return "blackout", f"{reason} — no charging until {cfg.NIGHT_BLACKOUT_END_HOUR}:00"

    low_reserve = cfg.BATTERY_LOW_RESERVE_PCT
    if battery_pct < low_reserve:
        reason = f"Critical low battery reserve ({battery_pct}% < {low_reserve}%)"
        if charging:
            return _stop(reason, f"Battery critical low reserve | {battery_pct}%")
        return "hold", f"Battery reserve low ({battery_pct}% < {low_reserve}% reserve limit)"

    if battery_pct < cfg.BATTERY_STOP_PCT:
        if charging:
            if not min_charge_time_met(snap):
                return "hold", f"Battery low ({battery_pct}%), but min {config.MIN_CHARGE_MINUTES}min session not met"
            return _stop(
                f"Battery {battery_pct}% < {cfg.BATTERY_STOP_PCT}% safe limit",
                f"Battery low | {battery_pct}% < {cfg.BATTERY_STOP_PCT}%",
            )
        return "hold", f"Battery {battery_pct}% < {cfg.BATTERY_STOP_PCT}% (need {cfg.BATTERY_START_PCT}% to restart)"

    # ── 2. Charge window set by the morning planner ─────────────────────────
    start_hr, end_hr = cfg.ALLOWED_CHARGE_START_HOUR, cfg.ALLOWED_CHARGE_END_HOUR
    if not is_in_charge_window(now.hour, start_hr, end_hr):
        window = f"AI charge window ({start_hr}:00 - {end_hr}:00)"
        if charging:
            if not min_charge_time_met(snap):
                return "hold", "Outside window, but min session not met"
            return _stop(f"Outside {window}", f"Outside AI window | {start_hr}:00 - {end_hr}:00")
        return "hold", f"Outside {window}"

    # ── 3. Core solar/battery logic ─────────────────────────────────────────
    if battery_pct >= cfg.BATTERY_START_PCT:
        reason = f"Battery healthy ({battery_pct}% >= {cfg.BATTERY_START_PCT}%)"
        if charging:
            return "hold", reason
        log_decision.info(f"START | {reason}")
        return "start", reason

    if charging:
        return "hold", f"Continuing charge (Battery {battery_pct}% > {cfg.BATTERY_STOP_PCT}%)"

    return "hold", f"Idle (Battery {battery_pct}% < {cfg.BATTERY_START_PCT}%)"
