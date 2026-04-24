from datetime import datetime
import config
import state
from logger import log_decision
from tou import get_tou_period, is_expensive_period, is_in_night_blackout, is_morning_window, is_weekend
from csv_logger import get_session_minutes

def min_charge_time_met() -> bool:
    if state.charge_session_start is None:
        return True
    return get_session_minutes() >= config.MIN_CHARGE_MINUTES

def should_charge_during_peak(solar_kw: float, home_kw: float, battery_pct: float) -> tuple[bool, str]:
    surplus = solar_kw - home_kw
    if surplus >= config.PEAK_MIN_SOLAR_SURPLUS_KW:
        return True, f"Solar surplus {surplus:.2f}kW >= {config.PEAK_MIN_SOLAR_SURPLUS_KW}kW threshold"
    if surplus >= config.PEAK_BATTERY_COVER_SURPLUS_KW and battery_pct >= config.PEAK_BATTERY_MIN_PCT:
        gap = round(config.CAR_CHARGE_KW - surplus, 2)
        return True, f"Battery {battery_pct}% covering {gap}kW gap (surplus={surplus:.2f}kW)"
    return False, (
        f"Solar surplus {surplus:.2f}kW too low "
        f"(need {config.PEAK_MIN_SOLAR_SURPLUS_KW}kW or {config.PEAK_BATTERY_COVER_SURPLUS_KW}kW+battery) — grid risk"
    )

