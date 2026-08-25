import schedule
import time
import requests
import signal
import sys
import threading
import concurrent.futures
from datetime import datetime

from core import config, state, check_manual_mode, evaluate, get_tou_period, get_tou_rate, is_in_night_blackout, is_weekend
from reporting import log, log_mode, log_netzero, log_chargepoint, log_decision, log_to_csv, get_session_minutes, notify
from services import get_powerwall_stats, start_charger, stop_charger, get_charger_status, set_charger_amperage_limit, ChargePointStartError
from agent import check_alerts, check_recent_log_errors, run_daily_agent, start_telegram_bot
from agent.telegram_bot import send_monthly_telegram_report

def daily_reset():
    log.info(
        f"DAILY RESET | sessions_today={state.session_count_today} | "
        f"grid_draw_events={state.grid_draw_count}"
    )
    state.session_count_today = 0
    state.grid_draw_count     = 0

cycle_lock = threading.Lock()
_cycle_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="CycleWorker")

def _stop_manual_charge(reason: str, notify_msg: str, stats: dict, now: datetime):
    stop_charger()
    try:
        set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
    except Exception as err:
        log_chargepoint.warning(f"Could not reset amperage to default {config.DEFAULT_CHARGER_AMPERAGE}A: {err}")
    state.charger_state = state.State.IDLE
    state.charge_session_start = None
    state.session_stop_reason = reason
    config.MANUAL_MODE_OVERRIDE = "auto"
    config.save_dynamic_config()
    state.clear_manual_guards()
    notify(notify_msg)
    log_to_csv(stats, "stop", reason, now)

def run_cycle_safe():
    acquired = cycle_lock.acquire(blocking=False)
    if not acquired:
        log.warning("SKIP CYCLE | Another cycle is already running or holding lock.")
        return
    try:
        future = _cycle_executor.submit(run_cycle)
        future.result(timeout=45.0)
    except concurrent.futures.TimeoutError:
        log.error("CYCLE TIMEOUT | run_cycle exceeded 45 seconds. Terminating cycle execution to protect scheduler.")
    except Exception as e:
        log.error(f"Uncaught exception in run_cycle: {e}", exc_info=True)
    finally:
        try:
            cycle_lock.release()
        except RuntimeError:
            pass

