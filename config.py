import os
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

# Thresholds
BATTERY_START_PCT       = float(os.getenv("BATTERY_START_PCT", "40"))
BATTERY_STOP_PCT        = float(os.getenv("BATTERY_STOP_PCT", "25"))
NIGHT_BLACKOUT_START_HOUR = int(os.getenv("NIGHT_BLACKOUT_START_HOUR", "16"))
NIGHT_BLACKOUT_END_HOUR   = int(os.getenv("NIGHT_BLACKOUT_END_HOUR", "9"))

MIN_CHARGE_MINUTES = int(os.getenv("MIN_CHARGE_MINUTES", "30"))

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

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