def evaluate(stats: dict, now: datetime) -> tuple[str, str]:
    battery_pct  = stats["battery_pct"]
    solar_kw     = stats["solar_kw"]
    home_kw      = stats["home_kw"]
    tou          = get_tou_period(now)
    expensive    = is_expensive_period(now)
    weekend      = is_weekend(now)
    morning      = is_morning_window(now)
    in_blackout  = is_in_night_blackout(now)

    # ── Night blackout: stop any active session and refuse to start ───────────
    if in_blackout:
        if state.charger_state == state.State.CHARGING:
            state.charger_state       = state.State.IDLE
            state.session_stop_reason = f"Night blackout window ({config.NIGHT_BLACKOUT_START_HOUR}PM–{config.NIGHT_BLACKOUT_END_HOUR}AM)"
            log_decision.info(
                f"STOP | Night blackout | battery={battery_pct}% | solar={solar_kw}kW"
            )
            return "stop", state.session_stop_reason
        return "blackout", f"Night blackout window — no charging until {config.NIGHT_BLACKOUT_END_HOUR}:00 AM"

    # ── Hard stop: battery too low ────────────────────────────────────────────
    if battery_pct < config.BATTERY_STOP_PCT and state.charger_state == state.State.CHARGING:
        if min_charge_time_met():
            state.charger_state       = state.State.WAITING
            state.session_stop_reason = f"Battery {battery_pct}% < {config.BATTERY_STOP_PCT}% threshold"
            log_decision.warning(
                f"STOP→WAITING | Battery low | {battery_pct}% < {config.BATTERY_STOP_PCT}% | "
                f"solar={solar_kw}kW | grid={stats['grid_kw']}kW"
            )
            return "stop", state.session_stop_reason
        return "hold", f"Battery low ({battery_pct}%) but min {config.MIN_CHARGE_MINUTES}min session not met yet"

    # ── Hard stop: solar too low ──────────────────────────────────────────────
    if solar_kw < config.SOLAR_STOP_KW and state.charger_state == state.State.CHARGING:
        if min_charge_time_met():
            state.charger_state       = state.State.IDLE
            state.session_stop_reason = f"Solar {solar_kw}kW < {config.SOLAR_STOP_KW}kW threshold"
            log_decision.info(
                f"STOP | Solar low | {solar_kw}kW < {config.SOLAR_STOP_KW}kW | battery={battery_pct}%"
            )
            return "stop", state.session_stop_reason

    # ── WAITING: hold until Powerwall has recovered ───────────────────────────
    if state.charger_state == state.State.WAITING:
        if battery_pct >= config.BATTERY_RESUME_PCT and solar_kw >= config.SOLAR_START_KW:
            if expensive:
                ok, reason = should_charge_during_peak(solar_kw, home_kw, battery_pct)
                if ok:
                    state.charger_state        = state.State.CHARGING
                    state.charge_session_start = now
                    state.session_count_today += 1
                    state.session_stop_reason  = None
                    log_decision.info(f"START | Battery recovered to {battery_pct}% | {reason}")
                    return "start", f"Battery recovered to {battery_pct}%. {reason}"
                log_decision.debug(f"WAIT | Peak guard | {reason}")
                return "hold", reason
            state.charger_state        = state.State.CHARGING
            state.charge_session_start = now
            state.session_count_today += 1
            state.session_stop_reason  = None
            log_decision.info(f"START | Battery recovered | {battery_pct}% | solar={solar_kw}kW")
            return "start", f"Battery recovered to {battery_pct}% — resuming off-peak"
        log_decision.debug(
            f"WAITING | battery={battery_pct}% (need {config.BATTERY_RESUME_PCT}%) | solar={solar_kw}kW"
        )
        return "hold", (
            f"Waiting for recovery: battery={battery_pct}% (need {config.BATTERY_RESUME_PCT}%), "
            f"solar={solar_kw}kW"
        )

    # ── CHARGING: check if we should stop ────────────────────────────────────
    if state.charger_state == state.State.CHARGING:
        if expensive:
            ok, reason = should_charge_during_peak(solar_kw, home_kw, battery_pct)
            if not ok and min_charge_time_met():
                state.charger_state       = state.State.IDLE
                state.session_stop_reason = f"Peak guard triggered: {reason}"
                log_decision.warning(
                    f"STOP | Peak guard | {reason} | battery={battery_pct}% | grid={stats['grid_kw']}kW"
                )
                return "stop", state.session_stop_reason
        log_decision.debug(
            f"CHARGING | battery={battery_pct}% | solar={solar_kw}kW | "
            f"surplus={stats['solar_surplus_kw']}kW | tou={tou} | "
            f"session={get_session_minutes():.0f}min"
        )
        return "hold", (
            f"Charging | battery={battery_pct}% | solar={solar_kw}kW | "
            f"surplus={stats['solar_surplus_kw']}kW | tou={tou}"
        )

    # ── IDLE: check if we should start ───────────────────────────────────────
    if state.charger_state == state.State.IDLE:

        # Weekend — relaxed thresholds, no TOU risk
        if weekend:
            if battery_pct >= config.WEEKEND_BATTERY_START_PCT and solar_kw >= config.SOLAR_START_KW:
                state.charger_state        = state.State.CHARGING
                state.charge_session_start = now
                state.session_count_today += 1
                state.session_stop_reason  = None
                log_decision.info(
                    f"START | Weekend | battery={battery_pct}% | solar={solar_kw}kW"
                )
                return "start", f"Weekend: battery={battery_pct}%, solar={solar_kw}kW"
            return "hold", (
                f"Weekend idle: battery={battery_pct}% (need {config.WEEKEND_BATTERY_START_PCT}%), "
                f"solar={solar_kw}kW (need {config.SOLAR_START_KW}kW)"
            )

        # Peak/partial-peak — only start if solar surplus safe
        if expensive:
            if battery_pct >= config.BATTERY_START_PCT:
                ok, reason = should_charge_during_peak(solar_kw, home_kw, battery_pct)
                if ok:
                    state.charger_state        = state.State.CHARGING
                    state.charge_session_start = now
                    state.session_count_today += 1
                    state.session_stop_reason  = None
                    log_decision.info(f"START | Peak-safe | {reason}")
                    return "start", reason
            return "hold", (
                f"Peak idle: battery={battery_pct}% | solar surplus={stats['solar_surplus_kw']}kW | "
                f"not starting — grid risk"
            )

        # Morning window (weekday 6 AM–1 PM, off-peak)
        if morning:
            if battery_pct >= config.BATTERY_START_PCT and solar_kw >= config.SOLAR_START_KW:
                state.charger_state        = state.State.CHARGING
                state.charge_session_start = now
                state.session_count_today += 1
                state.session_stop_reason  = None
                log_decision.info(
                    f"START | Morning window | battery={battery_pct}% | solar={solar_kw}kW"
                )
                return "start", f"Morning window: battery={battery_pct}%, solar={solar_kw}kW"
            if battery_pct >= 80 and now.hour < 9:
                state.charger_state        = state.State.CHARGING
                state.charge_session_start = now
                state.session_count_today += 1
                state.session_stop_reason  = None
                log_decision.info(
                    f"START | Early morning PW pre-charge | battery={battery_pct}%"
                )
                return "start", f"Early morning: PW at {battery_pct}% — pre-charging before solar"

        # Standard off-peak
        if battery_pct >= config.BATTERY_START_PCT and solar_kw >= config.SOLAR_START_KW:
            state.charger_state        = state.State.CHARGING
            state.charge_session_start = now
            state.session_count_today += 1
            state.session_stop_reason  = None
            log_decision.info(
                f"START | Off-peak | battery={battery_pct}% | solar={solar_kw}kW"
            )
            return "start", f"Off-peak: battery={battery_pct}%, solar={solar_kw}kW"

        return "hold", (
            f"Idle: battery={battery_pct}% (need {config.BATTERY_START_PCT}%), "
            f"solar={solar_kw}kW (need {config.SOLAR_START_KW}kW), tou={tou}"
        )

    return "hold", "No condition matched"
