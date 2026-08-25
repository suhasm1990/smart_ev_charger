import logging
import sys
import os
import socket

# Global 25-second socket timeout across all networking libraries (requests, urllib3, gspread, litellm)
socket.setdefaulttimeout(25.0)

TEXT_LOG_FILE = os.getenv("TEXT_LOG_FILE", "logs/charger.log")

# Ensure logs directory exists
os.makedirs(os.path.dirname(TEXT_LOG_FILE) or ".", exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s"

logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(TEXT_LOG_FILE),
    ],
)
log = logging.getLogger("EV_CHARGER")

log_netzero     = logging.getLogger("NETZERO")
log_chargepoint = logging.getLogger("CHARGEPOINT")
log_decision    = logging.getLogger("DECISION")
log_mode        = logging.getLogger("MODE")
log_csv         = logging.getLogger("CSV")

# Silence verbose third-party connection and debug logs
for logger_name in (
    "urllib3", "litellm", "LiteLLM", "httpcore", "httpx", 
    "asyncio", "telebot", "TeleBot", "google", "gspread",
    "openai", "openai._base_client", "matplotlib", "matplotlib.font_manager"
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

class GoogleSheetsLogHandler(logging.Handler):
    """Asynchronously forwards core system events, decisions, warnings, and errors to Google Sheets."""
    def emit(self, record):
        try:
            if record.levelno < logging.INFO:
                return
            if record.name.lower() in ("urllib3", "litellm", "httpcore", "httpx", "google", "gspread", "openai", "telebot"):
                return
            from services.sheets_db import append_system_log
            from datetime import datetime
            from core import config
            ts = datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M:%S")
            append_system_log(
                timestamp=ts,
                level=record.levelname,
                module=record.name,
                message=record.getMessage()
            )
        except Exception:
            pass

_sheets_handler = GoogleSheetsLogHandler()
_sheets_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_sheets_handler)
