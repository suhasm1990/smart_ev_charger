import json
import os
from datetime import date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        return float(default)


def _env_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        return int(default)


# ── NetZero Energy API ──────────────────────────────────────────────────────
NETZERO_SITE_ID   = _env("NETZERO_SITE_ID")
NETZERO_API_TOKEN = _env("NETZERO_API_TOKEN")
NETZERO_BASE_URL  = _env("NETZERO_BASE_URL", "https://api.netzero.energy/api/v1")

# ── ChargePoint ─────────────────────────────────────────────────────────────
CHARGEPOINT_USERNAME      = _env("CHARGEPOINT_USERNAME")
CHARGEPOINT_COULOMB_TOKEN = _env("CHARGEPOINT_COULOMB_TOKEN")
CHARGEPOINT_DEVICE_ID     = _env_int("CHARGEPOINT_DEVICE_ID", "0")

# ── Notifications ───────────────────────────────────────────────────────────
PUSHOVER_USER_KEY  = _env("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = _env("PUSHOVER_API_TOKEN")
GRID_EXPORT_ALERT_THRESHOLD_KW = _env_float("GRID_EXPORT_ALERT_THRESHOLD_KW", "1.0")

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")


def _telegram_user_id() -> int | None:
    """Parses the allowlisted Telegram user ID, or None if unset or invalid.

    Normalising to int-or-None matters because the bot reaches shell execution
    through the dev agent: a value left as a string would never equal the
    incoming integer user ID, silently locking the owner out, while an unset
    value must deny everyone rather than everyone.
    """
    raw = _env("TELEGRAM_ALLOWED_USER_ID").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


TELEGRAM_ALLOWED_USER_ID: int | None = _telegram_user_id()

# ── Google Sheets ───────────────────────────────────────────────────────────
GOOGLE_SHEET_URL = _env(
    "GOOGLE_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1-GKCjMHUIPdh_2vvN9CadfisgOwwYAe0GHkQk3e1HUA",
)

# ── Schedule ────────────────────────────────────────────────────────────────
TZ = ZoneInfo(_env("TZ", "America/Los_Angeles"))
DAILY_RESET_TIME = _env("DAILY_RESET_TIME", "00:00")
DAILY_AGENT_TIME = _env("DAILY_AGENT_TIME", "07:00")
CHECK_INTERVAL_MINUTES = _env_int("CHECK_INTERVAL_MINUTES", "15")

# ── Utility rate plan (MID, PGE, or a custom provider) ──────────────────────
UTILITY_PROVIDER = _env("UTILITY_PROVIDER", "MID").upper().strip()
_IS_MID = UTILITY_PROVIDER == "MID"
UTILITY_FIXED_MONTHLY_FEE        = _env_float("UTILITY_FIXED_MONTHLY_FEE", "32.00" if _IS_MID else "0.00")
UTILITY_VOLUMETRIC_ADDER         = _env_float("UTILITY_VOLUMETRIC_ADDER", "0.0151" if _IS_MID else "0.0000")
UTILITY_TAX_MULTIPLIER           = _env_float("UTILITY_TAX_MULTIPLIER", "1.065" if _IS_MID else "1.000")
UTILITY_SOLAR_EXPORT_CREDIT_RATE = _env_float("UTILITY_SOLAR_EXPORT_CREDIT_RATE", "0.076" if _IS_MID else "0.080")

# ── Charger & vehicle ───────────────────────────────────────────────────────
MIN_CHARGE_MINUTES       = _env_int("MIN_CHARGE_MINUTES", "15")
DEFAULT_CHARGER_AMPERAGE = _env_int("DEFAULT_CHARGER_AMPERAGE", "20")
MIN_CHARGER_AMPERAGE     = _env_int("MIN_CHARGER_AMPERAGE", "8")
MAX_CHARGER_AMPERAGE     = _env_int("MAX_CHARGER_AMPERAGE", "32")
CHARGER_VOLTAGE          = _env_float("CHARGER_VOLTAGE", "240.0")
EV_MILES_PER_KWH         = _env_float("EV_MILES_PER_KWH", "3.53")
POWERWALL_USABLE_KWH     = _env_float("POWERWALL_USABLE_KWH", "13.5")

# ── LLM (model-agnostic via litellm) ────────────────────────────────────────
LLM_PROVIDER        = _env("LLM_PROVIDER").lower().strip()
LLM_MODEL           = _env("LLM_MODEL") or _env("GEMINI_MODEL")
LLM_API_KEY         = _env("LLM_API_KEY")
LLM_BASE_URL        = _env("LLM_BASE_URL")
LLM_THINKING_BUDGET = _env_int("LLM_THINKING_BUDGET", "8192")

NVIDIA_API_KEY    = _env("NVIDIA_API_KEY")
OPENAI_API_KEY    = _env("OPENAI_API_KEY")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
GEMINI_API_KEY    = _env("GEMINI_API_KEY")

# ── Paths ───────────────────────────────────────────────────────────────────
DYNAMIC_CONFIG_FILE = _env("DYNAMIC_CONFIG_FILE", "logs/config_dynamic.json")
CSV_LOG_FILE        = _env("CSV_LOG_FILE", "logs/charger_log.csv")
TEXT_LOG_FILE       = _env("TEXT_LOG_FILE", "logs/charger.log")

# ── Runtime-tunable settings ────────────────────────────────────────────────
# Every key here can be changed at runtime by the Telegram bot or the morning
# planner. Precedence, lowest to highest: environment -> local JSON -> Sheets.
DYNAMIC_CONFIG_SCHEMA: dict[str, tuple[type, str]] = {
    "BATTERY_START_PCT":         (float, "40"),
    "BATTERY_STOP_PCT":          (float, "25"),
    "BATTERY_LOW_RESERVE_PCT":   (float, "15"),
    "NIGHT_BLACKOUT_START_HOUR": (int,   "16"),
    "NIGHT_BLACKOUT_END_HOUR":   (int,   "9"),
    "ALLOWED_CHARGE_START_HOUR": (int,   "0"),
    "ALLOWED_CHARGE_END_HOUR":   (int,   "24"),
    "MANUAL_MODE_OVERRIDE":      (str,   "default"),  # 'manual', 'auto', or 'default'
    "EV_MILES_PER_KWH":          (float, "3.53"),
    "LLM_PROVIDER":              (str,   "gemini"),
    "LLM_MODEL":                 (str,   "gemini-2.5-flash"),
}


def _apply(source: dict):
    g = globals()
    for key, (cast, _) in DYNAMIC_CONFIG_SCHEMA.items():
        value = source.get(key)
        if value is None:
            continue
        try:
            g[key] = cast(value)
        except (ValueError, TypeError):
            pass


def load_dynamic_config(remote: bool = True):
    """Reloads runtime settings from environment, local JSON, then the cloud.

    `remote=False` skips the Google Sheets layer, which is how import-time
    initialisation avoids a blocking network call before anything has started.
    """
    _apply({key: os.getenv(key, default) for key, (_, default) in DYNAMIC_CONFIG_SCHEMA.items()})

    try:
        with open(DYNAMIC_CONFIG_FILE) as f:
            _apply(json.load(f))
    except (OSError, ValueError):
        pass

    if remote:
        try:
            from services.sheets_db import get_settings
            _apply(get_settings())
        except Exception:
            pass


def save_dynamic_config():
    """Persists runtime settings locally, then mirrors them to the cloud.

    The Sheets write is queued rather than awaited, so callers on the control
    loop and the Telegram handler are never blocked by a network round-trip.
    """
    data = {key: globals().get(key) for key in DYNAMIC_CONFIG_SCHEMA}
    try:
        os.makedirs(os.path.dirname(DYNAMIC_CONFIG_FILE) or ".", exist_ok=True)
        with open(DYNAMIC_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except OSError:
        pass

    try:
        from services.sheets_db import update_settings
        update_settings(data)
    except Exception:
        pass


# Populate from environment and disk only; the cloud layer loads on first cycle.
load_dynamic_config(remote=False)

PGE_HOLIDAYS = {
    date(2025, 1, 1),  date(2025, 2, 17),  date(2025, 5, 26),
    date(2025, 7, 4),  date(2025, 9, 1),   date(2025, 11, 11),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1),  date(2026, 2, 16),  date(2026, 5, 25),
    date(2026, 7, 4),  date(2026, 9, 7),   date(2026, 11, 11),
    date(2026, 11, 26), date(2026, 12, 25),
}
