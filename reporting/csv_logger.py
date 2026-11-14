"""Telemetry persistence and energy/billing analytics over the logged history."""
import csv
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from core import config
from core.sensors import clamp_night_noise
from core.state import get_session_minutes, state
from core.tou import (
    ON_PEAK_HOURS,
    calculate_mid_bill_components,
    get_tou_period,
    get_tou_rate,
    is_expensive_period,
    is_in_night_blackout,
    is_weekend,
    provider_label,
)
from reporting.logger import log_csv

CSV_HEADERS = [
    "timestamp", "date", "time", "day_of_week", "is_weekend",
    "tou_period", "tou_rate_per_kwh", "is_expensive",
    "solar_kw", "home_kw", "solar_surplus_kw", "battery_kw", "grid_kw", "battery_pct", "self_powered_pct",
    "threshold_battery_start", "threshold_battery_stop", "charger_amperage", "charger_power_kw",
    "charger_state", "action", "reason",
    "session_active_minutes", "session_count_today", "session_stop_reason",
    "is_night_blackout", "manual_mode", "island_mode", "storm_mode",
    "est_grid_cost_this_minute", "charge_window_start_hour", "charge_window_end_hour",
    # Appended last for backward compatibility: rows written before this column
    # existed simply read as missing -> 0.0. Cumulative metered kWh reported by
    # the ChargePoint session, so analytics can eventually prefer real meter
    # deltas over the amperage x voltage x interval estimate.
    "cp_session_energy_kwh",
]

# `charger_amperage` and `charger_power_kw` replaced two always-zero columns.
# Rows written before that change surface under the legacy header names, so
# amperage lookup accepts either key.
_LEGACY_AMPERAGE_KEYS = ("charger_amperage", "threshold_solar_start")

DEFAULT_RATE = 0.1706  # Fallback when a row carries no parseable timestamp.


# ── Row helpers ─────────────────────────────────────────────────────────────

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_ev_charging_row(row: dict) -> bool:
    """True when the row represents an interval with the EV actively charging."""
    return "CHARGING" in str(row.get("charger_state", "")).upper() or \
           str(row.get("action", "")).lower() in ("start", "stop")


def _row_amperage(row: dict) -> int:
    """Charger amperage in effect for a row, with fallbacks for older rows."""
    for key in _LEGACY_AMPERAGE_KEYS:
        amp = int(_num(row.get(key)))
        if config.MIN_CHARGER_AMPERAGE <= amp <= config.MAX_CHARGER_AMPERAGE:
            return amp
    reason = f"{row.get('reason', '')} {row.get('session_stop_reason', '')}".lower()
    if "32a" in reason or "32 a" in reason:
        return config.MAX_CHARGER_AMPERAGE
    return config.DEFAULT_CHARGER_AMPERAGE


@dataclass(slots=True)
class Reading:
    """One parsed telemetry interval, normalised for analytics."""
    day: date
    hour: int
    rate: float
    solar_kw: float
    home_kw: float
    grid_kw: float
    charging: bool
    ev_power_kw: float
    session_minutes: float
    tou_period: str
    row: dict
    battery_kw: float = 0.0          # + discharging, - charging
    when: datetime | None = None     # parsed timestamp, when the row has one
    metered_kwh: float = 0.0         # cumulative ChargePoint session energy (0 = unknown)

    @property
    def interval_h(self) -> float:
        return config.CHECK_INTERVAL_MINUTES / 60.0

    @property
    def grid_import_kw(self) -> float:
        return max(0.0, self.grid_kw)

    @property
    def grid_export_kw(self) -> float:
        return max(0.0, -self.grid_kw)


def _readings(rows: list[dict], start: date = None, end: date = None):
    """Parses raw log rows into `Reading`s, optionally filtered to a date range.

    Every analytic below shares this parser; previously each re-implemented the
    same date/rate/kW coercion inline.
    """
    for row in rows:
        raw_date = row.get("date") or str(row.get("timestamp", ""))[:10]
        try:
            day = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (start and day < start) or (end and day > end):
            continue

        try:
            dt = datetime.fromisoformat(row["timestamp"])
            hour, rate, period = dt.hour, get_tou_rate(dt), get_tou_period(dt)
        except (KeyError, TypeError, ValueError):
            dt = None
            hour = int(_num(str(row.get("time", "12:00")).split(":")[0], 12))
            rate = _num(row.get("tou_rate_per_kwh"), DEFAULT_RATE) or DEFAULT_RATE
            period = str(row.get("tou_period", ""))

        charging = _is_ev_charging_row(row)

        battery_val = _num(row.get("battery_kw"))
        solar_val, home_val, grid_val = clamp_night_noise(
            hour, max(0.0, _num(row.get("solar_kw"))), max(0.0, _num(row.get("home_kw"))),
            _num(row.get("grid_kw")), battery_val)

        yield Reading(
            day=day,
            hour=hour,
            rate=rate,
            solar_kw=solar_val,
            home_kw=home_val,
            grid_kw=grid_val,
            charging=charging,
            ev_power_kw=state.charger_power_kw(_row_amperage(row)) if charging else 0.0,
            session_minutes=_num(row.get("session_active_minutes")),
            tou_period=period,
            row=row,
            battery_kw=battery_val,
            when=dt,
            metered_kwh=max(0.0, _num(row.get("cp_session_energy_kwh"))),
        )


