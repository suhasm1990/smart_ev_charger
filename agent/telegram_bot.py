"""Telegram control surface: LLM tool-calling assistant plus slash commands."""
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

import telebot
from telebot.handler_backends import BaseMiddleware, CancelUpdate

from agent import llm_client
from agent.alerts import add_alert, list_alerts, remove_alert
from core import config, state
from core.tou import RATE_SCHEDULES, get_tou_rate, provider, provider_label, weekday_schedule_description
from reporting.csv_logger import (
    get_daily_charging_cost as calc_cost,
)
from reporting.csv_logger import (
    get_energy_saving_advice as calc_advice,
)
from reporting.csv_logger import (
    get_home_energy_summary as calc_home_summary,
)
from reporting.csv_logger import (
    get_monthly_billing_data,
    get_recent_sessions,
    get_session_minutes,
)
from reporting.logger import log, tail_lines
from reporting.notifications import notify, strip_html
from reporting.report_generator import generate_monthly_report_image
from services.chargepoint import (
    get_charger_status as get_cp_status,
)
from services.chargepoint import (
    set_charger_amperage_limit,
    start_charger,
)
from services.charger_ops import stop_and_restore_defaults
from services.netzero import get_powerwall_stats
from services.sheets_db import add_user_instruction

RUN_CYCLE_CALLBACK = None
MAX_HISTORY_MESSAGES = 20
TELEGRAM_MAX_CHARS = 3800


def _trigger_cycle():
    """Re-evaluates immediately so a settings change takes effect at once."""
    if RUN_CYCLE_CALLBACK:
        threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True, name="BotCycle").start()


def _clamp_amperage(value, default=None) -> int:
    """Coerces an amperage to a supported integer within the charger's range."""
    fallback = config.DEFAULT_CHARGER_AMPERAGE if default is None else default
    try:
        amperage = int(float(value))
    except (TypeError, ValueError):
        return fallback
    if not config.MIN_CHARGER_AMPERAGE <= amperage <= config.MAX_CHARGER_AMPERAGE:
        return fallback
    return amperage


# ── LLM tools ───────────────────────────────────────────────────────────────

def get_system_status() -> str:
    """Gets the current EV charger state, battery percentage, solar generation, house usage, current grid import, and active thresholds."""
    try:
        pw = get_powerwall_stats()
    except Exception as e:
        pw = {}
        log.warning(f"Bot failed to get Powerwall stats: {e}")
    try:
        cp = get_cp_status()
    except Exception as e:
        cp = {}
        log.warning(f"Bot failed to get charger status: {e}")

    # Trust the hardware over in-memory state, which may have drifted.
    charging = cp.get("charging_status") == "CHARGING"
    state.sync_with_hardware(cp, datetime.now(config.TZ))

    is_manual = config.MANUAL_MODE_OVERRIDE == "manual"
    return json.dumps({
        "charger_state": "CHARGING" if charging else str(state.charger_state),
        "is_charging": charging,
        "session_duration_minutes": round(get_session_minutes(), 1),
        "battery_pct": pw.get("battery_pct"),
        "battery_activity": pw.get("battery_activity") or ("charging" if pw.get("battery_kw", 0) < -0.05 else ("discharging" if pw.get("battery_kw", 0) > 0.05 else "idle")),
        "battery_flow": pw.get("battery_flow_desc") or (
            f"charging at {abs(pw.get('battery_kw', 0))} kW from solar surplus"
            if pw.get("battery_kw", 0) < -0.05
            else (f"discharging {pw.get('battery_kw', 0)} kW to power home" if pw.get("battery_kw", 0) > 0.05 else "idle (0.0 kW)")
        ),
        "battery_kw": pw.get("battery_kw", 0.0),
        "solar_kw": pw.get("solar_kw"),
        "home_kw": pw.get("home_kw"),
        "surplus_kw": pw.get("solar_surplus_kw"),
        "grid_kw": pw.get("grid_kw"),
        "grid_export_kw": pw.get("grid_export_kw", 0.0),
        "self_powered_pct": pw.get("self_powered_pct", 100.0),
        "island_mode": pw.get("island_mode"),
        "storm_mode": pw.get("storm_mode"),
        "charging_status": cp.get("charging_status", "UNKNOWN"),
        "is_plugged_in": cp.get("is_plugged_in", False),
        "is_connected": cp.get("is_connected", False),
        "amperage_limit": cp.get("amperage_limit", config.DEFAULT_CHARGER_AMPERAGE),
        "cp_session_energy_kwh": cp.get("energy_kwh", 0.0),
        "cp_charging_power_kw": cp.get("power_kw", 0.0),
        "cp_miles_added": cp.get("miles_added", 0.0),
        "default_amperage": config.DEFAULT_CHARGER_AMPERAGE,
        "max_amperage": config.MAX_CHARGER_AMPERAGE,
        "config_battery_start_pct": config.BATTERY_START_PCT,
        "config_battery_stop_pct": config.BATTERY_STOP_PCT,
        "config_blackout_start_hour": config.NIGHT_BLACKOUT_START_HOUR,
        "config_blackout_end_hour": config.NIGHT_BLACKOUT_END_HOUR,
        "manual_mode_override": config.MANUAL_MODE_OVERRIDE,
        "mode_behavior": (
            "MANUAL_MODE: Automatic solar and battery stop thresholds (e.g. stop at 35%) and charge windows are PAUSED. "
            "Charging continues until the user manually stops it, switches back to auto, or morning reset at 09:00."
            if is_manual else
            "AUTO_MODE: Solar automation and battery stop thresholds are active."
        ),
        "manual_guard_stop_battery_pct": state.manual_guard_stop_battery_pct,
        "manual_guard_stop_at_hour": state.manual_guard_stop_at_hour,
        "manual_guard_stop_time": state.manual_guard_stop_time.isoformat() if state.manual_guard_stop_time else None,
    })


