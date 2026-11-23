"""Dashboard payload assembled from data the control loop has already fetched.

Nothing here calls NetZero or ChargePoint. The cycle hands over its readings via
`record_live()`, and the day's totals are derived from the telemetry log and
cached until the next cycle writes a row, so a page refresh costs no network
traffic at all (and at most one Sheets read per cycle when someone is looking).
"""
import calendar
import threading
from datetime import datetime, timedelta

from core import config, state
from core import goal as charge_goal
from core.tou import calculate_mid_bill_components, get_tou_period, get_tou_rate, is_in_night_blackout, provider_label
from reporting import csv_logger

_TOU_KEYS = ("on_peak", "partial_peak", "off_peak")
_TOU_NAMES = {"on_peak": "On-peak", "partial_peak": "Partial-peak", "off_peak": "Off-peak"}
_SESSIONS_DAYS = 30
_SESSIONS_SHOWN = 12

_lock = threading.Lock()
_live: dict | None = None
_summary: dict | None = None
_summary_key: tuple | None = None


def record_live(stats: dict, cp_status: dict, now: datetime):
    """Stores the cycle's latest Powerwall and charger readings."""
    global _live
    with _lock:
        _live = {"stats": dict(stats), "charger": dict(cp_status or {}), "at": now}


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _live_or_last_row() -> tuple[dict, dict, datetime | None]:
    """Live readings, or the newest logged row right after a restart."""
    with _lock:
        if _live is not None:
            return _live["stats"], _live["charger"], _live["at"]
    rows = csv_logger.get_all_log_rows()
    if not rows:
        return {}, {}, None
    row = rows[-1]
    stats = {key: _num(row.get(key)) for key in
             ("battery_pct", "solar_kw", "home_kw", "grid_kw", "battery_kw", "self_powered_pct")}
    stats["island_mode"] = row.get("island_mode")
    stats["storm_mode"] = str(row.get("storm_mode")).lower() == "true"
    try:
        at = datetime.fromisoformat(row["timestamp"])
    except (KeyError, TypeError, ValueError):
        at = None
    return stats, {}, at


def _goal_text() -> str | None:
    """The active miles goal as "40 of 100 miles added; ...", or None."""
    try:
        goal = charge_goal.get_goal()
        return charge_goal.describe(goal, charge_goal.progress(goal)) if goal else None
    except Exception:
        return None


def _bill(tou_kwh: dict, export_kwh: float, month: int, days: int) -> dict:
    return calculate_mid_bill_components(
        on_peak_kwh=tou_kwh["on_peak"], partial_peak_kwh=tou_kwh["partial_peak"],
        off_peak_kwh=tou_kwh["off_peak"], export_kwh=export_kwh, month=month, days=days)