# ── EV energy per interval ──────────────────────────────────────────────────

@dataclass(slots=True)
class EvInterval:
    """EV energy delivered in one logged interval, split by where it came from."""
    reading: Reading
    session: int            # sequence number of the charging session; -1 when idle
    kwh: float = 0.0
    solar_kwh: float = 0.0
    battery_kwh: float = 0.0
    grid_kwh: float = 0.0
    prev_when: datetime | None = None

    @property
    def grid_cost(self) -> float:
        return self.grid_kwh * self.reading.rate


def _starts_new_session(prev: Reading | None, r: Reading) -> bool:
    """True when charging row `r` begins a different session from `prev`.

    Rows are contiguous in the log, so a session that ends at 00:15 and another
    that starts at 00:30 would otherwise merge into one. A stop row, the
    session clock going backwards, or the ChargePoint meter resetting all mark
    a boundary.
    """
    if prev is None or not prev.charging:
        return True
    if str(prev.row.get("action", "")).lower() == "stop":
        return True
    if str(r.row.get("action", "")).lower() == "start":
        return True
    if r.session_minutes and prev.session_minutes and r.session_minutes + 1.0 < prev.session_minutes:
        return True
    return bool(r.metered_kwh and prev.metered_kwh and r.metered_kwh + 0.05 < prev.metered_kwh)


def _split_sources(r: Reading, kwh: float, hours: float) -> tuple[float, float, float]:
    """(solar, battery, grid) kWh behind `kwh` delivered to the car over `hours`.

    The house's own load is served first: solar, then Powerwall discharge,
    then grid. Whatever the car drew beyond what solar and the Powerwall
    measurably supplied came from the grid. (Previously the unexplained
    remainder was labelled "Powerwall", so any overestimate of the car's draw
    showed up as phantom battery energy.)
    """
    if kwh <= 0 or hours <= 0:
        return 0.0, 0.0, 0.0
    ev_kw = kwh / hours
    house_kw = max(0.0, r.home_kw - ev_kw)
    solar_house = min(r.solar_kw, house_kw)
    solar_ev = min(ev_kw, max(0.0, r.solar_kw - solar_house))
    discharge = max(0.0, r.battery_kw)
    battery_house = min(discharge, house_kw - solar_house)
    battery_ev = min(ev_kw - solar_ev, max(0.0, discharge - battery_house))
    solar_kwh, battery_kwh = solar_ev * hours, battery_ev * hours
    return solar_kwh, battery_kwh, max(0.0, kwh - solar_kwh - battery_kwh)


def _ev_intervals(readings) -> list[EvInterval]:
    """EV energy for every reading, in log order.

    The car's energy comes from the ChargePoint meter: the growth of the
    session's cumulative kWh since the previous row. That stays correct when
    the car tapers or stops drawing at its charge limit while the charger
    still reports CHARGING. Sessions with no meter readings at all (older
    rows) fall back to amperage x voltage x interval.
    """
    out: list[EvInterval] = []
    session_rows: list[EvInterval] = []
    prev: Reading | None = None
    session_no = -1
    default_h = config.CHECK_INTERVAL_MINUTES / 60.0

    def close_session():
        metered = any(iv.reading.metered_kwh for iv in session_rows)
        last_meter = 0.0
        # A log that begins mid-session has no baseline for its first reading.
        if session_rows and out and out[0] is session_rows[0]:
            first = session_rows[0].reading
            if first.session_minutes > 2 * config.CHECK_INTERVAL_MINUTES:
                last_meter = first.metered_kwh
        for iv in session_rows:
            r = iv.reading
            if metered:
                if r.metered_kwh <= 0:
                    continue  # missed reading; the next one carries the delta
                iv.kwh = max(0.0, r.metered_kwh - last_meter)
                last_meter = max(last_meter, r.metered_kwh)
            elif str(r.row.get("action", "")).lower() != "stop":
                # Estimate: each row stands for the interval that follows it, and
                # nothing flows after the charger was stopped.
                iv.kwh = r.ev_power_kw * default_h
            hours = default_h
            if iv.prev_when is not None and r.when is not None:
                gap = (r.when - iv.prev_when).total_seconds() / 3600.0
                if 0 < gap <= 2 * default_h:
                    hours = gap
            iv.solar_kwh, iv.battery_kwh, iv.grid_kwh = _split_sources(r, iv.kwh, hours)
        session_rows.clear()

    for r in readings:
        if r.charging:
            if _starts_new_session(prev, r):
                close_session()
                session_no += 1
            iv = EvInterval(r, session_no, prev_when=prev.when if prev is not None else None)
            session_rows.append(iv)
        else:
            close_session()
            iv = EvInterval(r, -1)
        out.append(iv)
        prev = r
    close_session()
    return out


# ── Writing ─────────────────────────────────────────────────────────────────