def run_cycle():
    config.load_dynamic_config()
    now = datetime.now(config.TZ)

    try:
        stats = get_powerwall_stats()
        battery_pct = stats["battery_pct"]
        tou = get_tou_period(now)

        try:
            cp_status = get_charger_status()
            stats["is_plugged_in"] = cp_status.get("is_plugged_in", False)
            
            # Sync internal state with physical reality (applies to both Auto and Manual modes)
            physical_charging = (cp_status.get("charging_status") == "CHARGING")
            internal_charging = (state.charger_state == state.State.CHARGING)
            
            if physical_charging:
                if not internal_charging or state.charge_session_start is None:
                    log.info("SYNC | Charger is physically charging. Synchronizing active charging state.")
                    state.charger_state = state.State.CHARGING
                    state.charge_session_start = cp_status.get("session_start_time") or now
                elif cp_status.get("session_start_time") and abs((state.charge_session_start - cp_status["session_start_time"]).total_seconds()) > 3600:
                    state.charge_session_start = cp_status["session_start_time"]
            else:
                if internal_charging or state.charge_session_start is not None:
                    log.info("SYNC | Charger is physically NOT charging. Synchronizing idle state.")
                    state.charger_state = state.State.IDLE
                    state.charge_session_start = None
                
        except Exception as e:
            log_chargepoint.warning(f"Failed to get charger status: {e}")
            cp_status = {}

        # Check manual override first — evaluate safety guardrails and log synchronized stats
        if check_manual_mode():
            try:
                log_mode.debug(
                    f"Manual mode | battery={stats['battery_pct']}% | "
                    f"solar={stats['solar_kw']}kW | grid={stats['grid_kw']}kW | "
                    f"charger={state.charger_state} | tou={tou}"
                )

                # Check custom alerts during manual mode
                try:
                    current_state = {
                        "battery_pct": stats.get("battery_pct"),
                        "solar_kw": stats.get("solar_kw"),
                        "home_kw": stats.get("home_kw"),
                        "surplus_kw": stats.get("solar_surplus_kw"),
                        "grid_export_kw": stats.get("grid_export_kw"),
                        "grid_kw": stats.get("grid_kw"),
                        "island_mode": stats.get("island_mode"),
                        "storm_mode": stats.get("storm_mode"),
                        "charging_status": cp_status.get("charging_status"),
                        "is_plugged_in": cp_status.get("is_plugged_in"),
                        "is_connected": cp_status.get("is_connected"),
                        "log_errors": check_recent_log_errors(interval_minutes=config.CHECK_INTERVAL_MINUTES + 5)
                    }
                    check_alerts(current_state)
                except Exception as alert_err:
                    log.warning(f"Error evaluating custom alerts in manual mode: {alert_err}")

                # ── Guardrails for Active Manual Charging ───────────────────────
                if state.charger_state == state.State.CHARGING or cp_status.get("charging_status") == "CHARGING":
                    if not cp_status.get("is_plugged_in", True):
                        log_chargepoint.warning("MANUAL GUARD | Car was unplugged — stopping charger and reverting to auto")
                        _stop_manual_charge(
                            "Car was unplugged during manual charge",
                            "🔴 <b>Manual Charging Ended</b>\nCar was unplugged. Returned to <b>Auto mode</b>.",
                            stats, now
                        )
                        return

                    if stats.get("island_mode") == "off_grid":
                        log.warning("OFF-GRID | Stopping manual charge to protect home load")
                        _stop_manual_charge(
                            "Powerwall went off-grid during manual charge",
                            "🔴 <b>Manual Charging Stopped</b>\nPowerwall went off-grid. Returned to <b>Auto mode</b>.",
                            stats, now
                        )
                        return

                    if stats.get("storm_mode"):
                        log.warning("STORM MODE | Stopping manual charge to preserve backup reserve")
                        _stop_manual_charge(
                            "Storm mode active during manual charge",
                            "🔴 <b>Manual Charging Stopped</b>\nStorm Watch active. Returned to <b>Auto mode</b>.",
                            stats, now
                        )
                        return

                    # 1. Battery Stop Guard (ONLY if user explicitly configured a manual stop guardrail)
                    if state.manual_guard_stop_battery_pct is not None:
                        if stats["battery_pct"] < state.manual_guard_stop_battery_pct:
                            log_decision.info(f"MANUAL GUARD | Battery {stats['battery_pct']}% < {state.manual_guard_stop_battery_pct}% limit — stopping charger")
                            _stop_manual_charge(
                                f"Manual stop guard triggered (Battery {stats['battery_pct']}% < {state.manual_guard_stop_battery_pct}%)",
                                f"🔴 <b>Manual Charging Stopped (Guardrail Triggered)</b>\nPowerwall battery dropped to <b>{stats['battery_pct']}%</b> (below your <b>{state.manual_guard_stop_battery_pct}%</b> stop limit).\nManual charge ended and returned to <b>Auto mode</b>.",
                                stats, now
                            )
                            return

                    # 2. Time Duration Cutoff Guard
                    if state.manual_guard_stop_time and now >= state.manual_guard_stop_time:
                        log_decision.info("MANUAL GUARD | Duration cutoff reached — stopping charger")
                        _stop_manual_charge(
                            "Manual charging duration limit reached",
                            "🔴 <b>Manual Charging Stopped (Time Limit Reached)</b>\nTarget charging duration completed.\nManual charge ended and returned to <b>Auto mode</b>.",
                            stats, now
                        )
                        return

                    # 3. Scheduled Hour / Night Blackout Cutoff Guard
                    stop_hr = state.manual_guard_stop_at_hour
                    if stop_hr is not None:
                        if now.hour >= stop_hr:
                            log_decision.info(f"MANUAL GUARD | Reached stop hour {stop_hr}:00 — stopping charger")
                            _stop_manual_charge(
                                f"Reached scheduled stop hour ({stop_hr}:00)",
                                f"🔴 <b>Manual Charging Stopped (Scheduled Cutoff)</b>\nReached scheduled stop time (<b>{now.strftime('%H:%M')}</b> >= <b>{stop_hr}:00</b>).\nManual charge ended and returned to <b>Auto mode</b>.",
                                stats, now
                            )
                            return
                    elif is_in_night_blackout(now) and not is_weekend(now):
                        log_decision.info(f"MANUAL GUARD | Reached night blackout window ({config.NIGHT_BLACKOUT_START_HOUR}:00) — stopping charger")
                        _stop_manual_charge(
                            f"Night blackout window ({config.NIGHT_BLACKOUT_START_HOUR}:00)",
                            f"🔴 <b>Manual Charging Stopped (TOU Peak Blackout)</b>\nReached {config.NIGHT_BLACKOUT_START_HOUR}:00 blackout window before peak rates start.\nManual charge ended and returned to <b>Auto mode</b>.",
                            stats, now
                        )
                        return

                if stats["grid_kw"] > 0.1:
                    log_mode.warning(
                        f"GRID DRAW IN MANUAL MODE | grid={stats['grid_kw']}kW | "
                        f"rate=${get_tou_rate(now)}/kWh | tou={tou}"
                    )
                if stats["grid_kw"] > 1.0 and tou in ["on_peak", "partial_peak"]:
                    rate = get_tou_rate(now)
                    hour_key = now.strftime("%Y-%m-%d-%H")
                    if getattr(state, "last_manual_grid_alert", None) != hour_key:
                        notify(
                            f"⚠️ <b>High Grid Draw Alert (Manual Mode)</b>\n"
                            f"Grid draw is <b>{stats['grid_kw']} kW</b> during {tou.upper()} rate (${rate}/kWh).\n"
                            f"Consider switching to Auto mode or pausing heavy loads."
                        )
                        state.last_manual_grid_alert = hour_key
                log_to_csv(stats, "manual", "Manual override active — automation paused", now)
            except Exception as e:
                log_mode.warning(f"Manual mode stats fetch failed: {e}")
            return

        # Check dynamic alerts
        try:
            current_state = {
                "battery_pct": stats.get("battery_pct"),
                "solar_kw": stats.get("solar_kw"),
                "home_kw": stats.get("home_kw"),
                "surplus_kw": stats.get("solar_surplus_kw"),
                "grid_export_kw": stats.get("grid_export_kw"),
                "grid_kw": stats.get("grid_kw"),
                "island_mode": stats.get("island_mode"),
                "storm_mode": stats.get("storm_mode"),
                "charging_status": cp_status.get("charging_status"),
                "is_plugged_in": cp_status.get("is_plugged_in"),
                "is_connected": cp_status.get("is_connected"),
                "log_errors": check_recent_log_errors(interval_minutes=config.CHECK_INTERVAL_MINUTES + 5)
            }
            check_alerts(current_state)
        except Exception as alert_err:
            log.warning(f"Error evaluating custom alerts during cycle: {alert_err}")

        if stats["grid_kw"] > 0.1:
            state.grid_draw_count += 1
            log_netzero.warning(
                f"GRID DRAW DETECTED | grid={stats['grid_kw']}kW | "
                f"solar={stats['solar_kw']}kW | battery={stats['battery_pct']}% | "
                f"tou={tou} | rate=${get_tou_rate(now)}/kWh | "
                f"charger_state={state.charger_state}"
            )

        if stats.get("island_mode") == "off_grid":
            log.warning(
                f"OFF-GRID DETECTED | Skipping cycle | battery={stats['battery_pct']}%"
            )
            if state.charger_state == state.State.CHARGING:
                log.warning("OFF-GRID | Active session detected — stopping charger to protect home load")
                stop_charger()
                state.charger_state       = state.State.IDLE
                state.charge_session_start = None
                state.session_stop_reason = "Powerwall went off-grid — stopping to protect home"
            log_to_csv(stats, "skipped", "Powerwall off-grid — protecting home load", now)
            return
        if stats.get("storm_mode"):
            log.warning(
                f"STORM MODE ACTIVE | Skipping cycle | battery={stats['battery_pct']}%"
            )
            if state.charger_state == state.State.CHARGING:
                log.warning("STORM MODE | Active session detected — stopping charger to preserve backup reserve")
                stop_charger()
                state.charger_state       = state.State.IDLE
                state.charge_session_start = None
                state.session_stop_reason = "Storm mode activated — stopping to preserve backup reserve"
            log_to_csv(stats, "skipped", "Storm mode active — preserving backup reserve", now)
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
                start_charger()
                log_chargepoint.info(
                    f"CHARGE STARTED | battery={stats['battery_pct']}% | "
                    f"solar={stats['solar_kw']}kW | tou={tou} | reason={reason}"
                )
                notify(
                    f"🟢 Charging started\n{reason}\n"
                    f"Battery: {stats['battery_pct']}% | Solar: {stats['solar_kw']}kW | "
                    f"TOU: {tou}"
                )
            except ChargePointStartError as cpe:
                state.charger_state = state.State.IDLE
                state.charge_session_start = None
                log_chargepoint.warning(f"CHARGE START REJECTED | {cpe}")
                notify(f"⚠️ <b>EV Charging Start Notice</b>\n{cpe}")
            except Exception as e:
                state.charger_state = state.State.IDLE
                state.charge_session_start = None
                raise

        elif action == "stop":
            try:
                stop_duration = get_session_minutes()
                stop_charger()
                log_chargepoint.info(
                    f"CHARGE STOPPED | battery={stats['battery_pct']}% | "
                    f"solar={stats['solar_kw']}kW | tou={tou} | reason={reason} | "
                    f"session_duration={stop_duration:.0f}min"
                )
                notify(
                    f"🔴 Charging stopped\n{reason}\n"
                    f"Battery: {stats['battery_pct']}% | Solar: {stats['solar_kw']}kW | "
                    f"Session: {stop_duration:.0f} min"
                )
                state.charger_state = state.State.IDLE
            except Exception as e:
                state.charger_state = state.State.CHARGING
                raise

        state.consecutive_api_failures = 0
        log_to_csv(stats, action, reason, now)
        if action == "stop" or state.charger_state == state.State.IDLE:
            state.charge_session_start = None

    except requests.exceptions.HTTPError as e:
        state.consecutive_api_failures += 1
        log.error(
            f"NETZERO API ERROR | status={e.response.status_code if e.response else 'N/A'} | "
            f"url={e.request.url if e.request else 'N/A'} | {e}"
        )
        if state.consecutive_api_failures == 3:
            notify("⚠️ <b>Smart EV Charger API Notice</b>\nFailed to fetch Powerwall stats for 3 consecutive cycles. Will continue retrying.")
    except Exception as e:
        state.consecutive_api_failures += 1
        log.error(f"CYCLE ERROR | {type(e).__name__}: {e}", exc_info=True)
        if state.consecutive_api_failures == 3:
            notify(f"⚠️ <b>Smart EV Charger Warning</b>\nEncountered repeated cycle errors for 3 consecutive cycles ({type(e).__name__}). Will continue retrying.")


