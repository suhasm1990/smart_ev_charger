"""Google Sheets cloud database: telemetry ring buffer, system logs, settings.

All writes are queued onto background workers so the control loop never blocks
on a network round-trip. Reads are TTL-cached to stay far below API quotas.
"""
import os
import queue
import threading
import time

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = Credentials = None

from core import config
from reporting.logger import log

SHEET_URL = config.GOOGLE_SHEET_URL
CREDS_FILE = config.GOOGLE_CREDENTIALS_FILE
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_TTL = 900.0     # Reuse the spreadsheet handle for 15 minutes.
SETTINGS_TTL = 60.0   # Serve settings from memory for 60 seconds.

TELEMETRY_TAB = "Telemetry"
SYSLOG_TAB = "System Logs"
SETTINGS_TAB = "Settings"
SYSLOG_HEADERS = ["Timestamp", "Level", "Module", "Message"]

# Guards the connection-handle globals (_client, _sheet, _worksheets, ...).
# An RLock because get_or_create_worksheet -> get_sheet -> get_client nest.
# Holding it across the open/create network calls serializes the three workers
# on (re)connection only, which is rare and keeps the handles consistent.
_lock = threading.RLock()
# Set on the Sheets worker threads. The Google Sheets log handler checks this to
# avoid a feedback loop: a warning raised while writing to Sheets would other-
# wise be queued for Sheets, drained by this same worker, and warn again.
_worker_local = threading.local()
_credentials_warned = False
_client = None


def in_worker() -> bool:
    """True when the calling thread is a Sheets background worker."""
    return getattr(_worker_local, "active", False)


def is_disabled() -> bool:
    """True when the integration cannot work, so callers can skip queueing."""
    return gspread is None or Credentials is None or not os.path.exists(CREDS_FILE)
_sheet = None
_sheet_opened_at = 0.0
_worksheets: dict = {}


def _reset_connection():
    global _client, _sheet, _worksheets
    with _lock:
        _client = _sheet = None
        _worksheets = {}


def get_client():
    global _client, _credentials_warned
    if gspread is None or Credentials is None:
        return None
    with _lock:
        if _client is None:
            if not os.path.exists(CREDS_FILE):
                if not _credentials_warned:
                    _credentials_warned = True
                    log.warning(f"{CREDS_FILE} not found. Google Sheets integration disabled.")
                return None
            try:
                _client = gspread.authorize(Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES))
                try:
                    _client.http_client.set_timeout(30)
                except AttributeError:  # gspread < 6
                    pass
            except Exception as e:
                log.error(f"Failed to authenticate with Google Sheets: {e}")
                return None
        return _client


def get_sheet():
    global _sheet, _sheet_opened_at, _worksheets
    with _lock:
        client = get_client()
        if not client:
            return None
        now = time.time()
        if _sheet is not None and (now - _sheet_opened_at) < SHEET_TTL:
            return _sheet
        try:
            _sheet = client.open_by_url(SHEET_URL)
            _sheet_opened_at = now
            _worksheets = {}
            return _sheet
        except Exception as e:
            log.error(f"Failed to open spreadsheet by URL: {e}")
            _sheet = None
            return None


def get_or_create_worksheet(title: str, headers: list = None, rows: int = 1000, cols: int = 32):
    """Returns a cached worksheet handle, creating the tab on first use."""
    with _lock:
        sheet = get_sheet()
        if not sheet:
            return None
        if title in _worksheets:
            return _worksheets[title]
        try:
            ws = sheet.worksheet(title)
        except Exception:
            # Adopt the legacy default tab name rather than orphaning its history.
            ws = None
            if title == TELEMETRY_TAB:
                try:
                    ws = sheet.worksheet("Sheet1")
                    ws.update_title(TELEMETRY_TAB)
                except Exception:
                    ws = None
            if ws is None:
                try:
                    ws = sheet.add_worksheet(title=title, rows=rows, cols=cols)
                    if headers:
                        ws.update(range_name="A1", values=[headers])
                except Exception as e:
                    log.error(f"Failed to create worksheet '{title}': {e}")
                    return None
        _worksheets[title] = ws
        return ws


# ── Asynchronous write workers ──────────────────────────────────────────────

_telemetry_queue: queue.Queue = queue.Queue(maxsize=2000)
_syslog_queue: queue.Queue = queue.Queue(maxsize=2000)
_settings_queue: queue.Queue = queue.Queue(maxsize=200)