def log_to_csv(stats: dict, action: str, reason: str, now: datetime):
    """Appends one telemetry row to the local CSV and queues it for the cloud."""
    global _log_rows_cache, _rows_version
    rate = get_tou_rate(now)
    est_cost = round(max(0.0, _num(stats.get("grid_kw"))) * rate * (config.CHECK_INTERVAL_MINUTES / 60.0), 4)
    charging = state.charger_state == state.State.CHARGING
    session_mins = get_session_minutes() if (charging or action == "stop") else 0.0

    row = [
        now.isoformat(), now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.strftime("%A"), is_weekend(now),
        get_tou_period(now), rate, is_expensive_period(now),
        stats.get("solar_kw", 0.0), stats.get("home_kw", 0.0), stats.get("solar_surplus_kw", 0.0),
        stats.get("battery_kw", 0.0), stats.get("grid_kw", 0.0), stats.get("battery_pct", 0.0),
        stats.get("self_powered_pct", 100.0),
        config.BATTERY_START_PCT, config.BATTERY_STOP_PCT,
        state.active_amperage, state.charger_power_kw(),
        state.charger_state, action, reason,
        session_mins, state.session_count_today, state.session_stop_reason or "",
        is_in_night_blackout(now), state.manual_mode,
        stats.get("island_mode", "on_grid"), stats.get("storm_mode", False),
        est_cost, config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR,
        _num(stats.get("cp_session_energy_kwh")),
    ]

    try:
        os.makedirs(os.path.dirname(config.CSV_LOG_FILE) or ".", exist_ok=True)
        write_header = not os.path.exists(config.CSV_LOG_FILE)
        with open(config.CSV_LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_HEADERS)
            writer.writerow(row)
    except OSError as e:
        log_csv.error(f"Failed to write local CSV row: {e}")

    try:
        from services.sheets_db import append_log_row
        append_log_row(row)
    except Exception as e:
        log_csv.error(f"Failed to queue row for Google Sheets: {e}")

    with _log_rows_cache_lock:
        _log_rows_cache = None
        _rows_version += 1


def rows_version() -> int:
    """Increments whenever a telemetry row is written, so derived caches know to rebuild."""
    with _log_rows_cache_lock:
        return _rows_version


# ── Reading ─────────────────────────────────────────────────────────────────

_log_rows_cache: list[dict] | None = None
_log_rows_cache_time = 0.0
_log_rows_cache_days = 0  # how many days the cached rows cover
_rows_version = 0
# Guards the cache tuple; read from the cycle, bot, and agent threads.
_log_rows_cache_lock = threading.Lock()
LOG_CACHE_TTL = 60.0


def get_all_log_rows(days: int = 7, force_refresh: bool = False) -> list[dict]:
    """Returns recent telemetry rows from Google Sheets, falling back to CSV.

    Sheets is the source of truth because it survives container restarts; a
    60-second in-memory cache keeps repeated analytics off the network.
    """
    global _log_rows_cache, _log_rows_cache_time, _log_rows_cache_days
    with _log_rows_cache_lock:
        # A cache covering fewer days than requested would silently truncate
        # longer periods (e.g. "this month" answered from 7 days of rows).
        if (not force_refresh and _log_rows_cache is not None and _log_rows_cache_days >= days
                and (time.time() - _log_rows_cache_time) < LOG_CACHE_TTL):
            return _log_rows_cache

    rows = []
    try:
        from services.sheets_db import get_recent_logs
        rows = get_recent_logs(days=days)
    except Exception as e:
        log_csv.warning(f"Google Sheets unavailable, falling back to local CSV: {e}")

    if not rows and os.path.exists(config.CSV_LOG_FILE):
        try:
            with open(config.CSV_LOG_FILE, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError as e:
            log_csv.warning(f"Error reading local CSV fallback: {e}")

    with _log_rows_cache_lock:
        _log_rows_cache, _log_rows_cache_time, _log_rows_cache_days = rows, time.time(), days
    return rows


def _rows_since(start: date | None) -> list[dict]:
    """Log rows reaching back far enough to cover a period beginning on `start`."""
    if start is None:
        return get_all_log_rows()
    return get_all_log_rows(days=max(7, (datetime.now(config.TZ).date() - start).days + 1))


def _ev_intervals_between(start: date | None, end: date | None = None) -> list[EvInterval]:
    """EV intervals for log rows from the day before `start` through `end`.

    The extra day gives a session that crosses midnight its meter baseline and
    its start, so only its after-midnight part is credited to `start`.
    """
    lookback = start - timedelta(days=1) if start else None
    return _ev_intervals(_readings(_rows_since(lookback), lookback, end))


def get_recent_sessions(limit: int = 5, period: str = None) -> list[dict]:
    """Distinct charging sessions, newest first.

    If period is supplied (e.g. 'today', 'yesterday', 'this_week', 'YYYY-MM-DD'),
    only sessions with charging inside that date window are returned.
    """
    start = end = None
    if period:
        start, end, _ = _resolve_date_range(period, datetime.now(config.TZ))

    def stamp(row):
        return row.get("timestamp") or f"{row.get('date', '')} {row.get('time', '')}".strip()

    sessions: dict[int, dict] = {}
    previous = None
    for iv in _ev_intervals_between(start, end):
        r, row = iv.reading, iv.reading.row
        if iv.session >= 0:
            current = sessions.get(iv.session)
            if current is None:
                current = sessions[iv.session] = {
                    "start_time": stamp(row),
                    "start_battery_pct": row.get("battery_pct", "N/A"),
                    "solar_kw": row.get("solar_kw", "0"),
                    "max_duration_minutes": r.session_minutes,
                    "stop_reason": row.get("session_stop_reason") or row.get("reason", ""),
                    "energy_kwh": 0.0,
                    "_in_period": False,
                }
            current["end_time"] = stamp(row)
            current["end_battery_pct"] = row.get("battery_pct", "N/A")
            current["max_duration_minutes"] = max(current["max_duration_minutes"], r.session_minutes)
            current["energy_kwh"] += iv.kwh
            current["_in_period"] |= start is None or start <= r.day
            if row.get("session_stop_reason"):
                current["stop_reason"] = row["session_stop_reason"]
        elif previous is not None and previous.session >= 0:
            current = sessions[previous.session]
            current["max_duration_minutes"] = max(current["max_duration_minutes"], r.session_minutes)
            current["stop_reason"] = row.get("session_stop_reason") or row.get("reason") or current["stop_reason"]
        previous = iv

    result = []
    for session in sessions.values():
        if not session.pop("_in_period"):
            continue
        session["energy_kwh"] = round(session["energy_kwh"], 2)
        session["miles_added"] = round(session["energy_kwh"] * config.EV_MILES_PER_KWH, 1)
        result.append(session)
    return result[-limit:][::-1] if limit else result[::-1]


def get_ev_kwh_since(since: datetime) -> float:
    """EV energy delivered from `since` onwards (ChargePoint meter where logged)."""
    total = 0.0
    for iv in _ev_intervals_between(since.date()):
        when = iv.reading.when
        if when is None:
            continue
        if (when.tzinfo is None) != (since.tzinfo is None):
            when = when.replace(tzinfo=since.tzinfo)
        if when >= since:
            total += iv.kwh
    return round(total, 2)


def get_hourly_profile(period: str = "today") -> list[dict]:
    """Average solar, home load, and Powerwall charge per hour of a single day."""
    day, _, _ = _resolve_date_range(period)
    buckets: dict[int, list[tuple[float, float, float]]] = {}
    for r in _readings(_rows_since(day), day, day):
        buckets.setdefault(r.hour, []).append((r.solar_kw, r.home_kw, _num(r.row.get("battery_pct"))))

    def avg(values, index):
        return round(sum(v[index] for v in values) / len(values), 2)

    return [
        {"hour": hour, "solar_kw": avg(vals, 0), "home_kw": avg(vals, 1), "battery_pct": avg(vals, 2)}
        for hour, vals in sorted(buckets.items())
    ]


# ── Period resolution ───────────────────────────────────────────────────────

_MONTH_FORMATS = ("%Y-%m", "%Y/%m", "%m/%Y", "%B %Y", "%b %Y", "%B", "%b")

_PERIOD_ALIASES = {
    "today": "today", "": "today",
    "yesterday": "yesterday",
    "this_week": "this_week", "week": "this_week", "this week": "this_week",
    "last_week": "last_week", "last week": "last_week",
    "previous_week": "last_week", "previous week": "last_week",
    "7days": "7_days", "7_days": "7_days", "last_7_days": "7_days", "last 7 days": "7_days",
    "past_7_days": "7_days", "past 7 days": "7_days", "past week": "7_days",
    "this_month": "this_month", "month": "this_month", "this month": "this_month",
    "30days": "this_month", "30 days": "this_month",
    "last_month": "last_month", "last month": "last_month",
    "previous_month": "last_month", "previous month": "last_month",
}


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1)) - timedelta(days=1)
    return start, end


