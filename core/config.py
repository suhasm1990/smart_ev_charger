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

# Google Sheets Integration
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "https://docs.google.com/spreadsheets/d/1-GKCjMHUIPdh_2vvN9CadfisgOwwYAe0GHkQk3e1HUA")

# Schedule Config
DAILY_RESET_TIME = os.getenv("DAILY_RESET_TIME", "00:00")
DAILY_AGENT_TIME = os.getenv("DAILY_AGENT_TIME", "07:00")

# Notification Thresholds
GRID_EXPORT_ALERT_THRESHOLD_KW = float(os.getenv("GRID_EXPORT_ALERT_THRESHOLD_KW", "1.0"))

# Dynamic Utility Provider Configuration (MID, PGE, or CUSTOM)
UTILITY_PROVIDER = os.getenv("UTILITY_PROVIDER", "MID").upper().strip()
UTILITY_FIXED_MONTHLY_FEE = float(os.getenv("UTILITY_FIXED_MONTHLY_FEE", "32.00" if UTILITY_PROVIDER == "MID" else "0.00"))
UTILITY_VOLUMETRIC_ADDER = float(os.getenv("UTILITY_VOLUMETRIC_ADDER", "0.0151" if UTILITY_PROVIDER == "MID" else "0.0000"))
UTILITY_TAX_MULTIPLIER = float(os.getenv("UTILITY_TAX_MULTIPLIER", "1.065" if UTILITY_PROVIDER == "MID" else "1.000"))
UTILITY_SOLAR_EXPORT_CREDIT_RATE = float(os.getenv("UTILITY_SOLAR_EXPORT_CREDIT_RATE", "0.076" if UTILITY_PROVIDER == "MID" else "0.080"))

# Timezone
TZ = ZoneInfo(os.getenv("TZ", "America/Los_Angeles"))

# Telegram AI Agent Configs
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
if TELEGRAM_ALLOWED_USER_ID:
    try:
        TELEGRAM_ALLOWED_USER_ID = int(TELEGRAM_ALLOWED_USER_ID)
    except ValueError:
        pass
# Model Agnostic AI Agent Configs (NVIDIA, OpenAI, Anthropic, Gemini, etc.)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").lower().strip()
LLM_MODEL = os.getenv("LLM_MODEL", "") or os.getenv("GEMINI_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_THINKING_BUDGET = int(os.getenv("LLM_THINKING_BUDGET", "8192"))

# Path for dynamic configuration file
DYNAMIC_CONFIG_FILE = "logs/config_dynamic.json"
MANUAL_MODE_OVERRIDE = "default"  # Can be 'manual', 'auto', or 'default'

# Thresholds (initialize as default)
BATTERY_START_PCT       = float(os.getenv("BATTERY_START_PCT", "40"))
BATTERY_STOP_PCT        = float(os.getenv("BATTERY_STOP_PCT", "25"))
BATTERY_LOW_RESERVE_PCT = float(os.getenv("BATTERY_LOW_RESERVE_PCT", "15"))
NIGHT_BLACKOUT_START_HOUR = int(os.getenv("NIGHT_BLACKOUT_START_HOUR", "16"))
NIGHT_BLACKOUT_END_HOUR   = int(os.getenv("NIGHT_BLACKOUT_END_HOUR", "9"))

MIN_CHARGE_MINUTES = int(os.getenv("MIN_CHARGE_MINUTES", "15"))
ALLOWED_CHARGE_START_HOUR = int(os.getenv("ALLOWED_CHARGE_START_HOUR", "0"))
ALLOWED_CHARGE_END_HOUR = int(os.getenv("ALLOWED_CHARGE_END_HOUR", "24"))
DEFAULT_CHARGER_AMPERAGE = int(os.getenv("DEFAULT_CHARGER_AMPERAGE", "20"))
MAX_CHARGER_AMPERAGE = int(os.getenv("MAX_CHARGER_AMPERAGE", "32"))


DYNAMIC_CONFIG_SCHEMA = {
    "BATTERY_START_PCT": (float, "40"),
    "BATTERY_STOP_PCT": (float, "25"),
    "BATTERY_LOW_RESERVE_PCT": (float, "15"),
    "NIGHT_BLACKOUT_START_HOUR": (int, "16"),
    "NIGHT_BLACKOUT_END_HOUR": (int, "9"),
    "MANUAL_MODE_OVERRIDE": (str, "default"),
    "ALLOWED_CHARGE_START_HOUR": (int, "0"),
    "ALLOWED_CHARGE_END_HOUR": (int, "24"),
    "LLM_PROVIDER": (str, "gemini"),
    "LLM_MODEL": (str, "gemini-2.5-flash"),
}

def _apply_config_dict(source_dict: dict):
    """Applies values from a dictionary to module-level globals using the schema."""
    globals_dict = globals()
    for key, (caster, _) in DYNAMIC_CONFIG_SCHEMA.items():
        if key in source_dict and source_dict[key] is not None:
            try:
                globals_dict[key] = caster(source_dict[key])
            except (ValueError, TypeError):
                pass

def load_dynamic_config():
    """Loads default env thresholds, then layers local JSON overrides, then Google Sheets settings."""
    # 1. Defaults from environment
    env_defaults = {
        key: os.getenv(key, default_val)
        for key, (_, default_val) in DYNAMIC_CONFIG_SCHEMA.items()
    }
    _apply_config_dict(env_defaults)

    # 2. Layer local JSON file overrides
    if os.path.exists(DYNAMIC_CONFIG_FILE):
        try:
            with open(DYNAMIC_CONFIG_FILE, "r") as f:
                _apply_config_dict(json.load(f))
        except Exception:
            pass

    # 3. Layer Google Sheets overrides (cloud single source of truth)
    try:
        from services.sheets_db import get_settings
        _apply_config_dict(get_settings())
    except Exception:
        pass

def save_dynamic_config():
    """Saves current in-memory dynamic configuration to local JSON and Google Sheets."""
    os.makedirs(os.path.dirname(DYNAMIC_CONFIG_FILE), exist_ok=True)
    data = {key: globals().get(key) for key in DYNAMIC_CONFIG_SCHEMA}
    try:
        with open(DYNAMIC_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
        try:
            from services.sheets_db import update_settings
            update_settings(data)
        except Exception:
            pass
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
