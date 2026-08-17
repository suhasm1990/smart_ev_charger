import os
import socket
import queue
import threading
import time

# Set 20-second socket timeout so network/DNS drops never hang Google Sheets calls indefinitely
socket.setdefaulttimeout(20.0)

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None
from reporting.logger import log

# We default to the URL provided by the user
SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "https://docs.google.com/spreadsheets/d/1-GKCjMHUIPdh_2vvN9CadfisgOwwYAe0GHkQk3e1HUA")
CREDS_FILE = "service_account.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

_client = None

def get_client():
    global _client
    if gspread is None or Credentials is None:
        log.warning("gspread or google-auth not installed. Google Sheets integration disabled.")
        return None
    if _client is None:
        if not os.path.exists(CREDS_FILE):
            log.warning(f"{CREDS_FILE} not found. Google Sheets integration disabled.")
            return None
        try:
            creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
            _client = gspread.authorize(creds)
        except Exception as e:
            log.error(f"Failed to authenticate with Google Sheets: {e}")
            return None
    return _client

def get_sheet():
    client = get_client()
    if not client:
        return None
    try:
        return client.open_by_url(SHEET_URL)
    except Exception as e:
        log.error(f"Failed to open spreadsheet by URL: {e}")
        return None

# Background asynchronous worker queue for appending log rows
_append_queue = queue.Queue(maxsize=2000)

def _queue_worker():
    while True:
        row_data = _append_queue.get()
        for attempt in range(3):
            try:
                sheet = get_sheet()
                if sheet:
                    worksheet = sheet.get_worksheet(0)
                    worksheet.append_row(row_data)
                    break
            except Exception as e:
                global _client
                _client = None  # Force re-authentication on next attempt
                log.warning(f"Google Sheets async append attempt {attempt+1} failed: {e}")
                time.sleep(2 ** attempt)
        _append_queue.task_done()

_worker_thread = threading.Thread(target=_queue_worker, daemon=True, name="SheetsQueueWorker")
_worker_thread.start()

def append_log_row(row_data):
    """Asynchronously enqueues a log row to be written to Google Sheets without blocking."""
    try:
        _append_queue.put_nowait(row_data)
        return True
    except queue.Full:
        log.warning("Google Sheets append queue is full. Dropping row.")
        return False

def get_recent_logs(days=7):
    sheet = get_sheet()
    if not sheet:
        return []
    try:
        worksheet = sheet.get_worksheet(0)
        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        
        # 96 rows a day * 7 days = 672 rows. Fetch last 700 to be safe.
        recent_rows = rows[-700:] 
        
        return [dict(zip(headers, row)) for row in recent_rows]
    except Exception as e:
        log.error(f"Failed to fetch logs: {e}")
        return []

def update_settings(settings: dict):
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        worksheet = sheet.worksheet("Settings")
    except gspread.exceptions.WorksheetNotFound:
        log.info("Creating 'Settings' worksheet...")
        worksheet = sheet.add_worksheet(title="Settings", rows="100", cols="2")
        
    try:
        cells = [["Key", "Value"]]
        for k, v in settings.items():
            cells.append([str(k), str(v)])
        
        worksheet.clear()
        worksheet.update('A1', cells)
        return True
    except Exception as e:
        log.error(f"Failed to update settings in Sheets: {e}")
        return False

def get_settings():
    sheet = get_sheet()
    if not sheet:
        return {}
    try:
        worksheet = sheet.worksheet("Settings")
        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return {}
        
        settings = {}
        for row in all_values[1:]:
            if len(row) >= 2:
                settings[row[0]] = row[1]
        return settings
    except Exception as e:
        log.error(f"Failed to fetch settings from Sheets: {e}")
        return {}

def add_user_instruction(instruction: str):
    settings = get_settings()
    settings["USER_INSTRUCTION"] = instruction
    return update_settings(settings)

def clear_user_instruction():
    settings = get_settings()
    if "USER_INSTRUCTION" in settings:
        del settings["USER_INSTRUCTION"]
        return update_settings(settings)
    return True