def _resolve_date_range(period: str, now: datetime = None, default: str = "today") -> tuple[date, date, str]:
    """Resolves a natural-language period into (start, end, label).

    Shared by every analytic so that 'July 2026' means the same thing whether
    it reaches the daily cost report or the monthly bill generator.
    """
    now = now or datetime.now(config.TZ)
    today = now.date()
    clean = str(period or default).lower().strip()
    key = _PERIOD_ALIASES.get(clean, clean if clean in _PERIOD_ALIASES.values() else None)

    if key == "today":
        return today, today, f"Today ({today})"
    if key == "yesterday":
        y = today - timedelta(days=1)
        return y, y, f"Yesterday ({y})"
    if key == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today, f"This Week ({start} to {today})"
    if key == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
        return start, end, f"Last Week ({start} to {end})"
    if key == "7_days":
        start = today - timedelta(days=7)
        return start, today, f"Past 7 Days ({start} to {today})"
    if key == "this_month":
        start = today.replace(day=1)
        return start, today, f"This Month ({start.strftime('%B %Y')})"
    if key == "last_month":
        start, end = _month_bounds(today.replace(day=1) - timedelta(days=1))
        return start, end, f"Last Month ({start.strftime('%B %Y')})"

    for fmt in _MONTH_FORMATS:
        try:
            parsed = datetime.strptime(clean, fmt)
        except ValueError:
            continue
        year = parsed.year
        if fmt in ("%B", "%b"):  # Bare month name means the most recent one.
            year = now.year - (1 if parsed.month > now.month else 0)
        start, end = _month_bounds(date(year, parsed.month, 1))
        return start, end, start.strftime("%B %Y")

    try:
        exact = datetime.strptime(clean, "%Y-%m-%d").date()
        return exact, exact, f"Date ({exact})"
    except ValueError:
        return _resolve_date_range(default, now, default="today")


# ── Analytics ───────────────────────────────────────────────────────────────

