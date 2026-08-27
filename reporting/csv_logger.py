"""Telemetry persistence and energy/billing analytics over the logged history."""
import csv
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from core import config, state
from core.state import get_session_minutes
from core.tou import (
    get_tou_period, get_tou_rate, is_expensive_period,
    is_in_night_blackout, is_weekend, provider_label,
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

    @property
    def interval_h(self) -> float:
        return config.CHECK_INTERVAL_MINUTES / 60.0

    @property
    def grid_import_kw(self) -> float:
        return max(0.0, self.grid_kw)

    @property
    def grid_export_kw(self) -> float:
        return max(0.0, -self.grid_kw)

    @property
    def ev_grid_kw(self) -> float:
        """Share of the grid import attributable to the charger."""
        return min(self.grid_import_kw, self.ev_power_kw) if self.charging else 0.0

    @property
    def ev_solar_kw(self) -> float:
        """Solar power flowing into the charger after the rest of the house."""
        if not self.charging:
            return 0.0
        return min(max(0.0, self.solar_kw - max(0.0, self.home_kw - self.ev_power_kw)), self.ev_power_kw)


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
            hour = int(_num(str(row.get("time", "12:00")).split(":")[0], 12))
            rate = _num(row.get("tou_rate_per_kwh"), DEFAULT_RATE) or DEFAULT_RATE
            period = str(row.get("tou_period", ""))

        charging = _is_ev_charging_row(row)
        yield Reading(
            day=day,
            hour=hour,
            rate=rate,
            solar_kw=max(0.0, _num(row.get("solar_kw"))),
            home_kw=max(0.0, _num(row.get("home_kw"))),
            grid_kw=_num(row.get("grid_kw")),
            charging=charging,
            ev_power_kw=state.charger_power_kw(_row_amperage(row)) if charging else 0.0,
            session_minutes=_num(row.get("session_active_minutes")),
            tou_period=period,
            row=row,
        )


# ── Writing ─────────────────────────────────────────────────────────────────

def log_to_csv(stats: dict, action: str, reason: str, now: datetime):
    """Appends one telemetry row to the local CSV and queues it for the cloud."""
    global _log_rows_cache
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

    _log_rows_cache = None


# ── Reading ─────────────────────────────────────────────────────────────────

_log_rows_cache: list[dict] | None = None
_log_rows_cache_time = 0.0
LOG_CACHE_TTL = 60.0


def get_all_log_rows(days: int = 7, force_refresh: bool = False) -> list[dict]:
    """Returns recent telemetry rows from Google Sheets, falling back to CSV.

    Sheets is the source of truth because it survives container restarts; a
    60-second in-memory cache keeps repeated analytics off the network.
    """
    global _log_rows_cache, _log_rows_cache_time
    if not force_refresh and _log_rows_cache is not None and (time.time() - _log_rows_cache_time) < LOG_CACHE_TTL:
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

    _log_rows_cache, _log_rows_cache_time = rows, time.time()
    return rows


def get_recent_sessions(limit: int = 5) -> list[dict]:
    """Groups contiguous charging rows into distinct sessions, newest first."""
    sessions, current = [], None

    def stamp(row):
        return row.get("timestamp") or f"{row.get('date', '')} {row.get('time', '')}".strip()

    for r in _readings(get_all_log_rows()):
        row = r.row
        if r.charging:
            if current is None:
                current = {
                    "start_time": stamp(row),
                    "start_battery_pct": row.get("battery_pct", "N/A"),
                    "solar_kw": row.get("solar_kw", "0"),
                    "max_duration_minutes": r.session_minutes,
                    "stop_reason": row.get("session_stop_reason") or row.get("reason", ""),
                }
            current["end_time"] = stamp(row)
            current["end_battery_pct"] = row.get("battery_pct", "N/A")
            current["max_duration_minutes"] = max(current["max_duration_minutes"], r.session_minutes)
            if row.get("session_stop_reason"):
                current["stop_reason"] = row["session_stop_reason"]
        elif current is not None:
            current["max_duration_minutes"] = max(current["max_duration_minutes"], r.session_minutes)
            current["stop_reason"] = row.get("session_stop_reason") or row.get("reason") or current["stop_reason"]
            sessions.append(current)
            current = None

    if current is not None:
        sessions.append(current)
    return sessions[-limit:][::-1]


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
    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    try:
        sessions, current = [], None
        grid_kwh = grid_cost = solar_kwh = 0.0
        intervals = 0

        for r in _readings(rows, start, end):
            if r.charging:
                intervals += 1
                if current is None:
                    current = {"minutes": r.session_minutes, "power_kw": r.ev_power_kw}
                else:
                    current["minutes"] = max(current["minutes"], r.session_minutes)
                    current["power_kw"] = max(current["power_kw"], r.ev_power_kw)
                grid_kwh += r.ev_grid_kw * r.interval_h
                grid_cost += r.ev_grid_kw * r.interval_h * r.rate
                solar_kwh += r.ev_solar_kw * r.interval_h
            elif current is not None:
                current["minutes"] = max(current["minutes"], r.session_minutes)
                sessions.append(current)
                current = None
        if current is not None:
            sessions.append(current)

        default_power = state.charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE)
        total_minutes = round(sum(s["minutes"] for s in sessions), 1)
        if not total_minutes and intervals:
            total_minutes = round(intervals * config.CHECK_INTERVAL_MINUTES, 1)

        total_kwh = round(sum(s["minutes"] / 60.0 * s["power_kw"] for s in sessions), 2)
        if not total_kwh and total_minutes:
            total_kwh = round(total_minutes / 60.0 * default_power, 2)

        from_grid = round(min(grid_kwh, total_kwh), 2)
        self_powered = round(max(0.0, total_kwh - from_grid), 2)
        from_solar = round(min(solar_kwh, self_powered), 2)
        from_battery = round(max(0.0, self_powered - from_solar), 2)

        return {
            "period": label,
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
            "calculation_note": "Includes direct solar, Tesla Powerwall battery reserves, and grid energy delivered to the vehicle.",
        }
    except Exception as e:
        log_csv.error(f"Error calculating charging cost summary: {e}", exc_info=True)
        return {"error": f"Failed to calculate charging cost: {e}"}