def get_recent_charging_sessions(limit: int = 5, date_or_period: str = None) -> str:
    """Gets EV charging sessions from logs, including start time, end time, duration in minutes, battery level change, and why charging stopped.
    Use this whenever the user asks for session details, session history, or why charging stopped.

    Args:
        limit: Number of recent sessions to return (default 5).
        date_or_period: Optional date or period filter such as 'today', 'yesterday', 'this_week', or 'YYYY-MM-DD'. If provided, only sessions from that period are returned.
    """
    sessions = get_recent_sessions(limit=limit, period=date_or_period)
    if not sessions:
        if date_or_period:
            return f"No charging sessions recorded for {date_or_period}."
        return "No recent charging sessions found in logs."

    header = f"Charging Sessions ({len(sessions)} recorded"
    if date_or_period:
        header += f" for {date_or_period}"
    header += "):\n\n"

    return header + "\n\n".join(
        f"Session {i}:\n"
        f"  • Start Time: {s.get('start_time', 'N/A')}\n"
        f"  • End Time: {s.get('end_time', 'N/A')}\n"
        f"  • Duration: {s.get('max_duration_minutes', 0):.1f} min\n"
        f"  • Battery SoC: {s.get('start_battery_pct', 'N/A')}% -> {s.get('end_battery_pct', 'N/A')}%\n"
        f"  • Stop Reason: {s.get('stop_reason') or 'Normal completion or manual stop'}"
        for i, s in enumerate(sessions, 1)
    )


def get_tou_schedule() -> str:
    """Gets Time-Of-Use electricity rate periods, night blackout windows, peak/partial-peak/off-peak hours, and rates for the configured utility provider."""
    schedule = RATE_SCHEDULES.get(provider(), RATE_SCHEDULES["MID"])
    def fmt(season):
        return {k: f"${v:.5f}/kWh" for k, v in schedule[season].items()}
    return json.dumps({
        "utility_provider": provider_label(),
        "timezone": str(config.TZ),
        "current_rate_per_kwh": f"${get_tou_rate(datetime.now(config.TZ)):.5f}",
        "summer_rates": fmt("summer"),
        "winter_rates": fmt("winter"),
        "weekday_schedule": weekday_schedule_description(),
        "weekend_schedule": {"off_peak": "All day on weekends and holidays"},
        "night_blackout_window": f"{config.NIGHT_BLACKOUT_START_HOUR}:00 - {config.NIGHT_BLACKOUT_END_HOUR}:00",
        "night_blackout_description":
            f"No EV charging between {config.NIGHT_BLACKOUT_START_HOUR}:00 and "
            f"{config.NIGHT_BLACKOUT_END_HOUR}:00 on weekdays",
    })


def get_tesla_powerwall_status() -> str:
    """Gets detailed Tesla Powerwall stats: battery level (SoC %), charge/discharge power (kW), solar generation, home usage, grid export/import, self-powered percentage, and grid connection status."""
    try:
        return json.dumps(get_powerwall_stats())
    except Exception as e:
        log.warning(f"Bot failed to get Powerwall stats: {e}")
        return json.dumps({"error": f"Failed to get Powerwall stats: {e}"})


def read_application_logs(num_lines: int = 50, level: str = None) -> str:
    """Reads recent system event, decision, and error logs from the Google Sheets 'System Logs' tab (falling back to local log file if offline).

    Args:
        num_lines: Number of trailing lines to read (default 50).
        level: Optional level filter ('INFO', 'WARNING', or 'ERROR').
    """
    try:
        from services.sheets_db import get_system_logs
        logs = get_system_logs(limit=num_lines, level_filter=level)
        if logs:
            return "\n".join(
                f"[{entry['timestamp']}] {entry['level']} ({entry['module']}): {entry['message']}"
                for entry in logs
            )
    except Exception as e:
        log.warning(f"Failed to read logs from Google Sheets: {e}")

    lines = tail_lines(config.TEXT_LOG_FILE, num_lines, level)
    if lines:
        return "\n".join(lines)

    return "No system logs found."