def get_daily_charging_cost(period: str = "today") -> dict:
    """Energy, cost, and driving range delivered to the EV over a period."""
    now = datetime.now(config.TZ)
    start, end, label = _resolve_date_range(period, now)
    rows = _rows_since(start)
    if not rows:
        return {"error": "No log data found yet."}

    def minutes_into_day(r: Reading) -> float:
        return r.hour * 60 + _num(str(r.row.get("time", "")).partition(":")[2])

    try:
        sessions: dict[int, dict] = {}
        total_kwh = grid_kwh = grid_cost = solar_kwh = battery_kwh = 0.0
        intervals = 0
        last = None

        for iv in _ev_intervals_between(start, end):
            r = iv.reading
            if r.day < start:
                continue
            if iv.session >= 0:
                intervals += 1
                current = sessions.get(iv.session)
                if current is None:
                    # session_minutes counts from the session's start, so a session
                    # carried over from before midnight would credit the earlier
                    # day's minutes to this period; those are subtracted here.
                    current = sessions[iv.session] = {
                        "minutes": r.session_minutes,
                        "carried": max(0.0, r.session_minutes - minutes_into_day(r)) if r.day == start else 0.0,
                    }
                current["minutes"] = max(current["minutes"], r.session_minutes)
                total_kwh += iv.kwh
                grid_kwh += iv.grid_kwh
                grid_cost += iv.grid_cost
                solar_kwh += iv.solar_kwh
                battery_kwh += iv.battery_kwh
            elif last is not None and last.session >= 0 and last.session in sessions:
                current = sessions[last.session]
                current["minutes"] = max(current["minutes"], r.session_minutes)
            last = iv

        if last is not None and last.session in sessions and last.reading.day < now.date():
            # Still charging when the period's last day ended: credit the time
            # up to midnight (at most one interval) that the next day's rows hold.
            sessions[last.session]["minutes"] += min(config.CHECK_INTERVAL_MINUTES,
                                                     24 * 60 - minutes_into_day(last.reading))

        total_minutes = round(sum(max(0.0, s["minutes"] - s["carried"]) for s in sessions.values()), 1)
        if not total_minutes and intervals:
            total_minutes = round(intervals * config.CHECK_INTERVAL_MINUTES, 1)

        total_kwh = round(total_kwh, 2)
        from_grid = round(grid_kwh, 2)
        from_solar = round(solar_kwh, 2)
        from_battery = round(battery_kwh, 2)
        self_powered = round(from_solar + from_battery, 2)

        return {
            "period": label,
            "total_sessions_count": len(sessions),
            "total_charging_hours": round(total_minutes / 60.0, 1),
            "total_charging_minutes": total_minutes,
            "charging_intervals_count": intervals,
            "ev_grid_kwh_pulled": from_grid,
            "solar_kwh_used": from_solar,
            "powerwall_battery_kwh_used": from_battery,
            "total_self_powered_kwh": self_powered,
            "total_kwh_added": total_kwh,
            "estimated_miles_added": round(total_kwh * config.EV_MILES_PER_KWH, 1),
            "ev_grid_cost_dollars": round(grid_cost, 2),
            "grid_percentage": round(from_grid / total_kwh * 100.0, 1) if total_kwh else 0.0,
            "solar_percentage": round(self_powered / total_kwh * 100.0, 1) if total_kwh else 100.0,
            "estimated_solar_savings_dollars": round(self_powered * get_tou_rate(now), 2),
            "calculation_note": ("Energy is the ChargePoint meter reading per interval (amperage x time only for "
                                 "older rows without it). Each interval is split using that interval's measured "
                                 "solar surplus and Powerwall discharge; the rest is grid."),
        }
    except Exception as e:
        log_csv.error(f"Error calculating charging cost summary: {e}", exc_info=True)
        return {"error": f"Failed to calculate charging cost: {e}"}


def _fixed_fee(days: int) -> float:
    if config._IS_MID:
        if days >= 28:
            return config.UTILITY_FIXED_MONTHLY_FEE
        return round(config.UTILITY_FIXED_MONTHLY_FEE / 30.0 * days, 2)
    return config.UTILITY_FIXED_MONTHLY_FEE / 30.0 * days * config.UTILITY_TAX_MULTIPLIER


def _export_credit_rate() -> float:
    if config._IS_MID:
        return config.UTILITY_SOLAR_EXPORT_CREDIT_RATE
    return config.UTILITY_SOLAR_EXPORT_CREDIT_RATE * config.UTILITY_TAX_MULTIPLIER


def _self_powered_pct(home_kwh: float, grid_kwh: float) -> float:
    if home_kwh <= 0:
        return 100.0
    return round(max(0.0, min(100.0, (1 - grid_kwh / home_kwh) * 100.0)), 1)


