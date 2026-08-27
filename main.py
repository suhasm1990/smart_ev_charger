"""Smart EV Charger daemon: solar-aware charging control loop."""
import signal
import sys
import threading
import time
from datetime import datetime

import requests
import schedule

from agent import check_alerts, check_recent_log_errors, run_daily_agent, start_telegram_bot
from agent.telegram_bot import send_monthly_telegram_report
from core import (
    check_manual_mode, config, evaluate, get_session_minutes, get_tou_period,
    get_tou_rate, is_in_night_blackout, is_weekend, state,
)
from reporting import (
    log, log_chargepoint, log_decision, log_mode, log_netzero, log_to_csv, notify,
)
from services import (
    ChargePointStartError, get_charger_status, get_powerwall_stats,
    set_charger_amperage_limit, start_charger, stop_charger,
)

CYCLE_TIMEOUT_SECONDS = 45.0
GRID_DRAW_THRESHOLD_KW = 0.1
GRID_ALERT_THRESHOLD_KW = 1.0
API_FAILURE_ALERT_STREAK = 3
SESSION_DRIFT_SECONDS = 3600


def daily_reset():
    log.info(f"DAILY RESET | sessions_today={state.session_count_today} | grid_draw_events={state.grid_draw_count}")
    state.session_count_today = 0
    state.grid_draw_count = 0


def _alert_state(stats: dict, cp_status: dict) -> dict:
    """Flattens live telemetry into the field names custom alert rules use."""
    return {
        "battery_pct":     stats.get("battery_pct"),
        "solar_kw":        stats.get("solar_kw"),
        "home_kw":         stats.get("home_kw"),
        "surplus_kw":      stats.get("solar_surplus_kw"),
        "grid_export_kw":  stats.get("grid_export_kw"),
        "grid_kw":         stats.get("grid_kw"),
        "island_mode":     stats.get("island_mode"),
        "storm_mode":      stats.get("storm_mode"),
        "charging_status": cp_status.get("charging_status"),
        "is_plugged_in":   cp_status.get("is_plugged_in"),
        "is_connected":    cp_status.get("is_connected"),
        "log_errors":      check_recent_log_errors(interval_minutes=config.CHECK_INTERVAL_MINUTES + 5),
    }


def _evaluate_alerts(stats: dict, cp_status: dict):
    try:
        check_alerts(_alert_state(stats, cp_status))
    except Exception as e:
        log.warning(f"Error evaluating custom alerts: {e}")


def _go_idle(reason: str):
    state.charger_state = state.State.IDLE
    state.charge_session_start = None
    state.session_stop_reason = reason


def _sync_with_hardware(cp_status: dict, now: datetime):
    """Reconciles in-memory session state with what the charger is actually doing."""
    if cp_status.get("charging_status") == "CHARGING":
        if state.charger_state != state.State.CHARGING or state.charge_session_start is None:
            log.info("SYNC | Charger is physically charging. Adopting active session.")
            state.charger_state = state.State.CHARGING
            state.charge_session_start = cp_status.get("session_start_time") or now
        else:
            reported = cp_status.get("session_start_time")
            if reported and abs((state.charge_session_start - reported).total_seconds()) > SESSION_DRIFT_SECONDS:
                state.charge_session_start = reported
    elif state.charger_state == state.State.CHARGING or state.charge_session_start is not None:
        log.info("SYNC | Charger is physically idle. Clearing session state.")
        state.charger_state = state.State.IDLE
        state.charge_session_start = None


def _stop_manual_charge(reason: str, message: str, stats: dict, now: datetime):
    """Ends a manual session, restores default amperage, and returns to auto."""
    stop_charger()
    try:
        set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
    except Exception as e:
        log_chargepoint.warning(f"Could not reset amperage to {config.DEFAULT_CHARGER_AMPERAGE}A: {e}")
    _go_idle(reason)
    config.MANUAL_MODE_OVERRIDE = "auto"
    config.save_dynamic_config()
    state.clear_manual_guards()
    notify(message)
    log_to_csv(stats, "stop", reason, now)