def _col_letter(index: int) -> str:
    """Converts a 1-based column index into its A1 letter (1 -> A, 27 -> AA)."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _data_row_count(ws) -> int:
    """Counts populated rows (column A), which `row_count` does not report."""
    try:
        return len(ws.col_values(1))
    except Exception:
        return 0


def _drain(target: queue.Queue, limit: int = 25) -> list:
    """Blocks for one item, then greedily batches whatever else is pending."""
    try:
        batch = [target.get(timeout=5.0)]
    except queue.Empty:
        return []
    while len(batch) < limit:
        try:
            batch.append(target.get_nowait())
        except queue.Empty:
            break
    return batch


def _write_batch(tab: str, batch: list, headers: list, row_count, max_rows: int, trim_chunk: int):
    """Writes one batch with retries; returns the updated row count (None on failure)."""
    for attempt in range(3):
        try:
            ws = get_or_create_worksheet(tab, headers=headers)
            if ws is None:
                # Treat like any failed attempt so the batch is retried and,
                # if still failing, its drop is logged — never silent.
                raise RuntimeError(f"worksheet '{tab}' is unavailable")
            ws.append_rows(batch)
            if row_count is None:
                row_count = _data_row_count(ws)
            else:
                row_count += len(batch)
            # Trim in chunks so a delete_rows call is amortised over
            # many appends instead of firing on every single write.
            if row_count > max_rows + trim_chunk:
                excess = row_count - max_rows
                ws.delete_rows(2, 1 + excess)
                row_count -= excess
            return row_count
        except Exception as e:
            row_count = None
            _reset_connection()
            log.warning(f"Sheets append to '{tab}' failed (attempt {attempt + 1}/3): {e}")
            if attempt == 2:
                log.error(f"Dropped {len(batch)} row(s) destined for '{tab}' after 3 failed attempts.")
            else:
                time.sleep(2 ** attempt)
    return None


def _append_worker(target: queue.Queue, tab: str, max_rows: int, trim_chunk: int, headers: list = None):
    """Batches queued rows into a worksheet and enforces a rolling ring buffer."""
    _worker_local.active = True
    row_count = None
    while True:
        batch = _drain(target)
        if not batch:
            continue
        try:
            row_count = _write_batch(tab, batch, headers, row_count, max_rows, trim_chunk)
        finally:
            for _ in batch:
                target.task_done()


def _settings_worker():
    """Applies queued settings patches, coalescing bursts into one write."""
    _worker_local.active = True
    while True:
        patches = _drain(_settings_queue, limit=50)
        if not patches:
            continue
        try:
            merged = {}
            for p in patches:
                merged.update(p)
            _write_settings(merged)
        except Exception as e:
            log.warning(f"Failed to persist settings to Sheets: {e}")
        finally:
            for _ in patches:
                _settings_queue.task_done()


for _target, _args in (
    (_append_worker, (_telemetry_queue, TELEMETRY_TAB, 6000, 100, None)),
    (_append_worker, (_syslog_queue, SYSLOG_TAB, 500, 50, SYSLOG_HEADERS)),
    (_settings_worker, ()),
):
    threading.Thread(target=_target, args=_args, daemon=True, name=f"Sheets-{_target.__name__}").start()


def append_log_row(row_data) -> bool:
    """Queues a telemetry row for the Telemetry tab without blocking."""
    if is_disabled():
        return False
    try:
        _telemetry_queue.put_nowait(row_data)
        return True
    except queue.Full:
        log.warning("Google Sheets telemetry queue is full. Dropping row.")
        return False


def append_system_log(timestamp: str, level: str, module: str, message: str) -> bool:
    """Queues a system event for the System Logs tab without blocking."""
    if is_disabled() or in_worker():
        return False
    try:
        _syslog_queue.put_nowait([str(timestamp), str(level), str(module), str(message)])
        return True
    except queue.Full:
        return False


def flush(timeout: float = 10.0):
    """Waits for pending writes to land, for use during graceful shutdown.

    unfinished_tasks (not empty()) is the right signal: workers pop an item
    before writing it, so an empty queue can still have a write in flight.
    """
    deadline = time.time() + timeout
    queues = (_settings_queue, _telemetry_queue, _syslog_queue)
    while any(q.unfinished_tasks for q in queues) and time.time() < deadline:
        time.sleep(0.05)


# ── Reads ───────────────────────────────────────────────────────────────────

def get_recent_logs(days: int = 7) -> list[dict]:
    """Fetches recent telemetry rows, reading only the tail of the sheet."""
    try:
        ws = get_or_create_worksheet(TELEMETRY_TAB)
        if not ws:
            return []
        # 96 rows/day at 15-minute resolution, plus headroom for denser intervals.
        wanted = max(100, days * 96 + 100)
        total = _data_row_count(ws)
        if total <= 1:
            return []
        headers = ws.row_values(1)
        first = max(2, total - wanted + 1)
        rows = ws.get(f"A{first}:{_col_letter(len(headers))}{total}")
        return [dict(zip(headers, r)) for r in rows if r]
    except Exception as e:
        log.error(f"Failed to fetch telemetry logs from Sheets: {e}")
        return []


def get_system_logs(limit: int = 100, level_filter: str = None) -> list[dict]:
    """Fetches recent rows from the System Logs tab."""
    try:
        ws = get_or_create_worksheet(SYSLOG_TAB, headers=SYSLOG_HEADERS)
        if not ws:
            return []
        total = _data_row_count(ws)
        if total <= 1:
            return []
        # Over-fetch when filtering so the filtered result still fills `limit`.
        span = limit * 10 if level_filter else limit
        rows = ws.get(f"A{max(2, total - span + 1)}:D{total}")
        parsed = [
            dict(zip(("timestamp", "level", "module", "message"), r))
            for r in rows
            if len(r) >= 4 and (not level_filter or r[1].upper() == level_filter.upper())
        ]
        return parsed[-limit:]
    except Exception as e:
        log.error(f"Failed to fetch system logs from Sheets: {e}")
        return []


_settings_cache: dict | None = None
_settings_cache_time = 0.0
# Guards only the cache tuple above — never held across a Sheets round-trip.
_settings_lock = threading.Lock()


def get_settings(force_refresh: bool = False) -> dict:
    """Returns the cloud key/value settings, cached for `SETTINGS_TTL` seconds.

    The control loop reads settings every cycle, so an uncached read here meant
    a Google Sheets round-trip on the hot path.
    """
    global _settings_cache, _settings_cache_time
    with _settings_lock:
        if not force_refresh and _settings_cache is not None and (time.time() - _settings_cache_time) < SETTINGS_TTL:
            return _settings_cache
    try:
        ws = get_or_create_worksheet(SETTINGS_TAB, headers=["Key", "Value"], rows=100, cols=2)
        values = ws.get_all_values() if ws else []
        settings = {r[0]: r[1] for r in values[1:] if len(r) >= 2 and r[0]}
    except Exception as e:
        log.error(f"Failed to fetch settings from Sheets: {e}")
        # Serve the last good snapshot rather than reporting "no settings".
        with _settings_lock:
            return _settings_cache if _settings_cache is not None else {}
    with _settings_lock:
        _settings_cache, _settings_cache_time = settings, time.time()
    return settings


def _write_settings(patch: dict, remove: set = frozenset()) -> bool:
    """Merges `patch` into the Settings tab, preserving unrelated keys."""
    global _settings_cache, _settings_cache_time
    try:
        ws = get_or_create_worksheet(SETTINGS_TAB, headers=["Key", "Value"], rows=100, cols=2)
        if not ws:
            return False
        merged = {k: v for k, v in get_settings(force_refresh=True).items() if k not in remove}
        merged.update({k: v for k, v in patch.items() if k not in remove})
        ws.clear()
        ws.update(range_name="A1", values=[["Key", "Value"]] + [[str(k), str(v)] for k, v in merged.items()])
        with _settings_lock:
            _settings_cache, _settings_cache_time = {k: str(v) for k, v in merged.items()}, time.time()
        return True
    except Exception as e:
        log.error(f"Failed to update settings in Sheets: {e}")
        return False


def update_settings(settings: dict, blocking: bool = False) -> bool:
    """Merges settings into the cloud tab; queued asynchronously by default.

    Merging matters: a full overwrite would erase keys this caller does not own,
    such as a pending USER_INSTRUCTION awaiting the morning planner.
    """
    global _settings_cache, _settings_cache_time
    if blocking:
        return _write_settings(settings)
    # Reflect the change locally at once so readers never see a stale value.
    with _settings_lock:
        if _settings_cache is not None:
            _settings_cache = {**_settings_cache, **{k: str(v) for k, v in settings.items()}}
            _settings_cache_time = time.time()
    try:
        _settings_queue.put_nowait(dict(settings))
        return True
    except queue.Full:
        log.warning("Settings queue is full. Writing synchronously.")
        return _write_settings(settings)


def add_user_instruction(instruction: str) -> bool:
    """Stores an instruction for the daily AI planner to consume."""
    return _write_settings({"USER_INSTRUCTION": instruction})


def clear_user_instruction() -> bool:
    """Clears a processed instruction from the Settings tab."""
    return _write_settings({}, remove={"USER_INSTRUCTION"})