def get_home_energy_summary(period: str = "today") -> dict:
    """Whole-home consumption, generation, and utility bill breakdown."""
    start, end, label = _resolve_date_range(period)
    days = (end - start).days + 1
    rows = _rows_since(start)
    if not rows:
        return {"error": "No log data found yet."}

    try:
        home = solar = imported = exported = 0.0
        grid_cost = export_credit = 0.0
        ev_kwh = ev_grid_kwh = ev_solar_kwh = ev_cost = 0.0

        for iv in _ev_intervals_between(start, end):
            r = iv.reading
            if r.day < start:
                continue
            h = r.interval_h
            home += r.home_kw * h
            solar += r.solar_kw * h
            ev_kwh += iv.kwh
            ev_grid_kwh += iv.grid_kwh
            ev_cost += iv.grid_cost
            ev_solar_kwh += iv.solar_kwh
            if r.grid_kw > 0:
                imported += r.grid_import_kw * h
                grid_cost += r.grid_import_kw * h * r.rate
            else:
                exported += r.grid_export_kw * h
                export_credit += r.grid_export_kw * h * _export_credit_rate()

        fixed = _fixed_fee(days)
        appliance_cost = max(0.0, grid_cost - ev_cost)

        if not ev_kwh:
            ev_summary = "No EV charging recorded in this period"
        elif not ev_cost:
            ev_summary = f"{round(ev_kwh, 1)} kWh added (100% solar/battery self-powered, $0.00 grid cost)"
        else:
            ev_summary = f"{round(ev_kwh, 1)} kWh added (${ev_cost:.2f} grid cost, {round(ev_solar_kwh, 1)} kWh direct solar)"

        return {
            "period": label,
            "period_days_count": days,
            "total_home_consumption_kwh": round(home, 2),
            "total_solar_generated_kwh": round(solar, 2),
            "total_grid_imported_kwh": round(imported, 2),
            "total_solar_exported_kwh": round(exported, 2),
            "fixed_service_fee_dollars": round(fixed, 2),
            "grid_delivered_energy_cost_dollars": round(grid_cost, 2),
            "solar_export_credit_dollars": round(export_credit, 2),
            "estimated_total_mid_utility_bill_dollars": round(max(0.0, fixed + grid_cost - export_credit), 2),
            "ev_charging_share_of_bill_dollars": round(ev_cost, 2),
            "ev_charging_total_kwh": round(ev_kwh, 2),
            "ev_estimated_miles_added": round(ev_kwh * config.EV_MILES_PER_KWH, 1),
            "ev_solar_kwh_used": round(ev_solar_kwh, 2),
            "ev_grid_kwh_used": round(ev_grid_kwh, 2),
            "ev_charging_summary": ev_summary,
            "home_appliances_grid_energy_cost_dollars": round(appliance_cost, 2),
            "home_appliances_share_of_bill_dollars": round(appliance_cost, 2),
            "home_self_powered_percentage": _self_powered_pct(home, imported),
            "utility_rate_plan": provider_label(),
        }
    except Exception as e:
        log_csv.error(f"Error calculating home energy summary: {e}", exc_info=True)
        return {"error": f"Failed to calculate home energy summary: {e}"}


_TOU_LABELS = (("on_peak", "On-peak"), ("partial_peak", "Partial-peak"), ("off_peak", "Off-peak"))


def get_bill_breakdown(period: str = "today") -> dict:
    """Itemised utility charges accrued so far in a period, grouped like the bill.

    Unlike get_monthly_billing_data this never scales up missing intervals:
    it reports what has actually been metered, so 'today' means 'so far today'.
    """
    start, end, label = _resolve_date_range(period)
    days = (end - start).days + 1
    rows = _rows_since(start)
    if not rows:
        return {"error": "No log data found yet."}

    grid_kwh = dict.fromkeys((key for key, _ in _TOU_LABELS), 0.0)
    taxed_cost = dict.fromkeys(grid_kwh, 0.0)
    export_kwh = 0.0
    for r in _readings(rows, start, end):
        h = r.interval_h
        if r.grid_kw > 0:
            bucket = r.tou_period if r.tou_period in grid_kwh else "off_peak"
            grid_kwh[bucket] += r.grid_import_kw * h
            taxed_cost[bucket] += r.grid_import_kw * h * r.rate
        else:
            export_kwh += r.grid_export_kw * h

    def line(text, amount, detail=""):
        return {"label": text, "detail": detail, "amount": round(amount, 2)}

    def group(title, lines):
        return {"title": title, "lines": lines, "subtotal": round(sum(item["amount"] for item in lines), 2)}

    fee = config.UTILITY_FIXED_MONTHLY_FEE
    fee_detail = f"${fee:.2f}/month, {days} day{'s' if days != 1 else ''}"
    if config._IS_MID:
        bill = calculate_mid_bill_components(
            on_peak_kwh=grid_kwh["on_peak"], partial_peak_kwh=grid_kwh["partial_peak"],
            off_peak_kwh=grid_kwh["off_peak"], export_kwh=export_kwh, month=start.month, days=days,
        )
        energy = [line(name, bill[f"{key}_cost"], f"{grid_kwh[key]:.1f} kWh") for key, name in _TOU_LABELS]
        fixed = [
            line("Customer service charge", bill["fixed_fee"], fee_detail),
            line("Energy efficiency (EEA)", bill["eea_cost"], f"${config.UTILITY_EEA_RATE}/kWh"),
            line("Climate initiative (CIA)", bill["cia_cost"], f"${config.UTILITY_CIA_RATE}/kWh"),
            line("State surcharge", bill["state_surcharge"], f"${config.UTILITY_STATE_SURCHARGE_RATE}/kWh"),
            line("Local surcharge", bill["local_surcharge"], f"{config.UTILITY_LOCAL_SURCHARGE_PCT * 100:g}% of MID charges"),
        ]
        credit_rate, total = bill["export_credit_rate"], bill["net_bill"]
    else:
        energy = [line(name, taxed_cost[key], f"{grid_kwh[key]:.1f} kWh, incl. taxes") for key, name in _TOU_LABELS]
        fixed = [line("Customer service charge", _fixed_fee(days), fee_detail)]
        credit_rate, total = _export_credit_rate(), None

    credits = [line("Solar export credit", -export_kwh * credit_rate, f"{export_kwh:.1f} kWh × ${credit_rate:.3f}")]
    groups = [group("Energy used", energy), group("Fixed charges & taxes", fixed), group("Credits", credits)]
    if total is None:
        total = max(0.0, sum(g["subtotal"] for g in groups))

    return {
        "period": label,
        "days": days,
        "rate_plan": provider_label(),
        "grid_import_kwh": round(sum(grid_kwh.values()), 2),
        "groups": groups,
        "total_dollars": round(total, 2),
    }