def _month(now: datetime) -> dict:
    """Month-to-date home energy, car energy and bill, day by day (Gateway-corrected grid import)."""
    today = now.date()
    start = today.replace(day=1)
    metered = csv_logger.metered_daily_totals(start, today)
    days = {start + timedelta(days=i): dict.fromkeys(
        ("solar_kwh", "home_kwh", "grid_kwh", "export_kwh", "cost", "car_kwh", "gateway_added_kwh"), 0.0) for i in range(today.day)}
    tou_kwh = dict.fromkeys(_TOU_KEYS, 0.0)
    for day, m in metered.items():
        d = days[day]
        d["solar_kwh"], d["home_kwh"], d["grid_kwh"] = m["solar_kwh"], m["home_kwh"], m["grid_import_kwh"]
        d["export_kwh"], d["cost"] = m["solar_export_kwh"], m["variable_grid_cost"]
        d["gateway_added_kwh"] = m["grid_import_kwh"] - m["tesla_grid_import_kwh"]
        for key in _TOU_KEYS:
            tou_kwh[key] += m[f"{key}_grid_kwh"]

    car = dict.fromkeys(("kwh", "solar_kwh", "battery_kwh", "grid_kwh", "cost"), 0.0)
    for iv in csv_logger._ev_intervals_between(start, today):
        r = iv.reading
        if iv.session < 0 or r.day < start:
            continue
        d = days[r.day]
        d["car_kwh"] += iv.kwh
        for key, value in (("kwh", iv.kwh), ("solar_kwh", iv.solar_kwh), ("battery_kwh", iv.battery_kwh),
                           ("grid_kwh", iv.grid_kwh), ("cost", iv.grid_cost)):
            car[key] += value

    home = sum(d["home_kwh"] for d in days.values())
    grid = sum(d["grid_kwh"] for d in days.values())
    export = sum(d["export_kwh"] for d in days.values())
    bill = None
    if metered:
        so_far = _bill(tou_kwh, export, today.month, today.day)
        month_days = calendar.monthrange(today.year, today.month)[1]
        elapsed = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400
        scale = month_days / max(elapsed, 1 / 24)
        projected = _bill({k: v * scale for k, v in tou_kwh.items()}, export * scale, today.month, month_days)
        bill = {
            "to_date": so_far["net_bill"],
            "projected": projected["net_bill"],
            "days_in_month": month_days,
            "lines": [{"label": _TOU_NAMES[k], "detail": f"{tou_kwh[k]:.0f} kWh", "amount": so_far[f"{k}_cost"]}
                      for k in _TOU_KEYS] + [
                {"label": "Service charge", "detail": "", "amount": so_far["fixed_fee"]},
                {"label": "Taxes & surcharges", "detail": "",
                 "amount": round(so_far["volumetric_adjustments"] + so_far["local_surcharge"], 2)},
                {"label": "Solar export credit", "detail": f"{export:.0f} kWh", "amount": -so_far["export_credit"]},
            ],
        }
    miles = car["kwh"] * config.EV_MILES_PER_KWH
    return {
        "label": start.strftime("%B %Y"),
        "has_home_data": bool(metered),
        "bill": bill,
        "solar_kwh": round(sum(d["solar_kwh"] for d in days.values()), 1),
        "home_kwh": round(home, 1),
        "grid_kwh": round(grid, 1),
        "export_kwh": round(export, 1),
        "gateway_added_kwh": round(sum(d["gateway_added_kwh"] for d in days.values()), 1),
        "self_powered_pct": round(max(0.0, 1 - grid / home) * 100) if home else None,
        "car": {**{k: round(v, 2) for k, v in car.items()},
                "miles": round(miles), "gas_cost": round(miles / config.GAS_CAR_MPG * config.GAS_PRICE_PER_GALLON, 2)},
        "days": [{"date": day.isoformat(), **{k: round(v, 2) for k, v in d.items()}} for day, d in days.items()],
    }


def _build_summary(now: datetime) -> dict:
    """The day's and month's totals and history; the only part that reads the logs."""
    ev = csv_logger.get_daily_charging_cost("today")
    sessions = csv_logger.sessions_between(now.date() - timedelta(days=_SESSIONS_DAYS), now.date())
    month = _month(now)
    today = month["days"][-1]
    return {
        "energy": None if not month["has_home_data"] else {
            "solar_kwh": today["solar_kwh"],
            "home_kwh": today["home_kwh"],
            "grid_import_kwh": today["grid_kwh"],
            "grid_export_kwh": today["export_kwh"],
            "self_powered_pct": round(max(0.0, 1 - today["grid_kwh"] / today["home_kwh"]) * 100)
            if today["home_kwh"] else None,
            "energy_cost": today["cost"],
        },
        "ev_today": None if "error" in ev else {
            "sessions": ev["total_sessions_count"],
            "minutes": ev["total_charging_minutes"],
            "energy_kwh": ev["total_kwh_added"],
            "miles": ev["estimated_miles_added"],
            "solar_kwh": ev["solar_kwh_used"],
            "battery_kwh": ev["powerwall_battery_kwh_used"],
            "grid_kwh": ev["ev_grid_kwh_pulled"],
            "grid_cost": ev["ev_grid_cost_dollars"],
        },
        "sessions": sessions[::-1][:_SESSIONS_SHOWN],
        "month": month,
        "hourly": csv_logger.get_hourly_profile("today"),
        "goal": _goal_text(),
    }


def _summary_cached(today) -> dict:
    """Rebuilds the summary only after a new telemetry row or a date change."""
    global _summary, _summary_key
    key = (csv_logger.rows_version(), today)
    with _lock:
        if _summary is not None and _summary_key == key:
            return _summary
    summary = _build_summary(datetime.now(config.TZ))
    with _lock:
        _summary, _summary_key = summary, key
    return summary


