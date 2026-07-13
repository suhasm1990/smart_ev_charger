import os
import json
import threading
from datetime import datetime
import telebot
from google import genai
from google.genai import types

import config
import state
from logger import log
from api_netzero import get_powerwall_stats
from api_chargepoint import get_charger_status as get_cp_status
from csv_logger import get_session_minutes

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
        "config_battery_start_pct": config.BATTERY_START_PCT,
        "config_battery_stop_pct": config.BATTERY_STOP_PCT,
        "config_blackout_start_hour": config.NIGHT_BLACKOUT_START_HOUR,
        "config_blackout_end_hour": config.NIGHT_BLACKOUT_END_HOUR,
        "manual_mode_override": config.MANUAL_MODE_OVERRIDE,
    }
    log.info(f"DEBUG: get_system_status result: {status_data}")
    return json.dumps(status_data)

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
    
    # Trigger run cycle immediately in background to pick up changes
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

def start_charging() -> str:
    """Immediately forces the charger to start charging, bypassing solar/battery rules."""
    from api_chargepoint import start_charger
    from notifications import notify
    try:
        # Start physically
        start_charger()
        
        # Sync in-memory state
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ)
        state.session_count_today += 1
        state.session_stop_reason = None
        
        # Save override state so it stays running
        config.MANUAL_MODE_OVERRIDE = "manual"
        config.save_dynamic_config()
        
        notify("🟢 Charging started (Forced manually via Telegram)")
        
        if RUN_CYCLE_CALLBACK:
            threading.Thread(target=RUN_CYCLE_CALLBACK, daemon=True).start()
            
        return "Success: Sent start command to charger. Switched mode to Manual override to prevent automatic shutdown."
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

def set_custom_alert(field: str, operator: str, value: float, message: str, once: bool = True) -> str:
    """Sets a dynamic notification alert when a metric condition is met.
    
    Args:
        field: The system attribute to monitor. Must be one of:
               - 'battery_pct' (float, Powerwall SoC percentage, e.g., 75.5)
               - 'solar_kw' (float, solar generation in kW)
               - 'home_kw' (float, home power consumption in kW)
               - 'surplus_kw' (float, solar generation minus home usage in kW)
               - 'grid_kw' (float, grid draw in kW)
               - 'island_mode' (string, 'on_grid' or 'off_grid')
               - 'storm_mode' (boolean, true if Tesla storm watch mode is active)
               - 'charging_status' (string, 'CHARGING', 'AVAILABLE', etc.)
               - 'is_plugged_in' (boolean, true if vehicle is plugged in)
               - 'is_connected' (boolean, true if charger is connected)
               - 'log_errors' (boolean, true if error severity logs are parsed)
        operator: Comparison operator. Must be one of:
                  - 'eq' (equal to)
                  - 'ne' (not equal to)
                  - 'gt' (greater than)
                  - 'gte' (greater than or equal to)
                  - 'lt' (less than)
                  - 'lte' (less than or equal to)
        value: The target value to compare against. Strings, booleans, or floats.
        message: The notification text to push when the condition triggers.
        once: If true (default), the alert is removed after triggering once.
    """
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

# ── 2. Gemini Response Handler ──────────────────────────────────────────────

gemini_client = None
gemini_chat = None

def handle_message_with_gemini(text: str) -> str:
    log.info(f"DEBUG: Gemini input text: '{text}'")
    if not config.GEMINI_API_KEY:
        return "Gemini API key is not configured. Please add GEMINI_API_KEY to your env variables."
        
    global gemini_client, gemini_chat
    
    # Allow manual conversation history reset
    if text.strip().lower() in ["/clear", "/reset"]:
        gemini_chat = None
        log.info("DEBUG: Gemini chat session cleared.")
        return "Conversation history cleared."
        
    if gemini_client is None:
        gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    
    tools = [
        get_system_status,
        get_tesla_powerwall_status,
        read_application_logs,
        set_battery_thresholds,
        set_blackout_hours,
        set_override_mode,
        start_charging,
        stop_charging,
        set_custom_alert,
        clear_custom_alert,
        list_custom_alerts
    ]
    
    if gemini_chat is None:
        log.info("DEBUG: Initializing new Gemini chat session.")
        gemini_chat = gemini_client.chats.create(
            model=config.GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are an AI assistant for a Smart EV Charger. "
                    "You can query status, modify thresholds (battery levels, blackout hours), "
                    "or force start/stop charging by calling tools. "
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
        
    # Send message to chat session to preserve context
    response = gemini_chat.send_message(text)
    log.info(f"DEBUG: Gemini response: {response}")
    return response.text

# ── 3. Telegram Polling Loop ────────────────────────────────────────────────

def _bot_polling_loop():
    bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
    
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        # Security check
        if config.TELEGRAM_ALLOWED_USER_ID:
            if message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
                bot.reply_to(message, "Unauthorized.")
                return
                
        help_text = (
            "🔋 <b>Welcome to the Smart EV Charger Assistant!</b>\n\n"
            "I'm powered by Gemini and can help you control your solar charger. You can text me in natural language, for example:\n"
            "• <i>'How is my charger doing right now?'</i>\n"
            "• <i>'Stop charging when the battery goes below 40%'</i>\n"
            "• <i>'Turn on manual mode'</i>\n"
            "• <i>'Force start the charger'</i>\n"
            "• <i>'Force stop the charger'</i>\n"
            "• <i>'Change night blackout hours to 5pm to 8am'</i>"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(func=lambda message: True)
    def handle_incoming_message(message):
        # Security check
        if config.TELEGRAM_ALLOWED_USER_ID:
            if message.from_user.id != config.TELEGRAM_ALLOWED_USER_ID:
                bot.reply_to(message, "Unauthorized: You are not allowed to control this EV Charger.")
                return
                
        user_text = message.text
        bot.send_chat_action(message.chat.id, 'typing')
        
        try:
            response_text = handle_message_with_gemini(user_text)
            bot.reply_to(message, response_text, parse_mode="HTML")
        except Exception as e:
            log.error(f"Telegram Bot error processing Gemini request: {e}", exc_info=True)
            bot.reply_to(message, f"Sorry, I encountered an error: {e}")

    log.info("Telegram Bot starting infinity polling...")
    bot.infinity_polling(timeout=50, long_polling_timeout=40)

def start_telegram_bot(run_cycle_callback):
    global RUN_CYCLE_CALLBACK
    RUN_CYCLE_CALLBACK = run_cycle_callback
    
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("Telegram Bot Token is missing, bot will not start.")
        return
        
    t = threading.Thread(target=_bot_polling_loop, daemon=True, name="TelegramBot")
    t.start()
    log.info("Telegram Bot thread started successfully.")
