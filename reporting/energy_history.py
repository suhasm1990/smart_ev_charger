"""Metered Powerwall energy history (Tesla Fleet API), stored locally and summed per day.

Each row is one Tesla interval (5 minutes for recent days, 30 minutes for
backfilled months) in watt-hours. Bills and summaries use these metered days
where they exist and fall back to the 15-minute snapshot estimate elsewhere.

Calls are kept to a minimum: one call for yesterday each night, and for a
month with several missing days a single month-wide call.
"""
import csv
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from core import config
from core.tou import get_tou_period, get_tou_rate

log = logging.getLogger("TESLA")

HEADERS = ["timestamp", "grid_import_wh", "grid_export_wh", "solar_wh", "home_wh"]

_lock = threading.RLock()
_cache: dict = {"mtime": None, "rows": {}}


def _wh(entry: dict, *keys) -> float:
    total = 0.0
    for key in keys:
        try:
            total += float(entry.get(key) or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def normalise(entry: dict) -> dict | None:
    """One Tesla time_series entry -> one stored row (None when unusable)."""
    ts = entry.get("timestamp")
    try:
        datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    home = _wh(entry, "total_home_usage") or _wh(
        entry, "consumer_energy_imported_from_grid", "consumer_energy_imported_from_solar",
        "consumer_energy_imported_from_battery", "consumer_energy_imported_from_generator")
    return {
        "timestamp": ts,
        "grid_import_wh": _wh(entry, "grid_energy_imported"),
        # Tesla's from_solar/from_battery split misses part of the metered export
        # (Oct 2026: 55 of 96 kWh), so the total wins when present.
        "grid_export_wh": _wh(entry, "total_grid_energy_exported") or _wh(
            entry, "grid_energy_exported_from_solar", "grid_energy_exported_from_battery",
            "grid_energy_exported_from_generator"),
        "solar_wh": _wh(entry, "solar_energy_exported"),
        "home_wh": home,
    }


# ── Storage ─────────────────────────────────────────────────────────────────

def _load() -> dict[str, dict]:
    """All stored rows keyed by timestamp, re-read only when the file changes."""
    path = config.ENERGY_HISTORY_FILE
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}
        if _cache["mtime"] == (path, mtime):
            return _cache["rows"]
        rows = {}
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("timestamp"):
                        rows[row["timestamp"]] = row
        except (OSError, csv.Error) as e:
            log.warning(f"Could not read energy history: {e}")
            return {}
        _cache.update(mtime=(path, mtime), rows=rows)
        return rows


def store(entries: list[dict]) -> int:
    """Merges Tesla entries into the history file; returns how many rows were new or changed."""
    new_rows = [row for row in (normalise(e) for e in entries) if row]
    if not new_rows:
        return 0
    path = config.ENERGY_HISTORY_FILE
    with _lock:
        rows = dict(_load())
        changed = 0
        for row in new_rows:
            if rows.get(row["timestamp"]) != {k: str(v) for k, v in row.items()}:
                changed += 1
            rows[row["timestamp"]] = row
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
            for ts in sorted(rows, key=lambda t: datetime.fromisoformat(t)):
                writer.writerow({k: rows[ts].get(k, "") for k in HEADERS})
        os.replace(tmp, path)
        _cache["mtime"] = None
    return changed


def _parsed_rows():
    for ts, row in _load().items():
        try:
            when = datetime.fromisoformat(ts).astimezone(config.TZ)
        except (TypeError, ValueError):
            continue
        yield when, row


def metered_days() -> set[date]:
    """Days whose history reaches the last hour, i.e. Tesla had the whole day."""
    return {when.date() for when, _ in _parsed_rows() if when.hour == 23}


# ── Daily totals ────────────────────────────────────────────────────────────

