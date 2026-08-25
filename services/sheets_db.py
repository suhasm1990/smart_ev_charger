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
_sheet = None
_worksheets = {}
_last_sheet_open_time = 0.0

def get_client():
    global _client, _sheet, _worksheets
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
            _sheet = None
            _worksheets = {}
        except Exception as e:
            log.error(f"Failed to authenticate with Google Sheets: {e}")
            return None
    return _client

def get_sheet():
    global _sheet, _last_sheet_open_time, _worksheets
    client = get_client()
    if not client:
        return None
    now_ts = time.time()
    # Cache spreadsheet object for up to 15 minutes to eliminate redundant metadata calls
    if _sheet is not None and (now_ts - _last_sheet_open_time) < 900.0:
        return _sheet
    try:
        _sheet = client.open_by_url(SHEET_URL)
        _last_sheet_open_time = now_ts
        _worksheets = {}
        return _sheet
    except Exception as e:
        log.error(f"Failed to open spreadsheet by URL: {e}")
        _sheet = None
        return None

def get_or_create_worksheet(title: str, default_headers: list = None, default_rows: str = "505", default_cols: str = "10"):
    """Unified worksheet fetcher and creator with in-memory handle caching."""
    global _worksheets
    sheet = get_sheet()
    if not sheet:
        return None
    if title in _worksheets:
        return _worksheets[title]
    try:
        ws = sheet.worksheet(title)
        _worksheets[title] = ws
        return ws
    except Exception:
        # If looking for Telemetry, check legacy Sheet1 and auto-rename
        if title == "Telemetry":
            try:
                ws = sheet.worksheet("Sheet1")
                try:
                    ws.update_title("Telemetry")
                except Exception:
                    pass
                _worksheets["Telemetry"] = ws
                return ws
            except Exception:
                pass
        # Auto-create worksheet if not found
        try:
            ws = sheet.add_worksheet(title=title, rows=default_rows, cols=default_cols)
            if default_headers:
                ws.update(range_name='A1', values=[default_headers])
            _worksheets[title] = ws
            return ws
        except Exception as e:
            log.error(f"Failed to create worksheet '{title}': {e}")
            return None

# Background asynchronous worker queues
_append_queue = queue.Queue(maxsize=2000)
_system_log_queue = queue.Queue(maxsize=2000)

def _flush_queue_to_worksheet(target_queue: queue.Queue, worksheet_name: str, max_rows: int, chunk_size: int, default_headers: list = None):
    """Generic worker loop for asynchronously batching, writing, and rolling over worksheet rows."""
    global _client, _sheet, _worksheets
    approx_count = None
    
    while True:
        batch = []
        try:
            item = target_queue.get(timeout=5.0)
            batch.append(item)
            while len(batch) < 25:
                try:
                    batch.append(target_queue.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            continue

        if not batch:
            continue

        for attempt in range(3):
            try:
                ws = get_or_create_worksheet(worksheet_name, default_headers=default_headers)
                if ws:
                    if len(batch) == 1:
                        ws.append_row(batch[0])
                    else:
                        ws.append_rows(batch)

                    if approx_count is not None:
                        approx_count += len(batch)
                    else:
                        approx_count = ws.row_count

                    # Enforce rolling ring buffer in chunks (minimizes API calls)
                    if approx_count > (max_rows + chunk_size):
                        excess = approx_count - (max_rows + 1)
                        if excess > 0:
                            ws.delete_rows(2, 2 + excess - 1)
                            approx_count = max_rows + 1
                    break
            except Exception as e:
                _client = None
                _sheet = None
                _worksheets = {}
                log.warning(f"Google Sheets async append attempt {attempt+1} on '{worksheet_name}' failed: {e}")
                time.sleep(2 ** attempt)

        for _ in range(len(batch)):
            target_queue.task_done()

_worker_thread = threading.Thread(
    target=_flush_queue_to_worksheet,
    args=(_append_queue, "Telemetry", 6000, 100),
    daemon=True,
    name="SheetsTelemetryWorker"
)
_worker_thread.start()

_syslog_thread = threading.Thread(
    target=_flush_queue_to_worksheet,
    args=(_system_log_queue, "System Logs", 500, 50, ['Timestamp', 'Level', 'Module', 'Message']),
    daemon=True,
    name="SheetsSyslogWorker"
)
_syslog_thread.start()

def append_log_row(row_data):
    """Asynchronously enqueues a log row to be written to Google Sheets Telemetry tab without blocking."""
    try:
        _append_queue.put_nowait(row_data)
        return True
    except queue.Full:
        log.warning("Google Sheets append queue is full. Dropping row.")
        return False

def append_system_log(timestamp: str, level: str, module: str, message: str):
    """Asynchronously enqueues a system event/error log to be written to Google Sheets 'System Logs' tab."""
    try:
        _system_log_queue.put_nowait([str(timestamp), str(level), str(module), str(message)])
        return True
    except queue.Full:
        return False

def get_recent_logs(days=7):
    """Fetches recent power telemetry log rows from Google Sheets 'Telemetry' tab."""
    try:
        worksheet = get_or_create_worksheet("Telemetry")
        if not worksheet:
            return []
        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        
        # 96 rows a day * 7 days = 672 rows. Fetch last 700 to be safe.
        recent_rows = rows[-700:] 
        return [dict(zip(headers, row)) for row in recent_rows]
    except Exception as e:
        log.error(f"Failed to fetch telemetry logs from Sheets: {e}")
        return []

def get_system_logs(limit: int = 100, level_filter: str = None) -> list[dict]:
    """Fetches recent system event and error logs from Google Sheets 'System Logs' tab."""
    try:
        worksheet = get_or_create_worksheet("System Logs")
        if not worksheet:
            return []
            
        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) <= 1:
            return []
        raw_rows = all_values[1:]
        
        parsed = []
        for r in raw_rows:
            if len(r) >= 4:
                item = {
                    "timestamp": r[0],
                    "level": r[1],
                    "module": r[2],
                    "message": r[3]
                }
                if level_filter and item["level"].upper() != level_filter.upper():
                    continue
                parsed.append(item)
                
        return parsed[-limit:]
    except Exception as e:
        log.error(f"Failed to fetch system logs from Sheets: {e}")
        return []

def update_settings(settings: dict):
    """Updates key-value settings in the 'Settings' worksheet tab."""
    try:
        worksheet = get_or_create_worksheet("Settings", default_headers=["Key", "Value"], default_rows="100", default_cols="2")
        if not worksheet:
            return False
            
        cells = [["Key", "Value"]]
        for k, v in settings.items():
            cells.append([str(k), str(v)])
        
        worksheet.clear()
        worksheet.update(range_name='A1', values=cells)
        return True
    except Exception as e:
        log.error(f"Failed to update settings in Sheets: {e}")
        return False

def get_settings():
    """Fetches key-value configuration dictionary from the 'Settings' worksheet tab."""
    try:
        worksheet = get_or_create_worksheet("Settings", default_headers=["Key", "Value"])
        if not worksheet:
            return {}
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
    """Saves a user instruction for the Daily AI Agent into the Settings tab."""
    settings = get_settings()
    settings["USER_INSTRUCTION"] = instruction
    return update_settings(settings)

def clear_user_instruction():
    """Clears processed user instruction from the Settings tab."""
    settings = get_settings()
    if "USER_INSTRUCTION" in settings:
        del settings["USER_INSTRUCTION"]
        return update_settings(settings)
    return True