def _fixed_fee(days: int) -> float:
    return config.UTILITY_FIXED_MONTHLY_FEE / 30.0 * days * config.UTILITY_TAX_MULTIPLIER


def _export_credit_rate() -> float:
    return config.UTILITY_SOLAR_EXPORT_CREDIT_RATE * config.UTILITY_TAX_MULTIPLIER


def _self_powered_pct(home_kwh: float, grid_kwh: float) -> float:
    if home_kwh <= 0:
        return 100.0
    return round(max(0.0, min(100.0, (1 - grid_kwh / home_kwh) * 100.0)), 1)


def get_home_energy_summary(period: str = "today") -> dict:
    """Whole-home consumption, generation, and utility bill breakdown."""
    start, end, label = _resolve_date_range(period)
    days = (end - start).days + 1
    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    try:
        home = solar = imported = exported = 0.0
        grid_cost = export_credit = 0.0
        ev_kwh = ev_grid_kwh = ev_solar_kwh = ev_cost = 0.0

        for r in _readings(rows, start, end):
            h = r.interval_h
            home += r.home_kw * h
            solar += r.solar_kw * h
            if r.charging:
                ev_kwh += r.ev_power_kw * h
                ev_grid_kwh += r.ev_grid_kw * h
                ev_cost += r.ev_grid_kw * h * r.rate
                ev_solar_kwh += r.ev_solar_kw * h
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


EVENING_START_HOUR, EVENING_END_HOUR = 16, 22


