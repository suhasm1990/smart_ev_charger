import os
import json
import re
import html
import time
import subprocess
import threading
import logging
from datetime import datetime, timedelta

import telebot
from agent import llm_client
from core import config, state
from core.tou import get_tou_rate
from reporting.logger import log
from reporting.notifications import notify
from services.netzero import get_powerwall_stats
from services.chargepoint import (
    get_charger_status as get_cp_status,
    start_charger,
    stop_charger,
    set_charger_amperage_limit
)
from reporting.csv_logger import (
    get_session_minutes,
    get_recent_sessions,
    get_daily_charging_cost as calc_cost,
    get_home_energy_summary as calc_home_summary,
    get_energy_saving_advice as calc_advice,
    get_monthly_billing_data
)
from reporting.report_generator import generate_monthly_report_image
from services.sheets_db import add_user_instruction
from agent.alerts import add_alert, remove_alert, list_alerts

RUN_CYCLE_CALLBACK = None

# ── 1. Helper Tools for LLM Function Calling ─────────────────────────────

def restart_and_update_application() -> str:
    """Restarts the Docker container to trigger 'git pull origin main' and load the latest updates.
    Use this tool whenever the user asks to restart the app, pull latest code, update the application, or reload after merging a PR.
    """
    def _delayed_exit():
        import time
        time.sleep(2)
        log.info("RESTART | Exiting process to trigger Docker container restart and git pull.")
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return "🔄 Restarting container now. It will automatically pull the latest code from GitHub and come back online in ~5-10 seconds."

def get_active_ai_model() -> str:
    """Gets the currently active AI provider and model name (e.g. Gemini 2.5 Flash, NVIDIA Nemotron)."""
    p = getattr(config, "LLM_PROVIDER", "gemini")
    m = getattr(config, "LLM_MODEL", "gemini-2.5-flash")
    return json.dumps({
        "provider": p,
        "model": m,
        "summary": f"Currently configured with {p.upper()} ({m})"
    })