def handle_shutdown(signum, frame):
    log.info("SHUTDOWN | Signal received — shutting down without altering charger state")
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)

    log.info("=" * 70)
    log.info("STARTUP | Smart EV Charger")
    log.info(f"STARTUP | Thresholds: start={config.BATTERY_START_PCT}% | stop={config.BATTERY_STOP_PCT}%")
    log.info(f"STARTUP | Min session: {config.MIN_CHARGE_MINUTES}min | Interval: {config.CHECK_INTERVAL_MINUTES}min")
    log.info("STARTUP | Manual override: Controlled dynamically via Telegram Bot")
    log.info(f"STARTUP | CSV log: {config.CSV_LOG_FILE} | Text log: {config.TEXT_LOG_FILE}")
    log.info("=" * 70)

    try:
        s = get_charger_status()
        log_chargepoint.info(
            f"STARTUP CHECK | status={s['charging_status']} | "
            f"plugged_in={s['is_plugged_in']} | "
            f"connected={s['is_connected']} | "
            f"amperage={s['amperage_limit']}A"
        )
        if s["charging_status"] == "CHARGING":
            log.info("STARTUP SYNC | Adopting active charging session.")
            state.charger_state = state.State.CHARGING
            state.charge_session_start = s.get("session_start_time") or datetime.now(config.TZ)
        else:
            state.charger_state = state.State.IDLE
            state.charge_session_start = None
    except Exception as e:
        log_chargepoint.warning(f"STARTUP CHECK FAILED | {e} — will retry on first cycle")

    tz_str = getattr(config.TZ, "key", str(config.TZ))
    
    def check_monthly_schedule():
        now = datetime.now(config.TZ)
        if now.day == 1:
            log.info("MONTHLY TRIGGER | Today is the 1st of the month. Triggering monthly bill report...")
            try:
                send_monthly_telegram_report(period="last_month")
            except Exception as e:
                log.error(f"Failed to execute monthly report schedule: {e}")

    def run_daily_agent_async():
        threading.Thread(target=run_daily_agent, daemon=True, name="DailyAgentThread").start()

    schedule.every().day.at(config.DAILY_RESET_TIME, tz_str).do(daily_reset)
    schedule.every().day.at(config.DAILY_AGENT_TIME, tz_str).do(run_daily_agent_async)
    schedule.every().day.at("07:00", tz_str).do(check_monthly_schedule)

    # Start Telegram Bot if configured
    if config.TELEGRAM_BOT_TOKEN:
        start_telegram_bot(run_cycle_safe)

    run_cycle_safe()
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_cycle_safe)

    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