def get_energy_saving_advice() -> dict:
    """Derives solar windows, evening battery needs, and actionable bill tips."""
    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    now = datetime.now(config.TZ)
    since = (now - timedelta(days=7)).date()

    try:
        hourly_surplus: dict[int, list[float]] = {h: [] for h in range(24)}
        on_peak_cost = ev_grid_cost = 0.0
        evening: dict[date, list[float]] = {}

        for r in _readings(rows, start=since):
            h = r.interval_h
            hourly_surplus[r.hour].append(max(0.0, r.solar_kw - r.home_kw))

            if r.tou_period == "on_peak":
                on_peak_cost += r.grid_import_kw * h * r.rate
            ev_grid_cost += r.ev_grid_kw * h * r.rate

            if EVENING_START_HOUR <= r.hour < EVENING_END_HOUR:
                # Exclude the charger so evening planning reflects the house alone.
                appliance_kw = max(0.0, r.home_kw - r.ev_power_kw)
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

        best_hours = [h for h, vals in hourly_surplus.items() if vals and sum(vals) / len(vals) >= 1.0]
        window = f"{min(best_hours)}:00 - {max(best_hours) + 1}:00" if best_hours else "10:00 - 15:00"

        peak_rate = get_tou_rate(now.replace(hour=18, minute=0))
        export_rate = _export_credit_rate()
        avoid_window = f"{17}:00 - {20}:00 (On-Peak ${peak_rate:.2f}/kWh rate)"

        return {
            "optimal_solar_appliance_window": window,
            "cheapest_ev_charging_window": f"{window} (Off-Peak solar surplus)",
            "hours_to_avoid_heavy_loads": avoid_window,
            "avg_evening_appliance_load_kwh": round(appliance_kwh, 2),
            "avg_evening_solar_generation_kwh": round(evening_solar_kwh, 2),
            "avg_evening_net_battery_deficit_kwh": round(deficit_kwh, 2),
            "recommended_evening_battery_reserve_pct": reserve_pct,
            "on_peak_grid_cost_last_7_days": round(on_peak_cost, 2),
            "ev_grid_charging_cost_last_7_days": round(ev_grid_cost, 2),
            "actionable_recommendations": [
                f"Run heavy appliances (AC, washer, dishwasher, dryer) during {window} when solar generation peaks.",
                f"Avoid heavy appliances between 17:00 and 20:00, when the on-peak rate is ${peak_rate:.2f}/kWh.",
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
              "variable_grid_cost", "solar_export_credit", "ev_grid_kwh", "ev_grid_cost")

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

    for r in _readings(rows, start, end):
        entry = daily.get(r.day)
        if entry is None:
            continue
        h = r.interval_h
        entry["readings_count"] += 1
        entry["home_kwh"] += r.home_kw * h
        entry["solar_kwh"] += r.solar_kw * h
        if r.grid_kw > 0:
            entry["grid_import_kwh"] += r.grid_import_kw * h
            entry["variable_grid_cost"] += r.grid_import_kw * h * r.rate
            entry["ev_grid_kwh"] += r.ev_grid_kw * h
            entry["ev_grid_cost"] += r.ev_grid_kw * h * r.rate
        else:
            entry["solar_export_kwh"] += r.grid_export_kw * h
            entry["solar_export_credit"] += r.grid_export_kw * h * _export_credit_rate()

    expected = int(24 * 60 / max(1, config.CHECK_INTERVAL_MINUTES))
    records, totals = [], dict.fromkeys(fields, 0.0)

    for day in sorted(daily):
        entry = daily[day]
        count = entry["readings_count"]
        # Scale up days that missed a few intervals (restarts, updates) so a
        # short gap does not read as genuinely lower consumption.
        if 0 < count < expected and expected - count <= 12:
            scale = expected / count
            for f in fields:
                entry[f] *= scale
        for f in fields:
            totals[f] += entry[f]
            entry[f] = round(entry[f], 2)
        entry["net_variable_cost"] = round(max(0.0, entry["variable_grid_cost"] - entry["solar_export_credit"]), 2)
        records.append(entry)

    days_count = (end - start).days + 1
    fixed = _fixed_fee(days_count)

    return {
        "month_label": start.strftime("%B %Y"),
        "start_date": str(start),
        "end_date": str(end),
        "days_count": days_count,
        "total_home_kwh": round(totals["home_kwh"], 2),
        "total_solar_kwh": round(totals["solar_kwh"], 2),
        "total_grid_import_kwh": round(totals["grid_import_kwh"], 2),
        "total_solar_export_kwh": round(totals["solar_export_kwh"], 2),
        "total_variable_grid_cost_dollars": round(totals["variable_grid_cost"], 2),
        "total_solar_export_credit_dollars": round(totals["solar_export_credit"], 2),
        "fixed_service_fee_dollars": round(fixed, 2),
        "estimated_net_bill_dollars": round(
            max(0.0, fixed + totals["variable_grid_cost"] - totals["solar_export_credit"]), 2),
        "ev_charging_kwh": round(totals["ev_grid_kwh"], 2),
        "ev_charging_cost_dollars": round(totals["ev_grid_cost"], 2),
        "home_appliances_cost_dollars": round(max(0.0, totals["variable_grid_cost"] - totals["ev_grid_cost"]), 2),
        "self_powered_percentage": _self_powered_pct(totals["home_kwh"], totals["grid_import_kwh"]),
        "utility_rate_plan": provider_label(),
        "daily_records": records,
    }