EVENING_START_HOUR, EVENING_END_HOUR = 16, 22


def get_energy_saving_advice() -> dict:
    """Derives solar windows, evening battery needs, and actionable bill tips."""
    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    now = datetime.now(config.TZ)
    since = (now - timedelta(days=7)).date()

    try:
        # (solar kW, house kW excluding the charger) per interval, by hour.
        hourly: dict[int, list[tuple[float, float]]] = {h: [] for h in range(24)}
        on_peak_cost = ev_grid_cost = 0.0
        evening: dict[date, list[float]] = {}

        for iv in _ev_intervals(_readings(rows, start=since - timedelta(days=1))):
            r = iv.reading
            if r.day < since:
                continue
            h = r.interval_h
            ev_kw = iv.kwh / h if h else 0.0
            hourly[r.hour].append((r.solar_kw, max(0.0, r.home_kw - ev_kw)))

            if r.tou_period == "on_peak":
                on_peak_cost += r.grid_import_kw * h * r.rate
            ev_grid_cost += iv.grid_cost

            if EVENING_START_HOUR <= r.hour < EVENING_END_HOUR:
                # Exclude the charger so evening planning reflects the house alone.
                appliance_kw = max(0.0, r.home_kw - ev_kw)
                totals = evening.setdefault(r.day, [0.0, 0.0, 0.0])
                totals[0] += appliance_kw * h
                totals[1] += r.solar_kw * h
                totals[2] += max(0.0, appliance_kw - r.solar_kw) * h

        def evening_avg(index: int, fallback: float) -> float:
            return sum(v[index] for v in evening.values()) / len(evening) if evening else fallback

        appliance_kwh = evening_avg(0, 5.0)
        evening_solar_kwh = evening_avg(1, 2.0)
        deficit_kwh = evening_avg(2, 3.5)

        # Reserve enough Powerwall to cover the evening deficit, plus 10% buffer.
        reserve_pct = round(min(60.0, max(25.0, deficit_kwh / config.POWERWALL_USABLE_KWH * 100.0 + 10.0)), 1)

        profile = [
            {
                "hour": hour,
                "solar_kw": round(sum(v[0] for v in vals) / len(vals), 2),
                "house_kw": round(sum(v[1] for v in vals) / len(vals), 2),
            }
            for hour, vals in hourly.items() if vals
        ]
        best_hours = [p["hour"] for p in profile if p["solar_kw"] - p["house_kw"] >= 1.0]
        window = f"{min(best_hours)}:00 - {max(best_hours) + 1}:00" if best_hours else "10:00 - 15:00"

        peak_start, peak_end = ON_PEAK_HOURS
        peak_rate = get_tou_rate(now.replace(hour=peak_start + 1, minute=0))
        export_rate = _export_credit_rate()
        avoid_window = f"{peak_start}:00 - {peak_end}:00 (On-Peak ${peak_rate:.2f}/kWh rate)"

        return {
            "optimal_solar_appliance_window": window,
            "cheapest_ev_charging_window": f"{window} (solar surplus)",
            "hours_to_avoid_heavy_loads": avoid_window,
            "avg_evening_appliance_load_kwh": round(appliance_kwh, 2),
            "avg_evening_solar_generation_kwh": round(evening_solar_kwh, 2),
            "avg_evening_net_battery_deficit_kwh": round(deficit_kwh, 2),
            "recommended_evening_battery_reserve_pct": reserve_pct,
            "on_peak_grid_cost_last_7_days": round(on_peak_cost, 2),
            "ev_grid_charging_cost_last_7_days": round(ev_grid_cost, 2),
            "hourly_profile_7_days": profile,
            "actionable_recommendations": [
                f"Run heavy appliances (AC, washer, dishwasher, dryer) during {window} when solar generation peaks.",
                f"Avoid heavy appliances between {peak_start}:00 and {peak_end}:00, "
                f"when the on-peak rate is ${peak_rate:.2f}/kWh.",
                f"Using solar directly saves {peak_rate / export_rate:.1f}x more than exporting it "
                f"(${peak_rate:.2f}/kWh avoided vs ${export_rate:.3f}/kWh export credit).",
                f"Charge the EV during the {window} solar surplus to avoid pulling grid power at night.",
                f"Evening appliance load averages {round(appliance_kwh, 1)} kWh, offset by {round(evening_solar_kwh, 1)} kWh "
                f"of late solar. Hold at least {reserve_pct}% Powerwall at {EVENING_START_HOUR}:00 to cover the "
                f"{round(deficit_kwh, 1)} kWh evening deficit.",
            ],
        }
    except Exception as e:
        log_csv.error(f"Error calculating energy saving advice: {e}", exc_info=True)
        return {"error": f"Failed to calculate energy saving advice: {e}"}


