import os
import json
import re
import threading
import logging
from datetime import datetime

import telebot
from google import genai
from google.genai import types

import config
import state
from logger import log
from api_netzero import get_powerwall_stats
from api_chargepoint import get_charger_status as get_cp_status
from csv_logger import get_session_minutes, get_recent_sessions, get_daily_charging_cost as calc_daily_cost

RUN_CYCLE_CALLBACK = None

# ── 1. Helper Tools for Gemini Function Calling ─────────────────────────────

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
        
    status_data = {
        "charger_state": str(state.charger_state),
        "session_duration_minutes": round(get_session_minutes(), 1),
        "previous_session_stop_reason": getattr(state, "session_stop_reason", "N/A"),
        "battery_pct": pw.get("battery_pct"),
        "solar_kw": pw.get("solar_kw"),
        "home_kw": pw.get("home_kw"),
        "surplus_kw": pw.get("solar_surplus_kw"),
        "grid_kw": pw.get("grid_kw"),
        "island_mode": pw.get("island_mode"),
        "storm_mode": pw.get("storm_mode"),
        "charging_status": cp.get("charging_status"),
        "is_plugged_in": cp.get("is_plugged_in"),
        "is_connected": cp.get("is_connected"),
        "amperage_limit": cp.get("amperage_limit"),
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
    """Gets details about PG&E Time-Of-Use (TOU) electricity rate periods, night blackout windows, peak/partial-peak/off-peak hours, and rates."""
    from tou import get_tou_rate
    now = datetime.now(config.TZ)
    current_rate = get_tou_rate(now)
    schedule_info = {
        "timezone": str(config.TZ),
        "current_rate_per_kwh": f"${current_rate:.5f}",
        "summer_rates": {
            "off_peak": "$0.14513/kWh",
            "partial_peak": "$0.20192/kWh",
            "on_peak": "$0.31235/kWh"
        },
        "winter_rates": {
            "off_peak": "$0.14324/kWh",
            "partial_peak": "$0.14324/kWh",
            "on_peak": "$0.22401/kWh"
        },
        "weekday_schedule": {
            "off_peak": "12:00 AM - 1:00 PM and 11:00 PM - 12:00 AM",
            "partial_peak_1": "1:00 PM - 5:00 PM",
            "on_peak": "5:00 PM - 8:00 PM",
            "partial_peak_2": "8:00 PM - 11:00 PM"
        },
        "weekend_schedule": {
            "off_peak": "All day on weekends and PG&E holidays"
        },
        "night_blackout_window": f"{config.NIGHT_BLACKOUT_START_HOUR}:00 - {config.NIGHT_BLACKOUT_END_HOUR}:00",
        "night_blackout_description": f"No EV charging between {config.NIGHT_BLACKOUT_START_HOUR}:00 (4 PM) and {config.NIGHT_BLACKOUT_END_HOUR}:00 (9 AM) on weekdays"
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
    
    if RUN_CYCLE_CALLBACK:
        threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
        
    return f"Success: Configured override mode to '{mode}'."

def start_charging(amperage: int = 20) -> str:
    """Immediately forces the charger to start charging, bypassing solar/battery rules.
    
    Args:
        amperage: The current limit to set in Amps (default is 20A for normal power, set to 32A for full/max power, range 8-32).
    """
    from api_chargepoint import start_charger
    from notifications import notify
    try:
        if amperage < 8 or amperage > 32:
            amperage = config.DEFAULT_CHARGER_AMPERAGE

        # Start physically
        start_charger(amperage)
        
        # Sync in-memory state
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ)
        state.session_count_today += 1
        state.session_stop_reason = None
        
        # Save override state so it stays running
        config.MANUAL_MODE_OVERRIDE = "manual"
        config.save_dynamic_config()
        
        notify(f"🟢 Charging started (Forced manually via Telegram at {amperage}A)")
        
        if RUN_CYCLE_CALLBACK:
            threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
            
        return f"Success: Sent start command to charger at {amperage}A. Switched mode to Manual override to prevent automatic shutdown."
    except Exception as e:
        return f"Error starting charger: {e}"

def stop_charging() -> str:
    """Immediately forces the charger to stop charging."""
    from api_chargepoint import stop_charger
    from notifications import notify
    try:
        # Stop physically
        stop_charger()
        
        # Sync in-memory state
        state.charger_state = state.State.IDLE
        state.charge_session_start = None
        state.session_stop_reason = "Stopped manually via Telegram bot"
        
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
    from api_chargepoint import set_charger_amperage_limit
    if amperage < 8 or amperage > 32:
        return "Error: Amperage limit must be between 8 and 32 Amps."
    try:
        set_charger_amperage_limit(amperage)
        return f"Success: Charger amperage limit set to {amperage}A."
    except Exception as e:
        return f"Error setting amperage: {e}"

def set_custom_alert(field: str, operator: str, value: float, message: str, once: bool = True) -> str:
    """Sets a dynamic notification alert when a metric condition is met."""
    from alerts import add_alert
    return add_alert(field, operator, value, message, once)

def clear_custom_alert(alert_id: str) -> str:
    """Clears/removes an active custom alert by its 8-character ID."""
    from alerts import remove_alert
    return remove_alert(alert_id)

def list_custom_alerts() -> str:
    """Returns a list of all currently active custom alerts."""
    from alerts import list_alerts
    return list_alerts()

def get_daily_charging_cost(date_or_period: str = "today") -> str:
    """Calculates total energy drawn from grid (kWh), solar energy used (kWh), and total cost ($) for EV charging for a period or date.
    Use this tool whenever the user asks how much it cost to charge the car today, yesterday, this week, this month, or on a specific date, or how many grid units were pulled.
    
    Args:
        date_or_period: Time period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD' (defaults to 'today').
    """
    from csv_logger import get_daily_charging_cost as calc_cost
    data = calc_cost(period=date_or_period)
    return json.dumps(data)


def get_home_energy_summary(date_or_period: str = "today") -> str:
    """Calculates total home electricity consumption (kWh), solar generated (kWh), grid energy imported (kWh), total electricity bill cost ($), and breakdown between EV charging vs home appliances for a given period or date.
    Use this tool whenever the user asks how much the home consumed today, how much the total home electricity costed today, this week, this month, or yesterday.
    
    Args:
        date_or_period: Time period or date, e.g. 'today', 'yesterday', 'this_week', 'this_month', or 'YYYY-MM-DD' (defaults to 'today').
    """
    from csv_logger import get_home_energy_summary as calc_home_summary
    data = calc_home_summary(period=date_or_period)
    return json.dumps(data)

def get_energy_saving_advice() -> str:
    """Analyzes recent 7-day power usage logs to calculate peak solar generation windows, identify high-cost grid draws, and provide actionable recommendations to reduce utility bills.
    Use this tool whenever the user asks for suggestions or advice on how to reduce their bill, when to run heavy appliances, or when to charge the car.
    """
    from csv_logger import get_energy_saving_advice as calc_advice
    data = calc_advice()
    return json.dumps(data)

def add_agent_instruction(text: str) -> str:
    """Saves a special note or override instruction for the Daily AI Agent."""
    from sheets_db import add_user_instruction
    success = add_user_instruction(text)
    if success:
        return f"Success: Saved instruction '{text}' for the Daily AI Agent. It will process this at midnight."
    else:
        return "Error: Failed to save instruction. Please check Google Sheets integration."

def clean_telegram_html(text: str) -> str:
    """Cleans and sanitizes Gemini outputs into valid Telegram HTML format."""
    if not text:
        return ""
    
    # Convert Markdown bold (**text** or __text__) -> <b>text</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    
    # Convert Markdown italic (*text* or _text_) -> <i>text</i>
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
    
    # Convert Markdown inline code (`code`) -> <code>code</code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    
    # Convert bullet markers at line starts (* or - ) to unicode bullet •
    text = re.sub(r'^[ \t]*[\*\-][ \t]+', '• ', text, flags=re.MULTILINE)
    
    # Strip unsupported HTML tags (ul, ol, li, p, br, div, span, etc.)
    text = re.sub(r'</?(?:ul|ol|li|p|br|div|span|header|footer|section|h[1-6]|table|tr|td|th)[^>]*>', '\n', text, flags=re.IGNORECASE)
    
    # Normalize multi-newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

# ── 2. Gemini Response Handler ──────────────────────────────────────────────

gemini_client = None
gemini_chat = None

def handle_message_with_gemini(text: str) -> str:
    log.info(f"DEBUG: Gemini input text: '{text}'")
    if not config.GEMINI_API_KEY:
        return "Gemini API key is not configured. Please add GEMINI_API_KEY to your env variables."
        
    global gemini_client, gemini_chat
    
    if text.strip().lower() in ["/clear", "/reset"]:
        gemini_chat = None
        log.info("DEBUG: Gemini chat session cleared.")
        return "Conversation history cleared."
        
    if gemini_client is None:
        gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    
    tools = [
        get_system_status,
        get_recent_charging_sessions,
        get_daily_charging_cost,
        get_home_energy_summary,
        get_energy_saving_advice,
        get_tou_schedule,
        get_tesla_powerwall_status,
        read_application_logs,
        set_battery_thresholds,
        set_blackout_hours,
        set_override_mode,
        start_charging,
        stop_charging,
        set_charger_amperage,
        set_custom_alert,
        clear_custom_alert,
        list_custom_alerts,
        add_agent_instruction
    ]
    
    if gemini_chat is None:
        log.info("DEBUG: Initializing new Gemini chat session.")
        gemini_chat = gemini_client.chats.create(
            model=config.GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are an AI assistant for a Smart EV Charger. "
                    "You can query status, check recent charging session history (when charging stopped and why), "
                    "calculate daily/weekly/monthly EV charging cost and total home energy consumption/cost, "
                    "provide personalized energy-saving advice and appliance scheduling recommendations based on solar logs, "
                    "check TOU rate schedules, modify thresholds (battery levels, blackout hours), "
                    "or force start/stop charging (setting 32A when user asks for full/max power, or 20A for default) by calling tools. "
                    "Always run the appropriate tools when requested, and summarize the actions taken "
                    "in a friendly natural language response. "
                    "Format all your responses in the strict HTML subset supported by Telegram. "
                    "You may only use: <b>, <i>, <u>, <s>, <code>, <pre>, and <blockquote> tags. "
                    "WARNING: Telegram does NOT support <ul>, <ol>, <li>, <p>, or <br> tags. "
                    "To make lists or line breaks, simply use plain text list characters (like bullets • or -) and standard newlines (\\n). "
                    "Never use Markdown markers like * or ** or _ or ` in your output. "
                    "If the user asks general questions, just reply politely without calling tools."
                ),
                tools=tools,
                temperature=0.0
            )
        )



        
    response = gemini_chat.send_message(text)
    log.info(f"DEBUG: Gemini response: {response}")
    return response.text

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
            "I'm powered by Gemini and can help you control your solar charger. You can text me in natural language, for example:\n"
            "• <i>'Why did charging stop last time?'</i>\n"
            "• <i>'What are the peak and partial peak timings?'</i>\n"
            "• <i>'Charge with full power'</i>\n"
            "• <i>'Stop charging when battery goes below 40%'</i>\n"
            "• <i>'Turn on manual mode'</i>\n"
            "• <i>'Force start the charger'</i>"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(func=lambda message: True)
    def handle_incoming_message(message):
        if config.TELEGRAM_ALLOWED_USER_ID:
            if message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
                bot.reply_to(message, "Unauthorized: You are not allowed to control this EV Charger.")
                return
                
        user_text = message.text
        bot.send_chat_action(message.chat.id, 'typing')
        
        try:
            raw_response = handle_message_with_gemini(user_text)
            response_html = clean_telegram_html(raw_response)
            try:
                bot.reply_to(message, response_html, parse_mode="HTML")
            except telebot.apihelper.ApiTelegramException as api_err:
                log.warning(f"Telegram HTML parse error: {api_err}. Falling back to plain text reply.")
                plain_text = re.sub(r'<[^>]+>', '', raw_response)
                bot.reply_to(message, plain_text, parse_mode=None)
        except Exception as e:
            log.error(f"Telegram Bot error processing Gemini request: {e}", exc_info=True)
            bot.reply_to(message, f"Sorry, I encountered an error: {e}")

    log.info("Telegram Bot starting infinity polling...")
    while True:
        try:
            bot.infinity_polling(timeout=50, long_polling_timeout=40, logger_level=logging.ERROR)
        except Exception as poll_err:
            log.error(f"Telegram polling encountered error: {poll_err}. Retrying polling in 5 seconds...")
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