def read_source_code(file_path: str = "main.py", start_line: int = 1, end_line: int = 100) -> str:
    """Reads a snippet of source code from a file in the repository, with line numbers.
    Use this whenever the user asks how a component works or wants to inspect logic.

    Args:
        file_path: Relative path to a project file (e.g. 'main.py', 'core/decision.py').
        start_line: Starting line number (1-indexed, default 1).
        end_line: Ending line number (default 100).
    """
    path = os.path.normpath(file_path).lstrip("/\\")
    # Keep the assistant inside the repository and away from credentials.
    if path.startswith("..") or path.startswith(".env") or "service_account" in path:
        return json.dumps({"error": "Access to sensitive files or parent directories is restricted."})
    if not os.path.isfile(path):
        return json.dumps({"error": f"File '{path}' not found in repository."})
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        first = max(1, start_line)
        last = min(len(lines), end_line if end_line and end_line > 0 else first + 100)
        return json.dumps({
            "file": path,
            "total_lines": len(lines),
            "showing_lines": f"{first}-{last}",
            "content": "".join(f"{i}: {lines[i - 1]}" for i in range(first, last + 1)),
        })
    except OSError as e:
        return json.dumps({"error": f"Failed to read source code: {e}"})


def get_active_ai_model() -> str:
    """Gets the currently active AI provider and model name."""
    return json.dumps({
        "provider": config.LLM_PROVIDER,
        "model": config.LLM_MODEL,
        "summary": f"Currently configured with {config.LLM_PROVIDER.upper()} ({config.LLM_MODEL})",
    })


# Aliases the user might type, mapped to (provider, model). None means "keep the provider default".
_MODEL_ALIASES = {
    "gemini": ("gemini", None), "google": ("gemini", None),
    "gemini-pro": ("gemini", "gemini-2.5-pro"), "gemini_pro": ("gemini", "gemini-2.5-pro"),
    "gemini-2.5-pro": ("gemini", "gemini-2.5-pro"), "pro": ("gemini", "gemini-2.5-pro"),
    "nvidia": ("nvidia", None), "nemotron": ("nvidia", None), "llama": ("nvidia", None),
    "nemotron-super": ("nvidia", "nvidia/nemotron-3-super-120b-a12b"),
    "nemotron-ultra": ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
    "nemotron-nano": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "openai": ("openai", None), "chatgpt": ("openai", None), "gpt": ("openai", None),
    "anthropic": ("anthropic", None), "claude": ("anthropic", None), "sonnet": ("anthropic", None),
}


def switch_llm_model(provider: str = "", model_name: str = None) -> str:
    """Switches the active AI model and provider, or reports the current one.

    Args:
        provider: Provider name ('gemini', 'nvidia', 'openai', 'anthropic', or 'status').
        model_name: Optional specific model (e.g. 'gemini-2.5-pro', 'gpt-4o', 'claude-sonnet-5').
    """
    key = (provider or "").lower().strip()
    if not key or key in ("status", "current", "what", "check", "get", "info"):
        return f"Currently using <b>{config.LLM_PROVIDER.upper()}</b> with model <code>{config.LLM_MODEL}</code>."

    resolved, alias_model = _MODEL_ALIASES.get(key, (None, None))
    if resolved is None:
        # Allow a bare model name whose provider is unambiguous from its prefix.
        resolved = next((p for a, (p, _) in _MODEL_ALIASES.items() if a in key), None)
        if resolved is None:
            return (f"Error: Unknown provider '{provider}'. "
                    f"Supported: {', '.join(llm_client.PROVIDER_DEFAULTS)}.")
        alias_model = provider if any(c in key for c in "-.") else None

    model = model_name or alias_model or llm_client.PROVIDER_MODELS.get(resolved)
    key_attr = llm_client.PROVIDER_DEFAULTS[resolved][0]
    if not getattr(config, key_attr, ""):
        return f"Warning: {key_attr} is not configured in .env."

    config.update(LLM_PROVIDER=resolved, LLM_MODEL=model)
    log.info(f"AI model switched to provider='{resolved}', model='{model}'")
    return f"✅ Switched active AI model to <b>{resolved.upper()}</b> (<code>{model}</code>)."