def _manual_guard_breach(stats: dict, now: datetime, cp_status: dict) -> tuple[str, str] | None:
    """Returns (reason, notification) for the first tripped manual guardrail."""
    guard_stop_pct = state.manual_guard_stop_battery_pct
    battery = stats["battery_pct"]
    stop_hour = state.manual_guard_stop_at_hour

    checks = [
        (not cp_status.get("is_plugged_in", True),
         "Car was unplugged during manual charge",
         "🔴 <b>Manual Charging Ended</b>\nCar was unplugged. Returned to <b>Auto mode</b>."),
        (stats.get("island_mode") == "off_grid",
         "Powerwall went off-grid during manual charge",
         "🔴 <b>Manual Charging Stopped</b>\nPowerwall went off-grid. Returned to <b>Auto mode</b>."),
        (bool(stats.get("storm_mode")),
         "Storm mode active during manual charge",
         "🔴 <b>Manual Charging Stopped</b>\nStorm Watch active. Returned to <b>Auto mode</b>."),
        (guard_stop_pct is not None and battery < guard_stop_pct,
         f"Manual stop guard triggered (Battery {battery}% < {guard_stop_pct}%)",
         f"🔴 <b>Manual Charging Stopped (Guardrail Triggered)</b>\nPowerwall battery dropped to <b>{battery}%</b>, "
         f"below your <b>{guard_stop_pct}%</b> stop limit.\nReturned to <b>Auto mode</b>."),
        (state.manual_guard_stop_time is not None and now >= state.manual_guard_stop_time,
         "Manual charging duration limit reached",
         "🔴 <b>Manual Charging Stopped (Time Limit Reached)</b>\nTarget charging duration completed.\n"
         "Returned to <b>Auto mode</b>."),
        (stop_hour is not None and now.hour >= stop_hour,
         f"Reached scheduled stop hour ({stop_hour}:00)",
         f"🔴 <b>Manual Charging Stopped (Scheduled Cutoff)</b>\nReached the scheduled stop time "
         f"(<b>{now.strftime('%H:%M')}</b>).\nReturned to <b>Auto mode</b>."),
        # Only fall back to the TOU blackout when no explicit stop hour was set.
        (stop_hour is None and is_in_night_blackout(now) and not is_weekend(now),
         f"Night blackout window ({config.NIGHT_BLACKOUT_START_HOUR}:00)",
         f"🔴 <b>Manual Charging Stopped (TOU Peak Blackout)</b>\nReached the "
         f"{config.NIGHT_BLACKOUT_START_HOUR}:00 blackout window before peak rates start.\n"
         f"Returned to <b>Auto mode</b>."),
    ]
    return next(((reason, message) for tripped, reason, message in checks if tripped), None)


def _run_manual_cycle(stats: dict, cp_status: dict, now: datetime, tou: str):
    log_mode.debug(
        f"Manual mode | battery={stats['battery_pct']}% | solar={stats['solar_kw']}kW | "
        f"grid={stats['grid_kw']}kW | charger={state.charger_state} | tou={tou}"
    )
    _evaluate_alerts(stats, cp_status)

    is_charging = state.charger_state == state.State.CHARGING or cp_status.get("charging_status") == "CHARGING"
    if is_charging:
        breach = _manual_guard_breach(stats, now, cp_status)
        if breach:
            reason, message = breach
            log_decision.info(f"MANUAL GUARD | {reason}")
            _stop_manual_charge(reason, message, stats, now)
            return

    grid_kw = stats["grid_kw"]
    if grid_kw > GRID_DRAW_THRESHOLD_KW:
        log_mode.warning(f"GRID DRAW IN MANUAL MODE | grid={grid_kw}kW | rate=${get_tou_rate(now)}/kWh | tou={tou}")

    # Rate-limit the expensive-draw warning to once per clock hour.
    if grid_kw > GRID_ALERT_THRESHOLD_KW and tou in ("on_peak", "partial_peak"):
        hour_key = now.strftime("%Y-%m-%d-%H")
        if state.last_manual_grid_alert != hour_key:
            notify(
                f"⚠️ <b>High Grid Draw Alert (Manual Mode)</b>\n"
                f"Grid draw is <b>{grid_kw} kW</b> during {tou.upper()} rate (${get_tou_rate(now)}/kWh).\n"
                f"Consider switching to Auto mode or pausing heavy loads."
            )
            state.last_manual_grid_alert = hour_key

    log_to_csv(stats, "manual", "Manual override active — automation paused", now)


def _handle_protective_stop(stats: dict, now: datetime, label: str, reason: str, csv_reason: str) -> bool:
    """Stops charging for off-grid or storm conditions. Returns True if tripped."""
    log.warning(f"{label} | Skipping cycle | battery={stats['battery_pct']}%")
    if state.charger_state == state.State.CHARGING:
        log.warning(f"{label} | Active session detected — stopping charger")
        stop_charger()
        _go_idle(reason)
    log_to_csv(stats, "skipped", csv_reason, now)
    return True