def daily_totals(start: date, end: date) -> dict[date, dict]:
    """Per-day metered kWh and TOU-priced import cost: complete days, plus today so far.

    Each interval is priced at the rate in force at its start. Tesla's
    intervals (5 or 30 minutes) never straddle a TOU boundary, since those
    fall on the hour.
    """
    today = datetime.now(config.TZ).date()
    if start <= today <= end:
        refresh_today()
    complete = metered_days() | {today}
    totals: dict[date, dict] = {}
    for when, row in _parsed_rows():
        day = when.date()
        if not (start <= day <= end) or day not in complete:
            continue
        t = totals.setdefault(day, {
            "home_kwh": 0.0, "solar_kwh": 0.0, "grid_import_kwh": 0.0, "solar_export_kwh": 0.0,
            "variable_grid_cost": 0.0,
            "on_peak_grid_kwh": 0.0, "partial_peak_grid_kwh": 0.0, "off_peak_grid_kwh": 0.0,
            "on_peak_grid_cost": 0.0, "partial_peak_grid_cost": 0.0, "off_peak_grid_cost": 0.0,
        })
        imported = _wh(row, "grid_import_wh") / 1000
        t["home_kwh"] += _wh(row, "home_wh") / 1000
        t["solar_kwh"] += _wh(row, "solar_wh") / 1000
        t["solar_export_kwh"] += _wh(row, "grid_export_wh") / 1000
        if imported:
            period = get_tou_period(when)
            bucket = period if period in ("on_peak", "partial_peak") else "off_peak"
            t["grid_import_kwh"] += imported
            cost = imported * get_tou_rate(when)
            t[f"{bucket}_grid_kwh"] += imported
            t[f"{bucket}_grid_cost"] += cost
            t["variable_grid_cost"] += cost
    return totals


# ── Sync ────────────────────────────────────────────────────────────────────

def _month_start(day: date) -> date:
    return day.replace(day=1)


def plan_fetches(today: date, have: set[date], months: int) -> list[tuple[date, date]]:
    """One call per month that has missing completed days, spanning the missing ones.

    Nightly that is just yesterday (a 5-minute day call); a first-start
    backfill is one 30-minute month call per month.
    """
    yesterday = today - timedelta(days=1)
    first = _month_start(today)
    for _ in range(max(0, months)):
        first = _month_start(first - timedelta(days=1))

    fetches = []
    month = first
    while month <= yesterday:
        nxt = _month_start(month + timedelta(days=32))
        last = min(nxt - timedelta(days=1), yesterday)
        missing = [month + timedelta(days=i) for i in range((last - month).days + 1)
                   if month + timedelta(days=i) not in have]
        if missing:
            fetches.append((missing[0], missing[-1]))
        month = nxt
    return fetches


_TODAY_MAX_AGE_S = 3600
_today_fetched_at: float | None = None


def refresh_today():
    """Today so far, fetched only when asked for and at most once an hour."""
    global _today_fetched_at
    from services import tesla_fleet

    if not tesla_fleet.is_configured():
        return
    with _lock:
        if _today_fetched_at is not None and time.monotonic() - _today_fetched_at < _TODAY_MAX_AGE_S:
            return
        _today_fetched_at = time.monotonic()
    today = datetime.now(config.TZ).date()
    try:
        store(tesla_fleet.get_energy_intervals(today, today))
    except Exception as e:
        log.warning(f"Could not fetch today's energy history: {type(e).__name__}: {e}")


def sync(now: datetime = None) -> dict:
    """Fetches any missing completed days from Tesla. Safe to call repeatedly."""
    from services import tesla_fleet

    if not tesla_fleet.is_configured():
        return {"skipped": "Tesla not configured"}
    now = now or datetime.now(config.TZ)
    # Yesterday is always re-fetched: an on-demand "today" fetch late last
    # night may have stored it only up to that moment.
    have = metered_days() - {now.date() - timedelta(days=1)}
    fetches = plan_fetches(now.date(), have, config.ENERGY_HISTORY_BACKFILL_MONTHS)
    stored = 0
    for first, last in fetches:
        try:
            stored += store(tesla_fleet.get_energy_intervals(first, last))
        except tesla_fleet.TeslaBudgetError as e:
            log.warning(f"Energy history sync stopped: {e}")
            break
        except Exception as e:
            log.warning(f"Energy history sync for {first}..{last} failed: {type(e).__name__}: {e}")
    if fetches:
        log.info(f"Energy history sync: {len(fetches)} call(s), {stored} interval(s) stored")
    return {"calls": len(fetches), "stored": stored}
