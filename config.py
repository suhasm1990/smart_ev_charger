from datetime import date
from zoneinfo import ZoneInfo

# NetZero Energy API
NETZERO_SITE_ID   = "2252365359032855"
NETZERO_API_TOKEN = "wy0G8DiEdSlAv7Zi9BDtao1lWcPNTCUTGZ67ISZm"
NETZERO_BASE_URL  = "https://api.netzero.energy/api/v1"

# ChargePoint
CHARGEPOINT_USERNAME      = "suhasmallesh"
CHARGEPOINT_COULOMB_TOKEN = "94803ef08378b01e91c20cf34b7f42bb%23D3d08877"
CHARGEPOINT_DEVICE_ID     = 17495831

# Pushover notifications (optional — leave blank to disable)
PUSHOVER_USER_KEY  = ""
PUSHOVER_API_TOKEN = ""

# Timezone
TZ = ZoneInfo("America/Los_Angeles")

# Thresholds
BATTERY_START_PCT       = 60
BATTERY_STOP_PCT        = 25
BATTERY_RESUME_PCT      = 60
SOLAR_START_KW          = 2.0
SOLAR_STOP_KW           = 1.0

PEAK_MIN_SOLAR_SURPLUS_KW     = 3.0
PEAK_BATTERY_COVER_SURPLUS_KW = 2.0
PEAK_BATTERY_MIN_PCT          = 50

WEEKEND_BATTERY_START_PCT = 40

SOLAR_START_HOUR          = 9
MORNING_CHARGE_START_HOUR = 6
MORNING_CHARGE_END_HOUR   = 13

NIGHT_BLACKOUT_START_HOUR = 16
NIGHT_BLACKOUT_END_HOUR   = 9

MIN_CHARGE_MINUTES = 30
CAR_CHARGE_KW = 3.6

CHECK_INTERVAL_SECONDS = 300

CONTROL_SHEET_URL = "https://docs.google.com/spreadsheets/d/1-1aZkpT5xoVXy_TyebXZx0mOe9DdsxZ_jYA7ctYqO8w/export?format=csv&gid=0"
GOOGLE_SHEET_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwtWaUPwN9KqqppQYOI95GEDIfi0yBXJvQVQMqKbBSTKPHssezEgBp9WcSBXVw7lHuv/exec"

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