def run_cycle():
    """One evaluation of the world: read telemetry, decide, act, record."""
    config.load_dynamic_config()
    now = datetime.now(config.TZ)

    try:
        stats = get_powerwall_stats()
        tou = get_tou_period(now)

        try:
            cp_status = get_charger_status()
            stats["is_plugged_in"] = cp_status.get("is_plugged_in", False)
            _sync_with_hardware(cp_status, now)
        except Exception as e:
            log_chargepoint.warning(f"Failed to get charger status: {e}")
            cp_status = {}

        if check_manual_mode():
            try:
                _run_manual_cycle(stats, cp_status, now, tou)
            except Exception as e:
                log_mode.warning(f"Manual mode cycle failed: {e}")
            return

        _evaluate_alerts(stats, cp_status)

        if stats["grid_kw"] > GRID_DRAW_THRESHOLD_KW:
            state.grid_draw_count += 1
            log_netzero.warning(
                f"GRID DRAW DETECTED | grid={stats['grid_kw']}kW | solar={stats['solar_kw']}kW | "
                f"battery={stats['battery_pct']}% | tou={tou} | rate=${get_tou_rate(now)}/kWh | "
                f"charger_state={state.charger_state}"
            )

        if stats.get("island_mode") == "off_grid":
            _handle_protective_stop(
                stats, now, "OFF-GRID DETECTED",
                "Powerwall went off-grid — stopping to protect home",
                "Powerwall off-grid — protecting home load")
            return
        if stats.get("storm_mode"):
            _handle_protective_stop(
                stats, now, "STORM MODE ACTIVE",
                "Storm mode activated — stopping to preserve backup reserve",
                "Storm mode active — preserving backup reserve")
            return

        action, reason = evaluate(stats, now)
        log.info(
            f"CYCLE | action={action} | state={state.charger_state} | tou={tou} | "
            f"battery={stats['battery_pct']}% | solar={stats['solar_kw']}kW | "
            f"surplus={stats['solar_surplus_kw']}kW | home={stats['home_kw']}kW | "
            f"grid={stats['grid_kw']}kW | session={get_session_minutes():.0f}min | "
            f"blackout={is_in_night_blackout(now)} | {reason}"
        )

        if action == "start":
            try:
                start_charger(config.DEFAULT_CHARGER_AMPERAGE)
                state.active_amperage = config.DEFAULT_CHARGER_AMPERAGE
                log_chargepoint.info(
                    f"CHARGE STARTED | battery={stats['battery_pct']}% | solar={stats['solar_kw']}kW | "
                    f"amperage={config.DEFAULT_CHARGER_AMPERAGE}A | tou={tou} | reason={reason}"
                )
                notify(
                    f"🟢 Charging started\n{reason}\n"
                    f"Battery: {stats['battery_pct']}% | Solar: {stats['solar_kw']}kW | TOU: {tou}"
                )
            except ChargePointStartError as e:
                _go_idle(str(e))
                log_chargepoint.warning(f"CHARGE START REJECTED | {e}")
                notify(f"⚠️ <b>EV Charging Start Notice</b>\n{e}")
            except Exception:
                _go_idle("Charger start failed")
                raise

        elif action == "stop":
            duration = get_session_minutes()
            try:
                stop_charger()
            except Exception:
                # Leave the state as CHARGING so the next cycle retries the stop.
                state.charger_state = state.State.CHARGING
                raise
            log_chargepoint.info(
                f"CHARGE STOPPED | battery={stats['battery_pct']}% | solar={stats['solar_kw']}kW | "
                f"tou={tou} | reason={reason} | session_duration={duration:.0f}min"
            )
            notify(
                f"🔴 Charging stopped\n{reason}\n"
                f"Battery: {stats['battery_pct']}% | Solar: {stats['solar_kw']}kW | Session: {duration:.0f} min"
            )
            state.charger_state = state.State.IDLE

        state.consecutive_api_failures = 0
        log_to_csv(stats, action, reason, now)
        if state.charger_state == state.State.IDLE:
            state.charge_session_start = None

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "N/A"
        log.error(f"NETZERO API ERROR | status={status} | {e}")
        _note_api_failure("Failed to fetch Powerwall stats")
    except Exception as e:
        log.error(f"CYCLE ERROR | {type(e).__name__}: {e}", exc_info=True)
        _note_api_failure(f"Encountered repeated cycle errors ({type(e).__name__})")