def read_source_code(file_path: str = "main.py", start_line: int = 1, end_line: int = 100) -> str:
    """Reads a concise snippet of source code from a file in the repository with line numbers.
    Use this tool whenever the user asks how a component works, to inspect logic, or investigate code.
    
    Args:
        file_path: Relative path to the project file (e.g. 'main.py', 'core/decision.py', 'services/chargepoint.py', 'agent/telegram_bot.py', 'reporting/csv_logger.py').
        start_line: Starting line number (1-indexed, default 1).
        end_line: Ending line number (default 100).
    """
    clean_path = os.path.normpath(file_path).lstrip("/\\")
    if clean_path.startswith("..") or clean_path.startswith(".env") or "service_account" in clean_path:
        return json.dumps({"error": "Security check: Access to sensitive files or parent directories is restricted."})
    
    if not os.path.exists(clean_path):
        return json.dumps({"error": f"File '{clean_path}' not found in repository."})
    
    try:
        with open(clean_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        s_line = max(1, start_line)
        e_line = min(total_lines, end_line if end_line and end_line > 0 else s_line + 100)
        
        # Format with line numbers for crystal-clear, unambiguous inspection
        numbered_lines = [f"{i}: {lines[i-1]}" for i in range(s_line, e_line + 1)]
        snippet = "".join(numbered_lines)
        
        return json.dumps({
            "file": clean_path,
            "total_lines": total_lines,
            "showing_lines": f"{s_line}-{e_line}",
            "content": snippet
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to read source code: {str(e)}"})

def switch_llm_model(provider: str = "", model_name: str = None) -> str:
    """Dynamically switches the active AI model and provider (e.g. Gemini, NVIDIA, OpenAI, Anthropic) or queries the active model.
    
    Args:
        provider: The provider name ('gemini', 'nvidia', 'openai', 'anthropic', 'status').
        model_name: Optional specific model name (e.g. 'gemini-2.5-flash', 'gemini-2.5-pro', 'nvidia/nemotron-3.5-lightning-30b-a3b', 'gpt-4o', 'claude-3-7-sonnet').
    """
    provider = (provider or "").lower().strip()
    if not provider or provider in ("status", "current", "what", "check", "get", "info"):
        current_p = getattr(config, "LLM_PROVIDER", "gemini")
        current_m = getattr(config, "LLM_MODEL", "gemini-2.5-flash")
        return f"Currently using <b>{current_p.upper()}</b> with model <code>{current_m}</code>."

    provider_defaults = {
        "gemini": "gemini-2.5-flash",
        "nvidia": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "openai": "gpt-4o",
        "anthropic": "claude-3-7-sonnet"
    }

    # Auto-resolve aliases if user passed model name directly as provider
    if "nemotron" in provider or "llama" in provider or "nvidia" in provider:
        if not model_name:
            model_name = provider if "/" in provider else "nvidia/nemotron-3.5-lightning-30b-a3b"
        provider = "nvidia"
    elif "gemini" in provider or "google" in provider:
        if not model_name:
            model_name = provider if "gemini-" in provider else "gemini-2.5-flash"
        provider = "gemini"
    elif "gpt" in provider or "openai" in provider or "o1" in provider or "o3" in provider:
        if not model_name:
            model_name = provider if ("gpt" in provider or "o1" in provider or "o3" in provider) else "gpt-4o"
        provider = "openai"
    elif "claude" in provider or "anthropic" in provider or "sonnet" in provider:
        if not model_name:
            model_name = provider if "claude" in provider else "claude-3-7-sonnet"
        provider = "anthropic"

    if provider not in provider_defaults:
        return f"Error: Unknown provider '{provider}'. Supported providers: gemini, nvidia, openai, anthropic."

    if not model_name:
        model_name = provider_defaults[provider]

    # Validate provider credentials
    key_attr, _, _ = llm_client.PROVIDER_DEFAULTS.get(provider, ("", None, ""))
    if key_attr and not getattr(config, key_attr, ""):
        return f"Warning: {key_attr} is not configured in .env."

    config.LLM_PROVIDER = provider
    config.LLM_MODEL = model_name
    config.save_dynamic_config()
    log.info(f"AI Model switched to provider='{provider}', model='{model_name}'")
    return f"✅ Successfully switched active AI Model to <b>{provider.upper()}</b> (<code>{model_name}</code>)."

def run_antigravity_dev_task(task_description: str, pr_number: int = None) -> str:
    """Dispatches an autonomous developer agent in the background to investigate logs, fix codebase issues, run tests, and create or update a GitHub Pull Request.
    Use this tool whenever the user asks to investigate a bug/issue, fix code, add a feature, create a PR, or update an existing open PR with changes.
    
    Args:
        task_description: Detailed description of what to investigate, fix, or update.
        pr_number: Optional integer PR number (e.g. 12) if the user wants to update an existing open PR.
    """
    from agent.dev_agent import dispatch_dev_task_background
    dispatch_dev_task_background(task_description, pr_number)
    if pr_number:
        return f"Autonomous agent dispatched in background to update PR #{pr_number}. You will receive a Telegram notification when complete."
    return "Autonomous agent dispatched in background to investigate, fix, and create a Pull Request. You will receive a Telegram notification when the PR is ready."

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
        log.warning(f"Bot failed to get Charger status: {e}")
        
    # Synchronize internal in-memory state with physical hardware status immediately
    physical_charging = (cp.get("charging_status") == "CHARGING")
    if physical_charging:
        state.charger_state = state.State.CHARGING
        if not state.charge_session_start:
            state.charge_session_start = cp.get("session_start_time") or datetime.now(config.TZ)
        elif cp.get("session_start_time") and abs((state.charge_session_start - cp.get("session_start_time")).total_seconds()) > 3600:
            state.charge_session_start = cp.get("session_start_time")
        state.session_stop_reason = None
    else:
        if state.charger_state == state.State.CHARGING or state.charge_session_start is not None:
            state.charger_state = state.State.IDLE
            state.charge_session_start = None

    effective_state = "CHARGING" if physical_charging else str(state.charger_state)

    status_data = {
        "charger_state": effective_state,
        "is_charging": physical_charging,
        "session_duration_minutes": round(get_session_minutes(), 1),
        "previous_session_stop_reason": getattr(state, "session_stop_reason", "N/A"),
        "battery_pct": pw.get("battery_pct"),
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
        "manual_guard_stop_battery_pct": state.manual_guard_stop_battery_pct,
        "manual_guard_stop_at_hour": state.manual_guard_stop_at_hour,
        "manual_guard_stop_time": state.manual_guard_stop_time.isoformat() if state.manual_guard_stop_time else None,
    }
    log.info(f"DEBUG: get_system_status result: {status_data}")
    return json.dumps(status_data)

def get_recent_charging_sessions(limit: int = 5) -> str:
    """Gets a list of recent EV charging sessions from logs, including start time, end time, session duration (minutes), battery level change, and the reason why charging stopped.
    Use this tool whenever the user asks when charging stopped, why charging stopped, or about previous session charge times.
    
    Args:
        limit: Number of recent sessions to return (default 5).
    """
    sessions = get_recent_sessions(limit=limit)
    if not sessions:
        return "No recent charging sessions found in logs."
    
    formatted = []
    for idx, s in enumerate(sessions, 1):
        formatted.append(
            f"Session {idx}:\n"
            f"  • Start Time: {s.get('start_time', 'N/A')}\n"
            f"  • End Time: {s.get('end_time', 'N/A')}\n"
            f"  • Duration: {s.get('max_duration_minutes', 0):.1f} min\n"
            f"  • Battery SoC: {s.get('start_battery_pct', 'N/A')}% -> {s.get('end_battery_pct', 'N/A')}%\n"
            f"  • Stop Reason: {s.get('stop_reason', 'Normal completion or manual stop')}"
        )
    return "\n\n".join(formatted)

def get_tou_schedule() -> str:
    """Gets details about Time-Of-Use (TOU) electricity rate periods, night blackout windows, peak/partial-peak/off-peak hours, and rates for the configured utility provider."""
    now = datetime.now(config.TZ)
    current_rate = get_tou_rate(now)
    provider = getattr(config, "UTILITY_PROVIDER", "MID").upper()
    
    if provider == "PGE":
        provider_name = "PG&E EV2-A"
        summer_rates = {"off_peak": "$0.28312/kWh", "partial_peak": "$0.44812/kWh", "on_peak": "$0.59251/kWh"}
        winter_rates = {"off_peak": "$0.26512/kWh", "partial_peak": "$0.41200/kWh", "on_peak": "$0.43512/kWh"}
    else:
        provider_name = "Modesto Irrigation District (MID) Rate N2-EVD"
        summer_rates = {"off_peak": "$0.14513/kWh", "partial_peak": "$0.20192/kWh", "on_peak": "$0.31235/kWh"}
        winter_rates = {"off_peak": "$0.14324/kWh", "partial_peak": "$0.14324/kWh", "on_peak": "$0.22401/kWh"}

    schedule_info = {
        "utility_provider": provider_name,
        "timezone": str(config.TZ),
        "current_rate_per_kwh": f"${current_rate:.5f}",
        "summer_rates": summer_rates,
        "winter_rates": winter_rates,
        "weekday_schedule": {
            "off_peak": "12:00 AM - 1:00 PM and 11:00 PM - 12:00 AM",
            "partial_peak_1": "1:00 PM - 5:00 PM",
            "on_peak": "5:00 PM - 8:00 PM",
            "partial_peak_2": "8:00 PM - 11:00 PM"
        },
        "weekend_schedule": {
            "off_peak": "All day on weekends and holidays"
        },
        "night_blackout_window": f"{config.NIGHT_BLACKOUT_START_HOUR}:00 - {config.NIGHT_BLACKOUT_END_HOUR}:00",
        "night_blackout_description": f"No EV charging between {config.NIGHT_BLACKOUT_START_HOUR}:00 and {config.NIGHT_BLACKOUT_END_HOUR}:00 on weekdays"
    }
    return json.dumps(schedule_info)

def get_tesla_powerwall_status() -> str:
    """Gets detailed stats about the Tesla Powerwall, including current battery level (SoC %), charge/discharge power (kW), solar generation (kW), home usage (kW), grid export/import (kW), self-powered percentage, and grid connection status."""
    try:
        pw = get_powerwall_stats()
        log.info(f"DEBUG: get_tesla_powerwall_status result: {pw}")
        return json.dumps(pw)
    except Exception as e:
        log.warning(f"Bot failed to get Powerwall stats: {e}")
        return json.dumps({"error": f"Failed to get Powerwall stats: {e}"})

def read_application_logs(num_lines: int = 50) -> str:
    """Reads the last N lines of the application log file (logs/charger.log) to check for warnings, errors, or execution history.
    
    Args:
        num_lines: Number of trailing lines to read (default 50).
    """
    log_file = config.TEXT_LOG_FILE
    if not os.path.exists(log_file):
        return f"Log file {log_file} does not exist yet."
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
            last_lines = lines[-num_lines:]
            return "".join(last_lines)
    except Exception as e:
        return f"Failed to read logs: {e}"

def set_battery_thresholds(start_pct: float, stop_pct: float) -> str:
    """Updates the battery start and stop percentage thresholds.
    
    Args:
        start_pct: The battery level (%) required to start/resume charging.
        stop_pct: The battery level (%) at which charging is stopped to protect home power.
    """
    try:
        start_pct = float(start_pct)
        stop_pct = float(stop_pct)
    except (ValueError, TypeError):
        return "Error: Invalid numeric value for battery thresholds."

    config.BATTERY_START_PCT = start_pct
    config.BATTERY_STOP_PCT = stop_pct
    config.save_dynamic_config()
    
    if RUN_CYCLE_CALLBACK:
        threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
        
    return f"Success: Set battery start threshold to {start_pct}% and stop threshold to {stop_pct}%."

def set_blackout_hours(start_hour: int, end_hour: int) -> str:
    """Updates PG&E TOU night blackout start and end hours (24-hour format).
    
    Args:
        start_hour: Hour (0-23) when night blackout begins (default 16 for 4 PM).
        end_hour: Hour (0-23) when night blackout ends (default 9 for 9 AM).
    """
    try:
        start_hour = int(start_hour)
        end_hour = int(end_hour)
    except (ValueError, TypeError):
        return "Error: Invalid hour values for blackout window."

    config.NIGHT_BLACKOUT_START_HOUR = start_hour
    config.NIGHT_BLACKOUT_END_HOUR = end_hour
    config.save_dynamic_config()
    
    if RUN_CYCLE_CALLBACK:
        threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
        
    return f"Success: Set night blackout window to {start_hour}:00 - {end_hour}:00."

def set_override_mode(mode: str) -> str:
    """Controls the automation override mode.
    
    Args:
        mode: Either 'manual' (forces manual override, pausing solar automation) or 'auto' (enables automatic solar tracking).
    """
    if mode.lower() not in ["manual", "auto"]:
        return "Error: Mode must be 'manual' or 'auto'."
    config.MANUAL_MODE_OVERRIDE = mode.lower()
    config.save_dynamic_config()
    if mode.lower() == "auto":
        state.clear_manual_guards()
        try:
            set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
        except Exception:
            pass
    
    if RUN_CYCLE_CALLBACK:
        threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
        
    return f"Success: Configured override mode to '{mode}'."

def start_charging(amperage: int = 20, stop_battery_pct: float = None, stop_at_hour: int = None, duration_hours: float = None) -> str:
    """Immediately forces the charger to start charging at specified amperage (8-32A), with optional safety guardrails to automatically stop and return to Auto mode if battery drops or cutoff time/duration is reached.
    
    Args:
        amperage: The current limit in Amps (default is 20A for normal power, set to 32A for full/max power, range 8-32).
        stop_battery_pct: Optional battery percentage below which charging will automatically stop. Only pass if the user explicitly requested a stop limit.
        stop_at_hour: Optional hour in 24-hour format (0-23, e.g. 16 for 4 PM / 16:00) when charging will automatically stop. Only pass if explicitly requested.
        duration_hours: Optional max duration in hours (e.g. 2.0) to run manual charge before automatically stopping. Only pass if explicitly requested.
    """
    try:
        if amperage is not None:
            try:
                amperage = int(amperage)
            except (ValueError, TypeError):
                amperage = config.DEFAULT_CHARGER_AMPERAGE
        else:
            amperage = config.DEFAULT_CHARGER_AMPERAGE

        if amperage < 8 or amperage > 32:
            amperage = config.DEFAULT_CHARGER_AMPERAGE

        # Configure guardrails in state
        if stop_battery_pct is not None:
            try:
                state.manual_guard_stop_battery_pct = float(stop_battery_pct)
            except (ValueError, TypeError):
                state.manual_guard_stop_battery_pct = None
        else:
            state.manual_guard_stop_battery_pct = None

        if stop_at_hour is not None:
            try:
                state.manual_guard_stop_at_hour = int(stop_at_hour)
            except (ValueError, TypeError):
                state.manual_guard_stop_at_hour = None
        else:
            state.manual_guard_stop_at_hour = None

        if duration_hours is not None:
            try:
                state.manual_guard_stop_time = datetime.now(config.TZ) + timedelta(hours=float(duration_hours))
            except (ValueError, TypeError):
                state.manual_guard_stop_time = None
        else:
            state.manual_guard_stop_time = None

        # Start physically
        start_charger(amperage)
        
        # Sync in-memory state
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ)
        state.active_amperage = amperage
        state.session_count_today += 1
        state.session_stop_reason = None
        
        # Save override state so it stays running
        config.MANUAL_MODE_OVERRIDE = "manual"
        config.save_dynamic_config()
        
        guards_desc = []
        if state.manual_guard_stop_battery_pct is not None:
            guards_desc.append(f"stop if battery < {state.manual_guard_stop_battery_pct}%")
        if state.manual_guard_stop_at_hour is not None:
            guards_desc.append(f"stop at {state.manual_guard_stop_at_hour}:00")
        if state.manual_guard_stop_time is not None:
            guards_desc.append(f"stop at {state.manual_guard_stop_time.strftime('%H:%M')}")
            
        guards_msg = f" (Guards: {', '.join(guards_desc)})" if guards_desc else ""
        notify(f"🟢 Charging started (Forced manually via Telegram at {amperage}A{guards_msg})")
        
        if RUN_CYCLE_CALLBACK:
            threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
            
        guards_summary = ", ".join(guards_desc) if guards_desc else "Default blackout/battery limits"
        return f"Success: Sent start command to charger at {amperage}A. Active Guardrails: {guards_summary}. Switched mode to Manual override."
    except Exception as e:
        return f"Error starting charger: {e}"

def stop_charging() -> str:
    """Immediately forces the charger to stop charging."""
    try:
        # Stop physically
        stop_charger()
        try:
            set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
        except Exception:
            pass
        
        # Sync in-memory state and clear guards
        state.charger_state = state.State.IDLE
        state.charge_session_start = None
        state.session_stop_reason = "Stopped manually via Telegram bot"
        state.clear_manual_guards()
        
        # Save override state so it stays stopped
        config.MANUAL_MODE_OVERRIDE = "manual"
        config.save_dynamic_config()
        
        notify("🔴 Charging stopped (Forced manually via Telegram)")
        
        if RUN_CYCLE_CALLBACK:
            threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
            
        return "Success: Sent stop command to charger. Switched mode to Manual override to prevent automatic restart."
    except Exception as e:
        return f"Error stopping charger: {e}"

def set_charger_amperage(amperage: int) -> str:
    """Updates the charger's current amperage limit dynamically (between 8 and 32 Amps).
    
    Args:
        amperage: The current limit to set in Amps (must be between 8 and 32).
    """
    try:
        amperage = int(amperage)
    except (ValueError, TypeError):
        return "Error: Amperage must be a valid integer between 8 and 32."

    if amperage < 8 or amperage > 32:
        return "Error: Amperage limit must be between 8 and 32 Amps."
    try:
        set_charger_amperage_limit(amperage)
        state.active_amperage = amperage
        return f"Success: Charger amperage limit set to {amperage}A."
    except Exception as e:
        return f"Error setting amperage: {e}"

def manage_custom_alert(action: str = "list", field: str = "battery_pct", operator: str = "gte", value: float = 80.0, message: str = "", alert_id: str = "") -> str:
    """Manages custom dynamic notification alerts (add new alert, list active alerts, or remove existing alert).
    
    Args:
        action: One of 'add' (create alert), 'list' (view all active alerts), or 'remove' (delete an alert).
        field: Metric to monitor when adding ('battery_pct', 'solar_kw', 'home_kw', 'grid_kw', 'charging_status', 'is_plugged_in', 'log_errors').
        operator: Comparison operator when adding ('gte', 'gt', 'lte', 'lt', 'eq', 'contains').
        value: Numeric threshold value for comparison when adding (e.g. 80.0).
        message: Custom alert message to send when condition triggers.
        alert_id: 8-character ID of alert to remove (when action is 'remove').
    """
    act = (action or "list").lower().strip()
    if act in ("add", "create", "set"):
        return add_alert(field=field, operator=operator, value=value, message=message, once=True)
    elif act in ("remove", "delete", "clear"):
        return remove_alert(alert_id)
    else:
        return list_alerts()

def set_custom_alert(field: str, operator: str, value: float, message: str, once: bool = True) -> str:
    """Sets a dynamic notification alert when a metric condition is met."""
    return add_alert(field, operator, value, message, once)

def clear_custom_alert(alert_id: str) -> str:
    """Clears/removes an active custom alert by its 8-character ID."""
    return remove_alert(alert_id)

def list_custom_alerts() -> str:
    """Returns a list of all currently active custom alerts."""
    return list_alerts()

def get_daily_charging_cost(date_or_period: str = "today") -> str:
    """Calculates total energy drawn from grid (kWh), solar energy used (kWh), total energy added (kWh), estimated miles added (driving range), and total cost ($) for EV charging for a period or date.
    Use this tool whenever the user asks how many miles were added, how much range was added today, yesterday, this week, this month, or on a specific date, how much it cost to charge the car, or how many grid units were pulled.
    
    Args:
        date_or_period: Time period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD' (defaults to 'today').
    """
    data = calc_cost(period=date_or_period)
    return json.dumps(data)

def get_home_energy_summary(date_or_period: str = "today") -> str:
    """Calculates total home electricity consumption (kWh), solar generated (kWh), grid energy imported (kWh), total electricity bill cost ($), and breakdown between EV charging vs home appliances for a given period or date.
    Use this tool whenever the user asks how much the home consumed today, how much the total home electricity costed today, this week, this month, or yesterday.
    
    Args:
        date_or_period: Time period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD' (defaults to 'today').
    """
    data = calc_home_summary(period=date_or_period)
    return json.dumps(data)

def get_energy_saving_advice() -> str:
    """Analyzes recent 7-day power usage logs to calculate peak solar generation windows, identify high-cost grid draws, and provide actionable recommendations to reduce utility bills.
    Use this tool whenever the user asks for suggestions or advice on how to reduce their bill, when to run heavy appliances, or when to charge the car.
    """
    data = calc_advice()
    return json.dumps(data)

def add_agent_instruction(text: str) -> str:
    """Saves a special note or override instruction for the Daily AI Agent."""
    success = add_user_instruction(text)
    if success:
        return f"Success: Saved instruction '{text}' for the Daily AI Agent. It will process this at midnight."
    else:
        return "Error: Failed to save instruction. Please check Google Sheets integration."

def trigger_daily_agent() -> str:
    """Manually runs the Daily AI Agent planner to analyze recent solar generation, optimize the charge window, update battery thresholds, and dispatch the daily strategy update.
    Use this tool whenever the user asks to run the daily agent, trigger daily AI, plan today's charging, or generate the daily update.
    """
    from agent.daily_agent import run_daily_agent
    try:
        run_daily_agent()
        return "Success: Triggered Daily AI Agent. The daily plan and settings have been updated and sent to your Telegram."
    except Exception as e:
        return f"Error executing Daily AI Agent: {e}"

_last_generated_image_path = None

def generate_monthly_report(period: str = "last_month") -> str:
    """Generates a high-resolution PNG monthly electricity utility bill report graphic for any given month (e.g. 'last_month', 'this_month', 'June', 'June 2026', 'July 2026', or 'YYYY-MM').
    Plots daily usage dates against variable grid electricity cost (EXCLUDING fixed daily connection fee) and solar generation, plus utility bill breakdown.
    Use this tool whenever the user asks for a monthly bill report, monthly usage graph, or monthly electricity bill PNG image for any month.
    """
    global _last_generated_image_path
    data = get_monthly_billing_data(period=period)
    if "error" in data:
        return json.dumps({"error": data["error"]})

    img_path = generate_monthly_report_image(period=period)
    if img_path and os.path.exists(img_path):
        _last_generated_image_path = img_path
        return json.dumps({"status": "success", "image_path": img_path, "note": "Monthly report PNG image generated successfully."})
    else:
        return json.dumps({"error": "Failed to generate monthly report image."})

def send_monthly_telegram_report(period: str = "last_month"):
    """Triggered on 1st of every month to send the monthly report image to Telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_ALLOWED_USER_ID:
        return
    try:
        img_path = generate_monthly_report_image(period=period)
        if img_path and os.path.exists(img_path):
            bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
            with open(img_path, 'rb') as f:
                bot.send_photo(config.TELEGRAM_ALLOWED_USER_ID, photo=f, caption="📊 <b>Monthly Utility & Energy Bill Briefing</b>", parse_mode="HTML")
            log.info("Successfully sent monthly report image to Telegram user.")
    except Exception as e:
        log.error(f"Failed to send monthly report to Telegram: {e}")

def clean_telegram_html(text: str) -> str:
    """Cleans and sanitizes LLM response outputs into valid Telegram HTML format."""
    if not text:
        return ""
    
    # Convert Markdown bold (**text**) -> <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # Convert Markdown inline code (`code`) -> <code>code</code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    
    # Convert Markdown italic only when surrounded by whitespace or string boundaries
    text = re.sub(r'(?<!\w)\*([^\*]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', text)
    
    # Convert bullet markers at line starts (* or - ) to unicode bullet •
    text = re.sub(r'^[ \t]*[\*\-][ \t]+', '• ', text, flags=re.MULTILINE)
    
    # Strip unsupported HTML tags (ul, ol, li, p, br, div, span, etc.)
    text = re.sub(r'</?(?:ul|ol|li|p|br|div|span|header|footer|section|h[1-6]|table|tr|td|th)[^>]*>', '\n', text, flags=re.IGNORECASE)
    
    # Normalize multi-newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

# ── 2. Model Agnostic LLM Response Handler ─────────────────────────────────

chat_history = []

def handle_message_with_llm(text: str) -> str:
    """Model agnostic handler for Telegram messages."""
    log.info(f"DEBUG: LLM input text: '{text}'")
    llm_cfg = llm_client.resolve_llm_config()
    if not llm_cfg.get("api_key"):
        return f"LLM API key is not configured for provider '{llm_cfg.get('provider')}'. Please set environment variables."

    global chat_history
    
    if text.strip().lower() in ["/clear", "/reset"]:
        chat_history = []
        log.info("DEBUG: LLM chat history cleared.")
        return "Conversation history cleared."

    tools = [
        get_system_status,
        get_recent_charging_sessions,
        get_daily_charging_cost,
        get_home_energy_summary,
        get_energy_saving_advice,
        generate_monthly_report,
        get_tou_schedule,
        read_application_logs,
        read_source_code,
        set_battery_thresholds,
        set_blackout_hours,
        set_override_mode,
        start_charging,
        stop_charging,
        set_charger_amperage,
        manage_custom_alert,
        switch_llm_model,
        trigger_daily_agent,
        add_agent_instruction,
        run_antigravity_dev_task,
        restart_and_update_application
    ]

    system_instruction = (
        "You are an AI assistant for a Smart EV Charger. "
        "You can query real-time system status (Powerwall battery SoC, solar, house load, grid, charger state), "
        "check recent charging session history (when charging stopped and why), "
        "calculate daily/weekly/monthly EV charging cost, energy (kWh), miles added (range), and total home energy consumption/cost, "
        "provide personalized energy-saving advice and appliance scheduling recommendations based on solar logs, "
        "check TOU rate schedules, modify thresholds (battery levels, blackout hours), "
        "manage custom notification alerts (add, list, remove), "
        "run the Daily AI Agent on demand to optimize charging strategy for today, "
        "or force start/stop charging (setting 32A when user asks for full/max power, or 20A for default; only pass stop_battery_pct, stop_at_hour, or duration_hours if the user EXPLICITLY requested them in their prompt) by calling tools. "
        "When the user asks you to inspect or check the AI model or switch provider (e.g. switch to nvidia, gemini, nemotron, claude, etc.), call the 'switch_llm_model' tool. "
        "When the user asks you to investigate an issue, fix a bug in the code, open a Pull Request, or update an existing GitHub PR with changes, call the 'run_antigravity_dev_task' tool to dispatch the autonomous developer agent. "
        "When the user asks you to view or read the application source code, call the 'read_source_code' tool. \n"
        "When the user asks you to restart the app, reload, or pull the latest code updates, call the 'restart_and_update_application' tool. "
        "Always run the appropriate tools when requested, and summarize the actions taken "
        "in a friendly natural language response. "
        "Format all your responses in the strict HTML subset supported by Telegram. "
        "You may only use: <b>, <i>, <u>, <s>, <code>, <pre>, and <blockquote> tags. "
        "WARNING: Telegram does NOT support <ul>, <ol>, <li>, <p>, or <br> tags. "
        "To make lists or line breaks, simply use plain text list characters (like bullets • or -) and standard newlines (\\n). "
        "Never use Markdown markers like * or ** or _ or ` in your output. "
        "If the user asks general questions, just reply politely without calling tools."
    )

    response_text, updated_history = llm_client.chat_with_tools(
        history=chat_history,
        user_text=text,
        tools=tools,
        system_instruction=system_instruction
    )
    chat_history = updated_history
    if len(chat_history) > 20:
        chat_history = chat_history[-20:]
    log.info(f"DEBUG: LLM response: {response_text}")
    return response_text


# ── 3. Telegram Polling Loop ────────────────────────────────────────────────

def _bot_polling_loop():
    bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
    
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        if config.TELEGRAM_ALLOWED_USER_ID:
            if message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
                bot.reply_to(message, "Unauthorized.")
                return
                
        help_text = (
            "🔋 <b>Welcome to the Smart EV Charger Assistant!</b>\n\n"
            "I'm powered by AI and can help you control your solar charger. You can text me in natural language, for example:\n"
            "• <i>'Run daily agent'</i> or <i>'Plan today's charging'</i>\n"
            "• <i>'Why did charging stop last time?'</i>\n"
            "• <i>'What are the peak and partial peak timings?'</i>\n"
            "• <i>'Switch model to nvidia'</i> or <i>'Use gemini pro'</i>\n"
            "• <i>'Charge with full power'</i>\n"
            "• <i>'Stop charging when battery goes below 40%'</i>\n"
            "• <i>'Turn on manual mode'</i>\n"
            "• <i>'Force start the charger'</i>\n"
            "• <i>'/model'</i> to view or switch active AI models\n"
            "• <i>'/update'</i> or <i>'/restart'</i> to pull latest code and restart"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(commands=['model', 'models'])
    def handle_model_command(message):
        if config.TELEGRAM_ALLOWED_USER_ID and message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
            bot.reply_to(message, "Unauthorized.")
            return
        text = (message.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            current_p = getattr(config, "LLM_PROVIDER", "gemini")
            current_m = getattr(config, "LLM_MODEL", "gemini-2.5-flash")
            msg = (
                f"🧠 <b>Active AI Model Configuration</b>\n\n"
                f"• <b>Provider</b>: <code>{current_p}</code>\n"
                f"• <b>Model</b>: <code>{current_m}</code>\n\n"
                f"<b>Quick Switch Commands:</b>\n"
                f"• <code>/model gemini</code> (Gemini 2.5 Flash)\n"
                f"• <code>/model gemini-pro</code> (Gemini 2.5 Pro Deep Reasoning)\n"
                f"• <code>/model nvidia</code> (Nemotron 30B)\n"
                f"• <code>/model openai</code> (GPT-4o)\n"
                f"• <code>/model claude</code> (Claude 3.7 Sonnet)\n\n"
                f"<i>Or just ask in chat: 'Switch model to nvidia'</i>"
            )
            bot.reply_to(message, msg, parse_mode="HTML")
            return

        target = parts[1].lower().strip()
        if target in ("gemini", "google"):
            res = switch_llm_model("gemini", "gemini-2.5-flash")
        elif target in ("gemini-pro", "gemini_pro", "gemini-2.5-pro", "pro"):
            res = switch_llm_model("gemini", "gemini-2.5-pro")
        elif target in ("nvidia", "nemotron"):
            res = switch_llm_model("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")
        elif target in ("openai", "chatgpt", "gpt"):
            res = switch_llm_model("openai", "gpt-4o")
        elif target in ("claude", "anthropic"):
            res = switch_llm_model("anthropic", "claude-3-7-sonnet")
        else:
            res = switch_llm_model(target, parts[2] if len(parts) > 2 else None)
        bot.reply_to(message, res, parse_mode="HTML")

    @bot.message_handler(commands=['restart', 'update'])
    def restart_cmd(message):
        if config.TELEGRAM_ALLOWED_USER_ID and message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
            bot.reply_to(message, "Unauthorized.")
            return
        bot.reply_to(message, "🔄 <b>Restarting container...</b>\nPulling latest code from GitHub and restarting. Will be back online in ~5-10s!", parse_mode="HTML")
        def _delayed_exit():
            import time
            time.sleep(2)
            os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()

    @bot.message_handler(commands=['daily_agent', 'plan', 'daily_plan'])
    def run_daily_agent_cmd(message):
        if config.TELEGRAM_ALLOWED_USER_ID and message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
            bot.reply_to(message, "Unauthorized.")
            return
        bot.send_chat_action(message.chat.id, 'typing')
        from agent.daily_agent import run_daily_agent
        try:
            run_daily_agent()
            bot.reply_to(message, "✅ <b>Daily AI Agent executed successfully.</b> Check the update above for today's optimal schedule and thresholds!", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ <b>Error executing Daily AI Agent:</b> {e}", parse_mode="HTML")

    @bot.message_handler(commands=['monthly_report', 'bill', 'monthly_bill'])
    def send_monthly_report_cmd(message):
        if config.TELEGRAM_ALLOWED_USER_ID and message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
            bot.reply_to(message, "Unauthorized.")
            return
        bot.send_chat_action(message.chat.id, 'upload_photo')
        args = (message.text or "").split(maxsplit=1)
        period = args[1].strip() if len(args) > 1 else 'last_month'
        img_path = generate_monthly_report_image(period)
        if img_path and os.path.exists(img_path):
            with open(img_path, 'rb') as f:
                bot.send_photo(message.chat.id, photo=f, caption="⚡ <b>Monthly Electricity & Utility Bill Report</b>", parse_mode="HTML")
        else:
            bot.reply_to(message, f"Error: Could not generate monthly report image for '{period}'.")

    @bot.message_handler(func=lambda message: True)
    def handle_incoming_message(message):
        if config.TELEGRAM_ALLOWED_USER_ID:
            if message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
                bot.reply_to(message, "Unauthorized: You are not allowed to control this EV Charger.")
                return
                
        user_text = message.text
        bot.send_chat_action(message.chat.id, 'typing')
        
        try:
            global _last_generated_image_path
            _last_generated_image_path = None

            raw_response = handle_message_with_llm(user_text)
            response_html = clean_telegram_html(raw_response)
            
            if response_html and response_html.strip():
                try:
                    bot.reply_to(message, response_html, parse_mode="HTML")
                except telebot.apihelper.ApiTelegramException as api_err:
                    log.warning(f"Telegram HTML parse error: {api_err}. Falling back to plain text reply.")
                    plain_text = re.sub(r'<[^>]+>', '', raw_response).strip()
                    if plain_text:
                        bot.reply_to(message, plain_text, parse_mode=None)
            elif not _last_generated_image_path:
                bot.reply_to(message, "Processed request.", parse_mode=None)

            if _last_generated_image_path and os.path.exists(_last_generated_image_path):
                try:
                    with open(_last_generated_image_path, 'rb') as f:
                        bot.send_photo(message.chat.id, photo=f, caption="⚡ <b>Monthly Electricity & Utility Bill Report</b>", parse_mode="HTML")
                    log.info(f"Successfully uploaded report photo to Telegram: {_last_generated_image_path}")
                except Exception as img_err:
                    log.error(f"Failed to send report photo to Telegram: {img_err}")
                _last_generated_image_path = None

        except Exception as e:
            log.error(f"Telegram Bot error processing LLM request: {e}", exc_info=True)
            bot.reply_to(message, f"Sorry, I encountered an error: {e}")

    log.info("Telegram Bot starting infinity polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=20, logger_level=logging.WARNING)
        except Exception as poll_err:
            log.warning(f"Telegram polling interrupted ({poll_err}). Reconnecting in 5 seconds...")
            import time
            time.sleep(5)

def start_telegram_bot(run_cycle_callback):
    global RUN_CYCLE_CALLBACK
    RUN_CYCLE_CALLBACK = run_cycle_callback
    
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not configured. Telegram bot disabled.")
        return
        
    thread = threading.Thread(target=_bot_polling_loop, daemon=True)
    thread.start()
    log.info("Telegram Bot thread started successfully.")
