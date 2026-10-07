"""Dashboard payload assembled from data the control loop has already fetched.

Nothing here calls NetZero or ChargePoint. The cycle hands over its readings via
`record_live()`, and the day's totals are derived from the telemetry log and
cached until the next cycle writes a row, so a page refresh costs no network
traffic at all (and at most one Sheets read per cycle when someone is looking).
"""
import threading
from datetime import datetime, timedelta

from core import config, state
from core.tou import get_tou_period, get_tou_rate, is_in_night_blackout, provider_label
from reporting import csv_logger

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


def _cost_today(bill: dict) -> dict:
    """Today's itemised utility bill, reduced to the energy charge and its two companions."""
    if "error" in bill:
        return {"energy_cost": None, "fixed_charges": None, "solar_credit": None}
    subtotals = {group["title"]: group["subtotal"] for group in bill["groups"]}
    return {
        "energy_cost": subtotals["Energy used"],
        "fixed_charges": subtotals["Fixed charges & taxes"],
        "solar_credit": -subtotals["Credits"],
    }


def _build_summary() -> dict:
    """The day's totals and history; the only part that reads the telemetry log."""
    energy = csv_logger.get_home_energy_summary("today")
    ev = csv_logger.get_daily_charging_cost("today")
    last = csv_logger.get_recent_sessions(limit=1)
    bill = csv_logger.get_bill_breakdown("today")
    return {
        "energy": None if "error" in energy else {
            "solar_kwh": energy["total_solar_generated_kwh"],
            "home_kwh": energy["total_home_consumption_kwh"],
            "grid_import_kwh": energy["total_grid_imported_kwh"],
            "grid_export_kwh": energy["total_solar_exported_kwh"],
            "self_powered_pct": energy["home_self_powered_percentage"],
            **_cost_today(bill),
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
        "last_session": last[0] if last else None,
        "hourly": csv_logger.get_hourly_profile("today"),
    }


def _summary_cached(today) -> dict:
    """Rebuilds the summary only after a new telemetry row or a date change."""
    global _summary, _summary_key
    key = (csv_logger.rows_version(), today)
    with _lock:
        if _summary is not None and _summary_key == key:
            return _summary
    summary = _build_summary()
    with _lock:
        _summary, _summary_key = summary, key
    return summary


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
        "power": {
            "battery_pct": stats.get("battery_pct"),
            "battery_kw": stats.get("battery_kw"),
            "solar_kw": stats.get("solar_kw"),
            "home_kw": stats.get("home_kw"),
            "grid_kw": stats.get("grid_kw"),
            "self_powered_pct": stats.get("self_powered_pct"),
        },
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
            "last_session": summary["last_session"],
            "last_stop_reason": snap.session_stop_reason,
        },
        "energy_today": summary["energy"],
        "hourly": summary["hourly"],
        "settings": {
            "charge_window": [cfg.ALLOWED_CHARGE_START_HOUR, cfg.ALLOWED_CHARGE_END_HOUR],
            "blackout": [cfg.NIGHT_BLACKOUT_START_HOUR, cfg.NIGHT_BLACKOUT_END_HOUR],
            "default_amperage": config.DEFAULT_CHARGER_AMPERAGE,
            "miles_per_kwh": cfg.EV_MILES_PER_KWH,
        },
    }