def _note_api_failure(description: str):
    state.consecutive_api_failures += 1
    if state.consecutive_api_failures == API_FAILURE_ALERT_STREAK:
        notify(
            f"⚠️ <b>Smart EV Charger Notice</b>\n{description} for "
            f"{API_FAILURE_ALERT_STREAK} consecutive cycles. Will keep retrying."
        )


_cycle_lock = threading.Lock()


def run_cycle_safe():
    """Runs one cycle, guaranteeing the scheduler is never blocked or re-entered.

    The cycle runs on a throwaway thread so a hung network call cannot wedge the
    scheduler; the lock is held by that thread and released only when it really
    finishes, so a timed-out cycle cannot be overlapped by the next one.
    """
    if not _cycle_lock.acquire(blocking=False):
        log.warning("SKIP CYCLE | Previous cycle is still running.")
        return

    def worker():
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Uncaught exception in run_cycle: {e}", exc_info=True)
        finally:
            _cycle_lock.release()

    thread = threading.Thread(target=worker, daemon=True, name="CycleWorker")
    thread.start()
    thread.join(timeout=CYCLE_TIMEOUT_SECONDS)
    if thread.is_alive():
        log.error(
            f"CYCLE TIMEOUT | run_cycle exceeded {CYCLE_TIMEOUT_SECONDS:.0f}s. "
            f"Returning to the scheduler; the next cycle will be skipped until it finishes."
        )


def _run_in_thread(fn, name: str):
    return lambda: threading.Thread(target=fn, daemon=True, name=name).start()


def check_monthly_schedule():
    """Sends the bill infographic on the first morning of each month."""
    if datetime.now(config.TZ).day != 1:
        return
    log.info("MONTHLY TRIGGER | Sending monthly bill report...")
    try:
        send_monthly_telegram_report(period="last_month")
    except Exception as e:
        log.error(f"Failed to send the monthly report: {e}")


def handle_shutdown(signum, frame):
    log.info("SHUTDOWN | Signal received — leaving charger state untouched")
    try:
        from services import flush
        flush(timeout=5.0)
    except Exception:
        pass
    sys.exit(0)


def adopt_startup_state():
    """Aligns in-memory state with the charger before the first cycle runs."""
    try:
        status = get_charger_status()
        log_chargepoint.info(
            f"STARTUP CHECK | status={status['charging_status']} | plugged_in={status['is_plugged_in']} | "
            f"connected={status['is_connected']} | amperage={status['amperage_limit']}A"
        )
        if status["charging_status"] == "CHARGING":
            log.info("STARTUP SYNC | Adopting active charging session.")
            state.charger_state = state.State.CHARGING
            state.charge_session_start = status.get("session_start_time") or datetime.now(config.TZ)
            state.active_amperage = status.get("amperage_limit") or config.DEFAULT_CHARGER_AMPERAGE
        else:
            state.charger_state = state.State.IDLE
            state.charge_session_start = None
    except Exception as e:
        log_chargepoint.warning(f"STARTUP CHECK FAILED | {e} — will retry on the first cycle")


def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log.info("=" * 70)
    log.info("STARTUP | Smart EV Charger")
    log.info(f"STARTUP | Thresholds: start={config.BATTERY_START_PCT}% | stop={config.BATTERY_STOP_PCT}%")
    log.info(f"STARTUP | Min session: {config.MIN_CHARGE_MINUTES}min | Interval: {config.CHECK_INTERVAL_MINUTES}min")
    log.info(f"STARTUP | Amperage: default={config.DEFAULT_CHARGER_AMPERAGE}A max={config.MAX_CHARGER_AMPERAGE}A")
    log.info(f"STARTUP | CSV log: {config.CSV_LOG_FILE} | Text log: {config.TEXT_LOG_FILE}")
    log.info("=" * 70)

    adopt_startup_state()

    tz_name = getattr(config.TZ, "key", str(config.TZ))
    schedule.every().day.at(config.DAILY_RESET_TIME, tz_name).do(daily_reset)
    schedule.every().day.at(config.DAILY_AGENT_TIME, tz_name).do(_run_in_thread(run_daily_agent, "DailyAgent"))
    schedule.every().day.at("07:00", tz_name).do(_run_in_thread(check_monthly_schedule, "MonthlyReport"))
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_cycle_safe)

    if config.TELEGRAM_BOT_TOKEN:
        start_telegram_bot(run_cycle_safe)

    run_cycle_safe()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