def set_battery_thresholds(start_pct: float, stop_pct: float) -> str:
    """Updates the battery start and stop percentage thresholds.

    Args:
        start_pct: Battery level (%) required to start or resume charging.
        stop_pct: Battery level (%) at which charging stops to protect home power.
    """
    try:
        start = float(start_pct)
        stop = float(stop_pct)
    except (TypeError, ValueError):
        return "Error: Invalid numeric value for battery thresholds."
    if start <= stop:
        return f"Error: Battery start threshold ({start}%) must be strictly greater than stop threshold ({stop}%)."
    config.update(BATTERY_START_PCT=start, BATTERY_STOP_PCT=stop)
    _trigger_cycle()
    return f"Success: Set battery start threshold to {config.BATTERY_START_PCT}% and stop to {config.BATTERY_STOP_PCT}%."


def set_blackout_hours(start_hour: int, end_hour: int) -> str:
    """Updates the TOU night blackout start and end hours (24-hour format).

    Args:
        start_hour: Hour (0-23) when the blackout begins (default 16).
        end_hour: Hour (0-23) when the blackout ends (default 9).
    """
    try:
        config.update(NIGHT_BLACKOUT_START_HOUR=int(start_hour), NIGHT_BLACKOUT_END_HOUR=int(end_hour))
    except (TypeError, ValueError):
        return "Error: Invalid hour values for the blackout window."
    _trigger_cycle()
    return (f"Success: Set the night blackout window to "
            f"{config.NIGHT_BLACKOUT_START_HOUR}:00 - {config.NIGHT_BLACKOUT_END_HOUR}:00.")


def set_override_mode(mode: str) -> str:
    """Controls the automation override mode.

    Args:
        mode: 'manual' to pause solar automation, or 'auto' to resume automatic tracking.
    """
    normalized = (mode or "").lower().strip()
    if normalized not in ("manual", "auto"):
        return "Error: Mode must be 'manual' or 'auto'."
    config.update(MANUAL_MODE_OVERRIDE=normalized)
    if normalized == "auto":
        state.clear_manual_guards()
        try:
            set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
        except Exception as e:
            log.warning(f"Could not restore default amperage: {e}")
    _trigger_cycle()
    return f"Success: Configured override mode to '{normalized}'."


def start_charging(amperage: int = 20, stop_battery_pct: float = None,
                   stop_at_hour: int = None, duration_hours: float = None) -> str:
    """Forces the charger to start at the given amperage, with optional guardrails that stop it and return to Auto mode.

    Args:
        amperage: Current limit in Amps (20 for normal, 32 for full power, range 8-32).
        stop_battery_pct: Optional battery percentage below which charging stops. Only pass if explicitly requested.
        stop_at_hour: Optional hour (0-23) when charging stops. Only pass if explicitly requested.
        duration_hours: Optional maximum duration in hours before stopping. Only pass if explicitly requested.
    """
    try:
        amperage = _clamp_amperage(amperage)

        def optional(value, cast):
            if value is None:
                return None
            try:
                return cast(value)
            except (TypeError, ValueError):
                return None

        hours = optional(duration_hours, float)
        state.set_manual_guards(
            stop_battery_pct=optional(stop_battery_pct, float),
            stop_at_hour=optional(stop_at_hour, int),
            stop_time=datetime.now(config.TZ) + timedelta(hours=hours) if hours else None,
        )

        start_charger(amperage)

        state.begin_session(datetime.now(config.TZ), amperage)
        config.update(MANUAL_MODE_OVERRIDE="manual")

        guards = []
        if state.manual_guard_stop_battery_pct is not None:
            guards.append(f"stop if battery < {state.manual_guard_stop_battery_pct}%")
        if state.manual_guard_stop_at_hour is not None:
            guards.append(f"stop at {state.manual_guard_stop_at_hour}:00")
        if state.manual_guard_stop_time is not None:
            guards.append(f"stop at {state.manual_guard_stop_time.strftime('%H:%M')}")

        notify(f"🟢 Charging started (Forced manually via Telegram at {amperage}A"
               f"{f' — guards: {chr(44).join(guards)}' if guards else ''})")
        _trigger_cycle()
        return (f"Success: Sent start command at {amperage}A. "
                f"Active guardrails: {', '.join(guards) if guards else 'default blackout/battery limits'}. "
                f"Switched to Manual override.")
    except Exception as e:
        return f"Error starting charger: {e}"


def stop_charging() -> str:
    """Immediately forces the charger to stop charging."""
    try:
        stop_and_restore_defaults()

        state.end_session("Stopped manually via Telegram bot")
        state.clear_manual_guards()
        config.update(MANUAL_MODE_OVERRIDE="manual")

        notify("🔴 Charging stopped (Forced manually via Telegram)")
        _trigger_cycle()
        return "Success: Sent stop command. Switched to Manual override to prevent an automatic restart."
    except Exception as e:
        return f"Error stopping charger: {e}"


