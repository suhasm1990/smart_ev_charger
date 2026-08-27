import logging
import logging.handlers
import os
import socket
import sys

# Single global socket timeout for every networking library in the process
# (requests, urllib3, gspread, litellm). Defined here because this module is
# imported first by every entry point.
socket.setdefaulttimeout(25.0)

TEXT_LOG_FILE = os.getenv("TEXT_LOG_FILE", "logs/charger.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(8 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s"

os.makedirs(os.path.dirname(TEXT_LOG_FILE) or ".", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Rotating handler keeps the log bounded; an unbounded file made every
        # tail read (error scanning, /logs) grow linearly with uptime.
        logging.handlers.RotatingFileHandler(
            TEXT_LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
        ),
    ],
)

log             = logging.getLogger("EV_CHARGER")
log_netzero     = logging.getLogger("NETZERO")
log_chargepoint = logging.getLogger("CHARGEPOINT")
log_decision    = logging.getLogger("DECISION")
log_mode        = logging.getLogger("MODE")
log_csv         = logging.getLogger("CSV")

_NOISY_LOGGERS = (
    "urllib3", "litellm", "LiteLLM", "httpcore", "httpx", "asyncio",
    "telebot", "TeleBot", "google", "gspread", "openai", "matplotlib",
)
for _name in _NOISY_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)


def tail_lines(path: str, limit: int, level: str = None) -> list[str]:
    """Reads the last `limit` lines of a file without loading the whole file.

    Seeks backwards in fixed blocks from EOF, which keeps cost proportional to
    the requested output rather than to total log size.
    """
    if not os.path.exists(path) or limit <= 0:
        return []
    # Over-read when filtering, since most lines will be discarded.
    want = limit * 20 if level else limit
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block, data, lines = 64 * 1024, b"", []
            while end > 0:
                step = min(block, end)
                end -= step
                f.seek(end)
                data = f.read(step) + data
                lines = data.splitlines()
                if len(lines) > want + 1:
                    break
        text = [ln.decode("utf-8", "replace") for ln in lines[-want:]]
    except Exception:
        return []
    if level:
        needle = f"| {level.upper()}"
        text = [ln for ln in text if needle in ln]
    return text[-limit:]


class GoogleSheetsLogHandler(logging.Handler):
    """Mirrors application events, warnings, and errors into Google Sheets."""

    _SKIP = frozenset(n.lower() for n in _NOISY_LOGGERS)

    def emit(self, record):
        try:
            if record.levelno < logging.INFO or record.name.lower() in self._SKIP:
                return
            from datetime import datetime
            from core import config
            from services.sheets_db import append_system_log, in_worker

            # A log emitted while writing to Sheets must not be queued for
            # Sheets, or the worker feeds itself in an unbounded loop.
            if in_worker():
                return

            append_system_log(
                timestamp=datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M:%S"),
                level=record.levelname,
                module=record.name,
                message=record.getMessage(),
            )
        except Exception:
            pass


_sheets_handler = GoogleSheetsLogHandler()
_sheets_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_sheets_handler)