def get_monthly_billing_data(period: str = "last_month") -> dict:
    """Per-day usage records and the monthly bill summary for a given month."""
    now = datetime.now(config.TZ)
    start, end, _ = _resolve_date_range(period, now, default="last_month")
    if start > now.date():
        return {"error": f"Cannot generate a monthly report for a future month ({start.strftime('%B %Y')})."}

    rows = get_all_log_rows(days=365)
    if not rows:
        return {"error": "No log data available for monthly report."}

    fields = ("home_kwh", "solar_kwh", "grid_import_kwh", "solar_export_kwh",
              "variable_grid_cost", "solar_export_credit", "ev_grid_kwh", "ev_grid_cost",
              "on_peak_grid_kwh", "partial_peak_grid_kwh", "off_peak_grid_kwh")

    daily = {}
    day = start
    while day <= end:
        daily[day] = {
            "date": day.strftime("%Y-%m-%d"),
            "date_short": day.strftime("%b %d"),
            "day_num": day.day,
            "readings_count": 0,
            **{f: 0.0 for f in fields},
        }
        day += timedelta(days=1)

    for iv in _ev_intervals(_readings(rows, start - timedelta(days=1), end)):
        r = iv.reading
        entry = daily.get(r.day)
        if entry is None:
            continue
        h = r.interval_h
        entry["readings_count"] += 1
        entry["home_kwh"] += r.home_kw * h
        entry["solar_kwh"] += r.solar_kw * h
        entry["ev_grid_kwh"] += iv.grid_kwh
        entry["ev_grid_cost"] += iv.grid_cost
        if r.grid_kw > 0:
            entry["grid_import_kwh"] += r.grid_import_kw * h
            entry["variable_grid_cost"] += r.grid_import_kw * h * r.rate
            bucket = r.tou_period if r.tou_period in ("on_peak", "partial_peak") else "off_peak"
            entry[f"{bucket}_grid_kwh"] += r.grid_import_kw * h
        else:
            entry["solar_export_kwh"] += r.grid_export_kw * h
            entry["solar_export_credit"] += r.grid_export_kw * h * _export_credit_rate()

    expected = int(24 * 60 / max(1, config.CHECK_INTERVAL_MINUTES))
    records, totals = [], dict.fromkeys(fields, 0.0)

    # Scale up days that missed intervals (restarts, updates, downtime)
    # so a gap does not read as artificially zero consumption.
    valid_days = [d for d in daily if daily[d]["readings_count"] > 0]
    avg_per_day = {
        f: (sum(daily[d][f] for d in valid_days) / len(valid_days)) if valid_days else 0.0
        for f in fields
    }

    for day in sorted(daily):
        entry = daily[day]
        count = entry["readings_count"]
        if 0 < count < expected:
            scale = expected / count
            for f in fields:
                entry[f] *= scale
        elif count == 0 and valid_days:
            for f in fields:
                entry[f] = avg_per_day[f]

        for f in fields:
            totals[f] += entry[f]
            entry[f] = round(entry[f], 2)
        entry["net_variable_cost"] = round(max(0.0, entry["variable_grid_cost"] - entry["solar_export_credit"]), 2)
        records.append(entry)

    days_count = (end - start).days + 1
    fixed = _fixed_fee(days_count)

    # Use exact MID tariff line-item reconciliation when configured for MID
    if config._IS_MID:
        bill_comp = calculate_mid_bill_components(
            on_peak_kwh=totals["on_peak_grid_kwh"],
            partial_peak_kwh=totals["partial_peak_grid_kwh"],
            off_peak_kwh=totals["off_peak_grid_kwh"],
            export_kwh=totals["solar_export_kwh"],
            month=start.month,
            days=days_count,
        )
        fixed_fee = bill_comp["fixed_fee"]
        total_var_cost = round(bill_comp["energy_cost"] + bill_comp["volumetric_adjustments"] + bill_comp["local_surcharge"], 2)
        export_credit = bill_comp["export_credit"]
        net_bill = bill_comp["net_bill"]
    else:
        fixed_fee = round(fixed, 2)
        total_var_cost = round(totals["variable_grid_cost"], 2)
        export_credit = round(totals["solar_export_credit"], 2)
        net_bill = round(max(0.0, fixed + totals["variable_grid_cost"] - totals["solar_export_credit"]), 2)

    return {
        "month_label": start.strftime("%B %Y"),
        "start_date": str(start),
        "end_date": str(end),
        "days_count": days_count,
        "total_home_kwh": round(totals["home_kwh"], 2),
        "total_solar_kwh": round(totals["solar_kwh"], 2),
        "total_grid_import_kwh": round(totals["grid_import_kwh"], 2),
        "total_solar_export_kwh": round(totals["solar_export_kwh"], 2),
        "total_variable_grid_cost_dollars": total_var_cost,
        "total_solar_export_credit_dollars": export_credit,
        "fixed_service_fee_dollars": fixed_fee,
        "estimated_net_bill_dollars": net_bill,
        "ev_charging_kwh": round(totals["ev_grid_kwh"], 2),
        "ev_charging_cost_dollars": round(totals["ev_grid_cost"], 2),
        "home_appliances_cost_dollars": round(max(0.0, total_var_cost - totals["ev_grid_cost"]), 2),
        "self_powered_percentage": _self_powered_pct(totals["home_kwh"], totals["grid_import_kwh"]),
        "utility_rate_plan": provider_label(),
        "daily_records": records,
    }