def set_charger_amperage(amperage: int) -> str:
    """Updates the charger's amperage limit dynamically (between 8 and 32 Amps).

    Args:
        amperage: The current limit to set in Amps.
    """
    clamped = _clamp_amperage(amperage, default=0)
    if not clamped:
        return (f"Error: Amperage must be between {config.MIN_CHARGER_AMPERAGE} "
                f"and {config.MAX_CHARGER_AMPERAGE} Amps.")
    try:
        set_charger_amperage_limit(clamped)
        state.active_amperage = clamped
        return f"Success: Charger amperage limit set to {clamped}A."
    except Exception as e:
        return f"Error setting amperage: {e}"


def manage_custom_alert(action: str = "list", field: str = "battery_pct", operator: str = "gte",
                        value: float = 80.0, message: str = "", alert_id: str = "") -> str:
    """Manages custom notification alerts: add a new alert, list active alerts, or remove one.

    Args:
        action: One of 'add', 'list', or 'remove'.
        field: Metric to monitor ('battery_pct', 'solar_kw', 'home_kw', 'grid_kw', 'charging_status', 'is_plugged_in', 'log_errors').
        operator: Comparison operator ('eq', 'ne', 'gt', 'gte', 'lt', 'lte').
        value: Numeric threshold to compare against.
        message: Alert message sent when the condition triggers.
        alert_id: 8-character ID of the alert to remove.
    """
    act = (action or "list").lower().strip()
    if act in ("add", "create", "set"):
        return add_alert(field=field, operator=operator, value=value, message=message, once=True)
    if act in ("remove", "delete", "clear"):
        return remove_alert(alert_id)
    return list_alerts()


def get_daily_charging_cost(date_or_period: str = "today") -> str:
    """Calculates grid energy drawn (kWh), solar energy used, total energy added, estimated miles added, and cost ($) for EV charging over a period.
    Use this whenever the user asks about total miles added today, miles or range added, total kWh charged, charging cost, or solar energy used.

    Args:
        date_or_period: Period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD'.
    """
    return json.dumps(calc_cost(period=date_or_period))


def get_home_energy_summary(date_or_period: str = "today") -> str:
    """Calculates total home electricity consumption, solar generated, grid imported, total bill cost, and the split between EV charging and home appliances.
    Use this whenever the user asks how much the home consumed or cost over a period.

    Args:
        date_or_period: Period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD'.
    """
    return json.dumps(calc_home_summary(period=date_or_period))


def get_energy_saving_advice() -> str:
    """Analyses recent 7-day usage to find peak solar windows, high-cost grid draws, and actionable ways to reduce the bill.
    Use this whenever the user asks for advice on reducing their bill or when to run heavy appliances.
    """
    return json.dumps(calc_advice())


def add_agent_instruction(text: str) -> str:
    """Saves a note or override instruction for the Daily AI Agent to apply on its next run."""
    if add_user_instruction(text):
        return f"Success: Saved instruction '{text}' for the Daily AI Agent."
    return "Error: Failed to save the instruction. Please check the Google Sheets integration."


def trigger_daily_agent() -> str:
    """Runs the Daily AI Agent planner to analyse recent solar generation, optimise the charge window, and update battery thresholds.
    Use this whenever the user asks to run the daily agent or plan today's charging.
    """
    from agent.daily_agent import run_daily_agent
    try:
        run_daily_agent()
        return "Success: Ran the Daily AI Agent. The plan and settings have been updated and sent to Telegram."
    except Exception as e:
        return f"Error executing the Daily AI Agent: {e}"


def run_antigravity_dev_task(task_description: str, pr_number: int = None) -> str:
    """Dispatches an autonomous developer agent in the background to investigate logs, fix code, run tests, and open or update a GitHub Pull Request.
    Use this whenever the user asks to investigate a bug, fix code, add a feature, or create/update a PR.

    Args:
        task_description: Detailed description of what to investigate, fix, or update.
        pr_number: Optional PR number if updating an existing open PR.
    """
    from agent.dev_agent import dispatch_dev_task_background
    dispatch_dev_task_background(task_description, pr_number)
    target = f"update PR #{pr_number}" if pr_number else "investigate, fix, and open a Pull Request"
    return f"Autonomous agent dispatched in the background to {target}. You will get a Telegram notification when it finishes."


def restart_and_update_application() -> str:
    """Restarts the container so it pulls the latest code from GitHub and reloads.
    Use this whenever the user asks to restart, update, pull the latest code, or reload after merging a PR.
    """
    _schedule_exit()
    return "🔄 Restarting now. The container will pull the latest code from GitHub and be back in ~5-10 seconds."


def _schedule_exit(delay: float = 2.0):
    """Exits after a short delay so the acknowledgement reaches Telegram first, ensuring all Sheets writes flush."""
    def worker():
        time.sleep(delay)
        log.info("RESTART | Flushing all pending queues to Google Sheets before exit...")
        try:
            from services.sheets_db import flush
            flush(timeout=10.0)
        except Exception:
            pass
        try:
            from reporting.notifications import notify_flush
            notify_flush(2.0)
        except Exception:
            pass
        log.info("RESTART | Exiting to trigger a container restart and git pull.")
        os._exit(0)
    threading.Thread(target=worker, daemon=True, name="Restart").start()


