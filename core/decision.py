"""Pure charging decision rules evaluated once per cycle.

evaluate() only reads state and returns (action, reason); the caller applies
state transitions after the hardware confirms the action succeeded.
"""
from datetime import datetime

from core import config
from core.state import StateSnapshot, state
from core.tou import is_cheapest_period, is_in_night_blackout, is_weekend
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

    # ── 2. Optional charge window (the morning planner resets it to 0-24) ───
    start_hr, end_hr = cfg.ALLOWED_CHARGE_START_HOUR, cfg.ALLOWED_CHARGE_END_HOUR
    if not is_in_charge_window(now.hour, start_hr, end_hr):
        window = f"AI charge window ({start_hr}:00 - {end_hr}:00)"
        if charging:
            if not min_charge_time_met(snap):
                return "hold", "Outside window, but min session not met"
            return _stop(f"Outside {window}", f"Outside AI window | {start_hr}:00 - {end_hr}:00")
        return "hold", f"Outside {window}"

    # ── 3. Live solar, or a miles goal at the cheapest rate ─────────────────
    # Everything here runs on this cycle's readings: the Powerwall % (already
    # above the stop threshold) and the solar left over after the house.
    solar_kw = stats.get("solar_kw", 0.0)
    surplus_kw = stats.get("solar_surplus_kw", 0.0)
    goal_kwh = stats.get("goal_night_kwh", 0.0)
    goal_night = goal_kwh > 0 and is_cheapest_period(now)

    if charging:
        if goal_night:
            return "hold", f"Miles goal: charging at the cheapest rate ({goal_kwh:.1f} kWh needed beyond solar)"
        if solar_kw >= config.SOLAR_MIN_KW:
            return "hold", f"Solar charging (solar {solar_kw} kW, battery {battery_pct}%)"
        if not min_charge_time_met(snap):
            return "hold", "Solar has faded, but min session not met"
        return _stop(f"Solar has faded ({solar_kw} kW) and no miles goal needs night charging",
                     f"Solar faded | solar={solar_kw}kW | battery={battery_pct}%")

    if goal_night:
        reason = f"Miles goal needs {goal_kwh:.1f} kWh beyond expected solar; charging at the cheapest rate"
    elif surplus_kw >= state.charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE):
        reason = f"Solar surplus {surplus_kw} kW covers the charger"
    elif battery_pct >= cfg.BATTERY_START_PCT and surplus_kw >= config.SOLAR_START_SURPLUS_KW:
        reason = f"Battery healthy ({battery_pct}% >= {cfg.BATTERY_START_PCT}%) with {surplus_kw} kW solar surplus"
    else:
        return "hold", (f"Idle (battery {battery_pct}%, solar surplus {surplus_kw} kW; starts with "
                        f">= {config.SOLAR_START_SURPLUS_KW} kW surplus and battery >= {cfg.BATTERY_START_PCT}%)")
    log_decision.info(f"START | {reason}")
    return "start", reason
