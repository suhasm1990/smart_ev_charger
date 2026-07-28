import schedule
import time
import requests
import signal
import sys
from datetime import datetime

import config
import state
from logger import log, log_mode, log_netzero, log_chargepoint
from api_netzero import get_powerwall_stats
from api_chargepoint import start_charger, stop_charger, get_charger_status
from tou import get_tou_period, get_tou_rate, is_in_night_blackout, is_weekend
from manual_override import check_manual_mode
from decision import evaluate
from csv_logger import log_to_csv, get_session_minutes
from notifications import notify

def daily_reset():
    log.info(
        f"DAILY RESET | sessions_today={state.session_count_today} | "
        f"grid_draw_events={state.grid_draw_count}"
    )
    state.session_count_today = 0
    state.grid_draw_count     = 0

import threading

cycle_lock = threading.Lock()

def run_cycle_safe():
    with cycle_lock:
        run_cycle()

def run_cycle():
    config.load_dynamic_config()
    now = datetime.now(config.TZ)

    # Check manual override first — still fetch real stats for CSV
    if check_manual_mode():
        try:
            stats = get_powerwall_stats()
            tou   = get_tou_period(now)
            log_mode.debug(
                f"Manual mode | battery={stats['battery_pct']}% | "
                f"solar={stats['solar_kw']}kW | grid={stats['grid_kw']}kW | tou={tou}"
            )
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

    try:
        stats = get_powerwall_stats()
        battery_pct = stats["battery_pct"]
        tou = get_tou_period(now)

        try:
            cp_status = get_charger_status()
            stats["is_plugged_in"] = cp_status["is_plugged_in"]
            
            # Sync internal state with physical reality
            physical_charging = (cp_status["charging_status"] == "CHARGING")
            internal_charging = (state.charger_state == state.State.CHARGING)
            
            if physical_charging and not internal_charging:
                log.info("SYNC | Charger is physically charging but internal state was IDLE. Synchronizing state.")
                state.charger_state = state.State.CHARGING
                state.charge_session_start = now
            elif not physical_charging and internal_charging:
                log.info("SYNC | Charger is physically NOT charging but internal state was CHARGING. Synchronizing state.")
                state.charger_state = state.State.IDLE
                
        except Exception as e:
            log_chargepoint.warning(f"Failed to get charger status: {e} | Skipping cycle")
            return

        # Check dynamic alerts
        try:
            from alerts import check_alerts, check_recent_log_errors
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
                from api_chargepoint import ChargePointStartError
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
                stop_charger()
                log_chargepoint.info(
                    f"CHARGE STOPPED | battery={stats['battery_pct']}% | "
                    f"solar={stats['solar_kw']}kW | tou={tou} | reason={reason} | "
                    f"session_duration={get_session_minutes():.0f}min"
                )
                notify(
                    f"🔴 Charging stopped\n{reason}\n"
                    f"Battery: {stats['battery_pct']}% | Solar: {stats['solar_kw']}kW | "
                    f"Session: {get_session_minutes():.0f} min"
                )
            except Exception as e:
                state.charger_state = state.State.CHARGING
                raise

        state.consecutive_api_failures = 0
        log_to_csv(stats, action, reason, now)

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
            state.charge_session_start = datetime.now(config.TZ)
        else:
            state.charger_state = state.State.IDLE
    except Exception as e:
        log_chargepoint.warning(f"STARTUP CHECK FAILED | {e} — will retry on first cycle")

    from daily_agent import run_daily_agent
    
    # Explicitly bind the schedule to the user's timezone to prevent UTC drift
    tz_str = getattr(config.TZ, "key", str(config.TZ))
    tz_param = None
    try:
        import pytz
        tz_param = pytz.timezone(tz_str)
    except Exception:
        tz_param = None
    
    log.info(f"STARTUP | Scheduled Daily Reset at {config.DAILY_RESET_TIME} ({tz_str})")
    log.info(f"STARTUP | Scheduled Daily Agent AI Planner at {config.DAILY_AGENT_TIME} ({tz_str})")

    if tz_param:
        schedule.every().day.at(config.DAILY_RESET_TIME, tz_param).do(daily_reset)
        schedule.every().day.at(config.DAILY_AGENT_TIME, tz_param).do(run_daily_agent)
    else:
        schedule.every().day.at(config.DAILY_RESET_TIME).do(daily_reset)
        schedule.every().day.at(config.DAILY_AGENT_TIME).do(run_daily_agent)



    # Start Telegram Bot if configured
    if config.TELEGRAM_BOT_TOKEN:
        import telegram_bot
        telegram_bot.start_telegram_bot(run_cycle_safe)

    run_cycle_safe()
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_cycle_safe)

    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
