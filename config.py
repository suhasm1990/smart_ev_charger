import os
import json
from datetime import date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# NetZero Energy API
NETZERO_SITE_ID   = os.getenv("NETZERO_SITE_ID", "")
NETZERO_API_TOKEN = os.getenv("NETZERO_API_TOKEN", "")
NETZERO_BASE_URL  = "https://api.netzero.energy/api/v1"

# ChargePoint
CHARGEPOINT_USERNAME      = os.getenv("CHARGEPOINT_USERNAME", "")
CHARGEPOINT_COULOMB_TOKEN = os.getenv("CHARGEPOINT_COULOMB_TOKEN", "")
CHARGEPOINT_DEVICE_ID     = int(os.getenv("CHARGEPOINT_DEVICE_ID", "0"))

# Pushover notifications (optional — leave blank to disable)
PUSHOVER_USER_KEY  = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")

# Timezone
TZ = ZoneInfo(os.getenv("TZ", "America/Los_Angeles"))

# Google Sheet Webhook
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "")
CONTROL_SHEET_URL = os.getenv("CONTROL_SHEET_URL", "")

# Telegram AI Agent Configs
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
if TELEGRAM_ALLOWED_USER_ID:
    try:
        TELEGRAM_ALLOWED_USER_ID = int(TELEGRAM_ALLOWED_USER_ID)
    except ValueError:
        pass
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Path for dynamic configuration file
DYNAMIC_CONFIG_FILE = "logs/config_dynamic.json"
MANUAL_MODE_OVERRIDE = "default"  # Can be 'manual', 'auto', or 'default'

# Thresholds (initialize as default)
BATTERY_START_PCT       = float(os.getenv("BATTERY_START_PCT", "40"))
BATTERY_STOP_PCT        = float(os.getenv("BATTERY_STOP_PCT", "25"))
NIGHT_BLACKOUT_START_HOUR = int(os.getenv("NIGHT_BLACKOUT_START_HOUR", "16"))
NIGHT_BLACKOUT_END_HOUR   = int(os.getenv("NIGHT_BLACKOUT_END_HOUR", "9"))

MIN_CHARGE_MINUTES = int(os.getenv("MIN_CHARGE_MINUTES", "15"))

def load_dynamic_config():
    global BATTERY_START_PCT, BATTERY_STOP_PCT
    global NIGHT_BLACKOUT_START_HOUR, NIGHT_BLACKOUT_END_HOUR
    global MANUAL_MODE_OVERRIDE

    # Re-read defaults first
    BATTERY_START_PCT = float(os.getenv("BATTERY_START_PCT", "40"))
    BATTERY_STOP_PCT = float(os.getenv("BATTERY_STOP_PCT", "25"))
    NIGHT_BLACKOUT_START_HOUR = int(os.getenv("NIGHT_BLACKOUT_START_HOUR", "16"))
    NIGHT_BLACKOUT_END_HOUR = int(os.getenv("NIGHT_BLACKOUT_END_HOUR", "9"))
    MANUAL_MODE_OVERRIDE = "default"

    if os.path.exists(DYNAMIC_CONFIG_FILE):
        try:
            with open(DYNAMIC_CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "BATTERY_START_PCT" in data:
                    BATTERY_START_PCT = float(data["BATTERY_START_PCT"])
                if "BATTERY_STOP_PCT" in data:
                    BATTERY_STOP_PCT = float(data["BATTERY_STOP_PCT"])
                if "NIGHT_BLACKOUT_START_HOUR" in data:
                    NIGHT_BLACKOUT_START_HOUR = int(data["NIGHT_BLACKOUT_START_HOUR"])
                if "NIGHT_BLACKOUT_END_HOUR" in data:
                    NIGHT_BLACKOUT_END_HOUR = int(data["NIGHT_BLACKOUT_END_HOUR"])
                if "MANUAL_MODE_OVERRIDE" in data:
                    MANUAL_MODE_OVERRIDE = str(data["MANUAL_MODE_OVERRIDE"])
        except Exception:
            pass

def save_dynamic_config():
    os.makedirs(os.path.dirname(DYNAMIC_CONFIG_FILE), exist_ok=True)
    try:
        data = {
            "BATTERY_START_PCT": BATTERY_START_PCT,
            "BATTERY_STOP_PCT": BATTERY_STOP_PCT,
            "NIGHT_BLACKOUT_START_HOUR": NIGHT_BLACKOUT_START_HOUR,
            "NIGHT_BLACKOUT_END_HOUR": NIGHT_BLACKOUT_END_HOUR,
            "MANUAL_MODE_OVERRIDE": MANUAL_MODE_OVERRIDE,
        }
        with open(DYNAMIC_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

# Initial load
load_dynamic_config()

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))

CSV_LOG_FILE  = "logs/charger_log.csv"
TEXT_LOG_FILE = "logs/charger.log"

PGE_HOLIDAYS = {
    date(2025, 1, 1),  date(2025, 2, 17), date(2025, 5, 26),
    date(2025, 7, 4),  date(2025, 9, 1),  date(2025, 11, 11),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1),  date(2026, 2, 16), date(2026, 5, 25),
    date(2026, 7, 4),  date(2026, 9, 7),  date(2026, 11, 11),
    date(2026, 11, 26), date(2026, 12, 25),
}