# ── Monthly report ──────────────────────────────────────────────────────────

_pending_image = threading.local()


def generate_monthly_report(period: str = "last_month") -> str:
    """Generates a high-resolution PNG monthly electricity bill report for a month (e.g. 'last_month', 'this_month', 'June 2026', or 'YYYY-MM').
    Use this whenever the user asks for a monthly bill report, usage graph, or bill image.
    """
    data = get_monthly_billing_data(period=period)
    if "error" in data:
        return json.dumps({"error": data["error"]})

    # Hand the computed summary straight to the renderer rather than recomputing it.
    path = generate_monthly_report_image(period=period, data=data)
    if not (path and os.path.exists(path)):
        return json.dumps({"error": "Failed to generate the monthly report image."})
    _pending_image.path = path
    return json.dumps({"status": "success", "image_path": path,
                       "note": "Monthly report PNG generated successfully."})


def send_monthly_telegram_report(period: str = "last_month"):
    """Sends the monthly bill infographic to the configured Telegram user."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_ID):
        return
    try:
        path = generate_monthly_report_image(period=period)
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                telebot.TeleBot(config.TELEGRAM_BOT_TOKEN).send_photo(
                    config.TELEGRAM_ALLOWED_USER_ID, photo=f,
                    caption="📊 <b>Monthly Utility & Energy Bill Briefing</b>", parse_mode="HTML",
                )
            log.info("Sent the monthly report image to Telegram.")
    except Exception as e:
        log.error(f"Failed to send the monthly report to Telegram: {e}")


# ── Response formatting ─────────────────────────────────────────────────────

_UNSUPPORTED_TAGS = re.compile(
    r"</?(?:ul|ol|li|p|br|div|span|header|footer|section|h[1-6]|table|tr|td|th)[^>]*>", re.IGNORECASE)


def clean_telegram_html(text: str) -> str:
    """Normalises model output into the small HTML subset Telegram accepts."""
    if not text:
        return ""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<!\w)\*([^\*]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"^[ \t]*[\*\-][ \t]+", "• ", text, flags=re.MULTILINE)
    text = _UNSUPPORTED_TAGS.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── LLM message handling ────────────────────────────────────────────────────

TOOLS = [
    get_system_status, get_recent_charging_sessions, get_daily_charging_cost,
    get_home_energy_summary, get_energy_saving_advice, generate_monthly_report,
    get_tou_schedule, get_tesla_powerwall_status, read_application_logs, read_source_code,
    set_battery_thresholds, set_blackout_hours, set_override_mode,
    start_charging, stop_charging, set_charger_amperage, manage_custom_alert,
    switch_llm_model, trigger_daily_agent, add_agent_instruction,
    run_antigravity_dev_task, restart_and_update_application,
]

SYSTEM_INSTRUCTION = (
    "You are an AI assistant for a Smart EV Charger. You help the user monitor and control their solar EV charging system.\n\n"
    "CAPABILITIES & TOOLS:\n"
    "- Real-time status: Call 'get_system_status' or 'get_tesla_powerwall_status' for Powerwall battery %, solar, home load, grid, and charger state.\n"
    "- History & metrics: Call 'get_daily_charging_cost' (returns 'total_sessions_count' for session count, and 'total_kwh_added') or 'get_recent_charging_sessions' (pass date_or_period='today' or 'yesterday' to filter by date) or 'get_home_energy_summary'.\n"
    "- Advice & TOU rates: Call 'get_energy_saving_advice' or 'get_tou_schedule'.\n"
    "- Charger control: Call 'start_charging' (use 32A for full/maximum power, 20A otherwise) or 'stop_charging'. Only pass stop_battery_pct, stop_at_hour, or duration_hours if explicitly asked.\n"
    "- Thresholds & blackout: Call 'set_battery_thresholds', 'set_blackout_hours', or 'set_override_mode'.\n"
    "- Alerts & models: Call 'manage_custom_alert' or 'switch_llm_model'.\n"
    "- Daily planner & system: Call 'trigger_daily_agent', 'generate_monthly_report', 'read_application_logs', 'read_source_code', or 'restart_and_update_application'.\n\n"
    "CRITICAL OPERATIONAL RULES:\n"
    "1. When asked how many charging sessions happened (e.g. 'How many charging sessions happened today?'), report 'total_sessions_count' from 'get_daily_charging_cost'. NEVER report 'charging_15min_intervals_count' (which counts 15-minute polling rows, not sessions).\n"
    "2. When the user asks for session details or history for a specific day (or asks 'Total charging session details' after asking about today/yesterday), ALWAYS pass date_or_period='today' (or the specified date) to 'get_recent_charging_sessions' so sessions from other days are excluded.\n"
    "3. Whenever the user asks about status, metrics, battery, solar, or costs, ALWAYS call the relevant tool to fetch fresh live data. Never guess or use stale numbers.\n"
    "4. In MANUAL mode, automatic solar optimization and battery-stop thresholds are paused until auto mode is restored.\n"
    "5. After running a tool, always provide a concise, friendly natural language summary of the results.\n"
    "6. Format responses using clean Telegram HTML (<b>bold</b>, <i>italic</i>, <code>code</code>). Avoid unsupported HTML tags."
)

_history: list = []
_history_lock = threading.Lock()


def handle_message_with_llm(text: str) -> str:
    """Runs one assistant turn against the shared conversation history."""
    global _history
    if not llm_client.resolve_llm_config().get("api_key"):
        provider_name = llm_client.resolve_llm_config().get("provider")
        return f"LLM API key is not configured for provider '{provider_name}'. Please set the environment variables."

    if text.strip().lower() in ("/clear", "/reset"):
        with _history_lock:
            _history = []
        return "Conversation history cleared."

    with _history_lock:
        history = list(_history)

    reply, updated = llm_client.chat_with_tools(
        history=history, user_text=text, tools=TOOLS, system_instruction=SYSTEM_INSTRUCTION,
    )

    with _history_lock:
        _history = llm_client.trim_history(updated, MAX_HISTORY_MESSAGES)
    return reply


# ── Polling loop ────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🔋 <b>Welcome to the Smart EV Charger Assistant!</b>\n\n"
    "I'm powered by AI and can control your solar charger. Text me in natural language, for example:\n"
    "• <i>'Run daily agent'</i> or <i>'Plan today's charging'</i>\n"
    "• <i>'Why did charging stop last time?'</i>\n"
    "• <i>'What are the peak and partial peak timings?'</i>\n"
    "• <i>'Switch model to nvidia'</i> or <i>'Use gemini pro'</i>\n"
    "• <i>'Charge with full power'</i>\n"
    "• <i>'Stop charging when battery goes below 40%'</i>\n"
    "• <i>'Turn on manual mode'</i>\n\n"
    "<b>Commands:</b> /model, /logs, /daily_agent, /monthly_report, /update, /clear"
)


class AuthMiddleware(BaseMiddleware):
    """Rejects every message from anyone but the allowlisted owner.

    This runs before dispatch, so authorisation cannot be forgotten when a new
    handler is added — which matters because an accepted message can reach
    shell execution through the dev agent, restart the container, and start or
    stop the physical charger.
    """

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.update_types = ["message", "edited_message"]

    def pre_process(self, message, data):
        if message.from_user.id == config.TELEGRAM_ALLOWED_USER_ID:
            return None
        log.warning(
            f"UNAUTHORIZED | Rejected Telegram message from user_id={message.from_user.id} "
            f"(@{getattr(message.from_user, 'username', None)})"
        )
        try:
            self.bot.reply_to(message, "Unauthorized: you are not allowed to control this EV charger.")
        except Exception:
            pass
        return CancelUpdate()

    def post_process(self, message, data, exception):
        if exception:
            log.error(f"Unhandled Telegram handler error: {exception}", exc_info=exception)


def _bot_polling_loop():
    bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, use_class_middlewares=True)
    bot.setup_middleware(AuthMiddleware(bot))

    def command(*names):
        """Registers a slash-command handler. Authorisation is handled upstream."""
        def decorator(fn):
            bot.message_handler(commands=list(names))(fn)
            return fn
        return decorator

    @command("start", "help")
    def _help(message):
        bot.reply_to(message, HELP_TEXT, parse_mode="HTML")

    @command("model", "models")
    def _model(message):
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) == 1:
            bot.reply_to(message, (
                f"🧠 <b>Active AI Model</b>\n\n"
                f"• <b>Provider</b>: <code>{config.LLM_PROVIDER}</code>\n"
                f"• <b>Model</b>: <code>{config.LLM_MODEL}</code>\n\n"
                f"<b>Quick switch:</b>\n"
                + "\n".join(f"• <code>/model {alias}</code>" for alias in
                            ("gemini", "gemini-pro", "nvidia", "openai", "claude"))
                + "\n\n<i>Or just ask: 'Switch model to nvidia'</i>"
            ), parse_mode="HTML")
            return
        bot.reply_to(message, switch_llm_model(parts[1], parts[2] if len(parts) > 2 else None),
                     parse_mode="HTML")

    @command("restart", "update")
    def _restart(message):
        bot.reply_to(message, "🔄 <b>Restarting...</b>\nPulling the latest code. Back online in ~5-10s.",
                     parse_mode="HTML")
        _schedule_exit()

    @command("daily_agent", "plan", "daily_plan")
    def _daily_agent(message):
        bot.send_chat_action(message.chat.id, "typing")
        from agent.daily_agent import run_daily_agent
        try:
            run_daily_agent()
            bot.reply_to(message, "✅ <b>Daily AI Agent finished.</b> See the update above for today's plan.",
                         parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ <b>Daily AI Agent error:</b> {html.escape(str(e))}", parse_mode="HTML")

    @command("monthly_report", "bill", "monthly_bill")
    def _monthly(message):
        bot.send_chat_action(message.chat.id, "upload_photo")
        args = (message.text or "").split(maxsplit=1)
        period = args[1].strip() if len(args) > 1 else "last_month"
        path = generate_monthly_report_image(period)
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                bot.send_photo(message.chat.id, photo=f,
                               caption="⚡ <b>Monthly Electricity & Utility Bill Report</b>", parse_mode="HTML")
        else:
            bot.reply_to(message, f"Error: could not generate a monthly report for '{period}'.")

    @command("logs", "log", "syslog")
    def _logs(message):
        bot.send_chat_action(message.chat.id, "typing")
        parts = (message.text or "").strip().split()
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 25
        level = parts[2].upper() if len(parts) > 2 and parts[2].upper() in ("INFO", "WARNING", "ERROR") else None
        text = read_application_logs(num_lines=count, level=level)
        bot.reply_to(message, f"📋 <b>Recent System Logs</b>\n\n<pre>{html.escape(text[-TELEGRAM_MAX_CHARS:])}</pre>",
                     parse_mode="HTML")

    @bot.message_handler(func=lambda message: True)
    def _chat(message):
        bot.send_chat_action(message.chat.id, "typing")
        _pending_image.path = None
        try:
            raw = handle_message_with_llm(message.text)
            cleaned = clean_telegram_html(raw)

            if cleaned:
                try:
                    bot.reply_to(message, cleaned, parse_mode="HTML")
                except telebot.apihelper.ApiTelegramException as e:
                    log.warning(f"Telegram HTML parse error: {e}. Retrying as plain text.")
                    plain = strip_html(raw).strip()
                    if plain:
                        bot.reply_to(message, plain, parse_mode=None)
            elif not getattr(_pending_image, "path", None):
                user_msg = (message.text or "").lower()
                if any(k in user_msg for k in ("status", "charger", "battery", "solar", "state")):
                    try:
                        status_json = json.loads(get_system_status())
                        status_reply = (
                            f"⚡ <b>Charger State</b>: <code>{status_json.get('charger_state', 'IDLE')}</code>\n"
                            f"🔋 <b>Battery</b>: {status_json.get('battery_pct', 0)}%\n"
                            f"☀️ <b>Solar</b>: {status_json.get('solar_kw', 0)} kW | 🏠 <b>Home</b>: {status_json.get('home_kw', 0)} kW\n"
                            f"🔌 <b>Plugged in</b>: {'Yes' if status_json.get('is_plugged_in') else 'No'}\n"
                            f"⚙️ <b>Amperage</b>: {status_json.get('amperage_limit', 20)}A"
                        )
                        bot.reply_to(message, status_reply, parse_mode="HTML")
                    except Exception:
                        bot.reply_to(message, "✅ Request received. Check /logs for recent activity.", parse_mode=None)
                else:
                    bot.reply_to(message, "✅ Request received. Check /logs for recent activity.", parse_mode=None)

            path = getattr(_pending_image, "path", None)
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        bot.send_photo(message.chat.id, photo=f,
                                       caption="⚡ <b>Monthly Electricity & Utility Bill Report</b>",
                                       parse_mode="HTML")
                except Exception as e:
                    log.error(f"Failed to send the report photo: {e}")
                _pending_image.path = None
        except Exception as e:
            log.error(f"Telegram bot error: {e}", exc_info=True)
            bot.reply_to(message, f"Sorry, I encountered an error: {e}")

    log.info("Telegram bot polling started.")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=20, logger_level=logging.WARNING)
        except Exception as e:
            log.warning(f"Telegram polling interrupted ({e}). Reconnecting in 5s...")
            time.sleep(5)


def start_telegram_bot(run_cycle_callback):
    """Starts the bot on a daemon thread, wired to trigger control cycles."""
    global RUN_CYCLE_CALLBACK
    RUN_CYCLE_CALLBACK = run_cycle_callback
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not configured. Telegram bot disabled.")
        return
    if config.TELEGRAM_ALLOWED_USER_ID is None:
        # Refusing to start is the safe default: this bot can run shell commands
        # via the dev agent, so an unset allowlist must not mean "allow anyone".
        log.error(
            "TELEGRAM_ALLOWED_USER_ID is missing or not a number. Telegram bot disabled — "
            "set it to your numeric Telegram user ID to enable remote control."
        )
        return
    threading.Thread(target=_bot_polling_loop, daemon=True, name="TelegramBot").start()
    log.info("Telegram bot thread started.")