def _live_power(stats: dict, car_kw: float) -> dict:
    """Live readings with grid import (and so home load) corrected like the bill; see metered_daily_totals."""
    solar, battery, grid, home = (_num(stats.get(k)) for k in ("solar_kw", "battery_kw", "grid_kw", "home_kw"))
    power = {"battery_pct": stats.get("battery_pct"), "battery_kw": battery, "solar_kw": solar,
             "home_kw": home, "grid_kw": grid, "self_powered_pct": stats.get("self_powered_pct")}
    if None in (solar, battery, grid, home):
        return power
    car_grid = max(0.0, car_kw - solar - max(0.0, battery))
    imported = csv_logger.gateway_corrected_import(max(0.0, grid), car_grid)
    if imported > 0:
        power["grid_kw"] = round(imported, 2)
        power["home_kw"] = round(home + imported - max(0.0, grid), 2)
        power["self_powered_pct"] = round(max(0.0, 1 - imported / power["home_kw"]) * 100)
    return power


def get_dashboard_data() -> dict:
    now = datetime.now(config.TZ)
    stats, cp, updated_at = _live_or_last_row()
    summary = _summary_cached(now.date())
    snap = state.snapshot()
    cfg = config.snapshot()

    charging = cp.get("charging_status") == "CHARGING" if cp else snap.charger_state == state.State.CHARGING
    session = None
    if charging:
        start = cp.get("session_start_time") or snap.charge_session_start
        energy = _num(cp.get("energy_kwh"), 0.0)
        session = {
            "start_time": start.isoformat() if start else None,
            "minutes": state.get_session_minutes(),
            "energy_kwh": round(energy, 2),
            "miles": round(_num(cp.get("miles_added"), 0.0) or energy * cfg.EV_MILES_PER_KWH, 1),
            "power_kw": _num(cp.get("power_kw"), 0.0),
            "amperage": cp.get("amperage_limit") or snap.active_amperage,
            # Source split and cost so far, from the log (up to one interval behind).
            "logged": summary["sessions"][0] if summary["sessions"] else None,
        }

    interval = cfg.CHECK_INTERVAL_MINUTES
    return {
        "generated_at": now.isoformat(),
        "updated_at": updated_at.isoformat() if updated_at else None,
        "next_update_at": (updated_at + timedelta(minutes=interval)).isoformat() if updated_at else None,
        "interval_minutes": interval,
        "status": {
            "mode": "manual" if snap.manual_mode or cfg.MANUAL_MODE_OVERRIDE == "manual" else "auto",
            "charger_state": snap.charger_state,
            "tou_period": get_tou_period(now),
            "tou_rate": get_tou_rate(now),
            "in_blackout": is_in_night_blackout(now),
            "island_mode": stats.get("island_mode"),
            "storm_mode": bool(stats.get("storm_mode")),
            "api_failures": snap.consecutive_api_failures,
            "grid_draw_events": snap.grid_draw_count,
            "rate_plan": provider_label(),
        },
        "power": _live_power(stats, session["power_kw"] if session else 0.0),
        "powerwall": {
            "start_pct": cfg.BATTERY_START_PCT,
            "stop_pct": cfg.BATTERY_STOP_PCT,
            "reserve_pct": cfg.BATTERY_LOW_RESERVE_PCT,
            "usable_kwh": config.POWERWALL_USABLE_KWH,
        },
        "ev": {
            "charging": charging,
            "plugged_in": cp.get("is_plugged_in") if cp else None,
            "connected": cp.get("is_connected") if cp else None,
            "session": session,
            "today": summary["ev_today"],
            "last_session": summary["sessions"][0] if summary["sessions"] else None,
            "sessions": summary["sessions"],
            "last_stop_reason": snap.session_stop_reason,
            "goal": summary["goal"],
        },
        "energy_today": summary["energy"],
        "month": summary["month"],
        "hourly": summary["hourly"],
        "tou_today": [{"hour": h, "period": get_tou_period(now.replace(hour=h, minute=0)),
                       "rate": get_tou_rate(now.replace(hour=h, minute=0))} for h in range(24)],
        "settings": {
            "charge_window": [cfg.ALLOWED_CHARGE_START_HOUR, cfg.ALLOWED_CHARGE_END_HOUR],
            "blackout": [cfg.NIGHT_BLACKOUT_START_HOUR, cfg.NIGHT_BLACKOUT_END_HOUR],
            "default_amperage": config.DEFAULT_CHARGER_AMPERAGE,
            "miles_per_kwh": cfg.EV_MILES_PER_KWH,
            "gas_mpg": config.GAS_CAR_MPG,
            "gas_price": config.GAS_PRICE_PER_GALLON,
            "gateway_read_pct": config.GATEWAY_GRID_READ_PCT,
        },
    }
