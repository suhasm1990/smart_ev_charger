import csv
import os
import requests
from datetime import datetime, timedelta, date

from core import state, config
from core.tou import get_tou_period, get_tou_rate, is_expensive_period, is_in_night_blackout, is_weekend
from reporting.logger import log_csv

CSV_HEADERS = [
    "timestamp", "date", "time", "day_of_week", "is_weekend",
    "tou_period", "tou_rate_per_kwh", "is_expensive",
    "solar_kw", "home_kw", "solar_surplus_kw", "battery_kw", "grid_kw", "battery_pct", "self_powered_pct",
    "threshold_battery_start", "threshold_battery_stop", "threshold_solar_start", "threshold_solar_stop",
    "charger_state", "action", "reason",
    "session_active_minutes", "session_count_today", "session_stop_reason",
    "is_night_blackout", "manual_mode", "island_mode", "storm_mode",
    "est_grid_cost_this_minute", "charge_window_start_hour", "charge_window_end_hour"
]

def get_session_minutes() -> float:
    if state.charge_session_start is None:
        return 0.0
    return max(0.0, round((datetime.now(config.TZ) - state.charge_session_start).total_seconds() / 60, 1))

def log_to_csv(stats: dict, action: str, reason: str, now: datetime):
    tou    = get_tou_period(now)
    rate   = get_tou_rate(now)
    grid   = float(stats.get("grid_kw", 0.0) or 0.0)
    interval_h = config.CHECK_INTERVAL_MINUTES / 60.0
    est_cost = round(max(0.0, grid) * rate * interval_h, 4)

    # Only record active session minutes if charging or at the moment of stop
    session_mins = get_session_minutes() if (state.charger_state == state.State.CHARGING or action == "stop") else 0.0

    row = [
        now.isoformat(),
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        now.strftime("%A"),
        is_weekend(now),
        tou,
        rate,
        is_expensive_period(now),
        stats.get("solar_kw", 0.0),
        stats.get("home_kw", 0.0),
        stats.get("solar_surplus_kw", 0.0),
        stats.get("battery_kw", 0.0),
        stats.get("grid_kw", 0.0),
        stats.get("battery_pct", 0.0),
        stats.get("self_powered_pct", 100.0),
        config.BATTERY_START_PCT,
        config.BATTERY_STOP_PCT,
        0.0,
        0.0,
        state.charger_state,
        action,
        reason,
        session_mins,
        state.session_count_today,
        state.session_stop_reason or "",
        is_in_night_blackout(now),
        state.manual_mode,
        stats.get("island_mode", "on_grid"),
        stats.get("storm_mode", False),
        est_cost,
        config.ALLOWED_CHARGE_START_HOUR,
        config.ALLOWED_CHARGE_END_HOUR,
    ]

    file_exists = os.path.exists(config.CSV_LOG_FILE)
    os.makedirs(os.path.dirname(config.CSV_LOG_FILE) or ".", exist_ok=True)
    with open(config.CSV_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADERS)
        writer.writerow(row)

    try:
        from services.sheets_db import append_log_row
        append_log_row(row)
    except Exception as e:
        log_csv.error(f"Failed to push row to Google Sheets: {e}")

    if est_cost > 0:
        log_csv.debug(
            f"Grid draw logged | grid={grid}kW | rate=${rate}/kWh | "
            f"est_cost_this_min=${est_cost:.5f} | tou={tou}"
        )

def get_all_log_rows(days: int = 7) -> list[dict]:
    """
    Fetches recent log rows directly from Google Sheets (primary source of truth across container restarts).
    Falls back to local CSV file if Google Sheets API is unconfigured or temporarily unavailable.
    """
    # 1. Try Google Sheets first (primary single source of truth)
    try:
        from services.sheets_db import get_recent_logs
        sheets_logs = get_recent_logs(days=days)
        if sheets_logs:
            return sheets_logs
    except Exception as e:
        log_csv.warning(f"Failed to fetch logs from Google Sheets, falling back to local CSV: {e}")

    # 2. Fallback to local CSV if Sheets API fails or is offline
    rows = []
    csv_file = config.CSV_LOG_FILE
    if os.path.exists(csv_file):
        try:
            with open(csv_file, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            log_csv.warning(f"Error reading local CSV fallback: {e}")

    return rows

def get_recent_sessions(limit: int = 5) -> list[dict]:
    """Parses logs (local CSV + Google Sheets) and groups contiguous charging rows into distinct charging sessions."""
    rows = get_all_log_rows()
    if not rows:
        return []

    sessions = []
    current_session = None

    try:
        for row in rows:
            state_str = row.get("charger_state", "")
            action_str = row.get("action", "")
            is_charging = ("CHARGING" in state_str) or (action_str == "start")

            if is_charging:
                if current_session is None:
                    current_session = {
                        "start_time": row.get("timestamp", f"{row.get('date', '')} {row.get('time', '')}"),
                        "start_battery_pct": row.get("battery_pct", "N/A"),
                        "end_time": row.get("timestamp", f"{row.get('date', '')} {row.get('time', '')}"),
                        "end_battery_pct": row.get("battery_pct", "N/A"),
                        "max_duration_minutes": float(row.get("session_active_minutes", 0) or 0),
                        "stop_reason": row.get("session_stop_reason", "") or row.get("reason", ""),
                        "solar_kw": row.get("solar_kw", "0")
                    }
                else:
                    current_session["end_time"] = row.get("timestamp", f"{row.get('date', '')} {row.get('time', '')}")
                    current_session["end_battery_pct"] = row.get("battery_pct", "N/A")
                    dur = float(row.get("session_active_minutes", 0) or 0)
                    if dur > current_session["max_duration_minutes"]:
                        current_session["max_duration_minutes"] = dur
                    if row.get("session_stop_reason"):
                        current_session["stop_reason"] = row.get("session_stop_reason")
            else:
                if current_session is not None:
                    # Session just ended
                    dur = float(row.get("session_active_minutes", 0) or 0)
                    if dur > current_session["max_duration_minutes"]:
                        current_session["max_duration_minutes"] = dur
                    if action_str == "stop" or row.get("session_stop_reason") or row.get("reason"):
                        current_session["stop_reason"] = row.get("session_stop_reason") or row.get("reason") or current_session["stop_reason"]
                    sessions.append(current_session)
                    current_session = None

        if current_session is not None:
            sessions.append(current_session)

    except Exception as e:
        log_csv.error(f"Error parsing recent sessions: {e}")

    # Return most recent sessions first
    return sessions[-limit:][::-1]

def _is_ev_charging_row(row: dict) -> bool:
    """Returns True if the log row represents active EV charging."""
    state_str = str(row.get("charger_state", "")).upper()
    action_str = str(row.get("action", "")).lower()
    return ("CHARGING" in state_str) or (action_str in ["start", "stop"])

def _resolve_date_range(period: str, now: datetime = None) -> tuple[datetime.date, datetime.date, str]:
    """Resolves period string into (start_date, end_date, period_label)."""
    if now is None:
        now = datetime.now(config.TZ)
    
    clean = str(period or "today").lower().strip()
    if clean in ["today", ""]:
        return now.date(), now.date(), f"Today ({now.date()})"
    if clean == "yesterday":
        yest = now.date() - timedelta(days=1)
        return yest, yest, f"Yesterday ({yest})"
    if clean in ["this_week", "week", "this week"]:
        start = now.date() - timedelta(days=now.weekday())
        return start, now.date(), f"This Week ({start} to {now.date()})"
    if clean in ["last_week", "last week", "previous_week", "previous week"]:
        this_week_start = now.date() - timedelta(days=now.weekday())
        start = this_week_start - timedelta(days=7)
        end = this_week_start - timedelta(days=1)
        return start, end, f"Last Week ({start} to {end})"
    if clean in ["7days", "7_days", "last_7_days", "last 7 days", "past_7_days", "past 7 days", "past week"]:
        start = now.date() - timedelta(days=7)
        return start, now.date(), f"Past 7 Days ({start} to {now.date()})"
    if clean in ["this_month", "month", "30days", "30 days", "this month"]:
        start = now.date().replace(day=1)
        return start, now.date(), f"This Month ({start.strftime('%B %Y')})"
    if clean in ["last_month", "previous_month", "last month", "previous month"]:
        first_this_month = now.date().replace(day=1)
        last_prev_month = first_this_month - timedelta(days=1)
        start = last_prev_month.replace(day=1)
        return start, last_prev_month, f"Last Month ({start.strftime('%B %Y')})"
    
    # Try parsing month strings (e.g. 'July', 'July 2026', '2026-07')
    month_formats = ["%Y-%m", "%B %Y", "%b %Y", "%B", "%b"]
    for fmt in month_formats:
        try:
            dt_cand = datetime.strptime(clean, fmt)
            y = dt_cand.year
            if fmt in ["%B", "%b"]:
                y = now.year
                if dt_cand.month > now.month:
                    y -= 1
            m_start = date(y, dt_cand.month, 1)
            if m_start.month == 12:
                m_end = date(m_start.year, 12, 31)
            else:
                m_end = date(m_start.year, m_start.month + 1, 1) - timedelta(days=1)
            return m_start, m_end, f"{m_start.strftime('%B %Y')}"
        except Exception:
            continue

    try:
        parsed = datetime.strptime(clean, "%Y-%m-%d").date()
        return parsed, parsed, f"Date ({parsed})"
    except Exception:
        return now.date(), now.date(), f"Today ({now.date()})"

def get_daily_charging_cost(period: str = "today") -> dict:
    """Calculates total grid energy drawn (kWh), solar energy used (kWh), and cost ($) for EV charging for a period."""
    now = datetime.now(config.TZ)
    start_date, end_date, period_label = _resolve_date_range(period, now)

    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    sessions = []
    curr_session = None
    grid_energy_by_interval = 0.0
    grid_cost_by_interval = 0.0
    solar_energy_by_interval = 0.0
    charging_intervals_count = 0
    
    interval_h = config.CHECK_INTERVAL_MINUTES / 60.0
    ev_power_kw = (config.DEFAULT_CHARGER_AMPERAGE * 240.0) / 1000.0  # 4.8 kW at 20A, 7.68 kW at 32A

    try:
        for row in rows:
            row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
            try:
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
            except Exception:
                continue

            if not (start_date <= row_date <= end_date):
                continue

            state_str = str(row.get("charger_state", "")).upper()
            action_str = str(row.get("action", "")).lower()
            is_chg = ("CHARGING" in state_str) or (action_str == "start")
            dur = float(row.get("session_active_minutes", 0) or 0)
            grid_kw = max(0.0, float(row.get("grid_kw", 0) or 0))
            solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
            home_kw = max(0.0, float(row.get("home_kw", 0) or 0))
            
            ts_str = row.get("timestamp")
            try:
                rate = get_tou_rate(datetime.fromisoformat(ts_str))
            except Exception:
                rate = float(row.get("tou_rate_per_kwh", 0) or 0.1706)

            if is_chg:
                charging_intervals_count += 1
                reason_combined = (str(row.get("reason", "")) + " " + str(row.get("session_stop_reason", ""))).lower()
                row_amp = 32 if ("32a" in reason_combined or "32 a" in reason_combined) else getattr(state, "active_amperage", config.DEFAULT_CHARGER_AMPERAGE)
                row_power_kw = (row_amp * 240.0) / 1000.0
                
                if curr_session is None:
                    curr_session = {"max_dur": dur, "amperage": row_amp, "power_kw": row_power_kw}
                else:
                    if dur > curr_session["max_dur"]:
                        curr_session["max_dur"] = dur
                    if row_amp > curr_session.get("amperage", 20):
                        curr_session["amperage"] = row_amp
                        curr_session["power_kw"] = row_power_kw
                    
                ev_grid_kw = min(grid_kw, row_power_kw)
                grid_energy_by_interval += ev_grid_kw * interval_h
                grid_cost_by_interval += ev_grid_kw * interval_h * rate
                solar_surplus_kw = max(0.0, solar_kw - max(0.0, home_kw - row_power_kw))
                solar_energy_by_interval += min(solar_surplus_kw, row_power_kw) * interval_h
            else:
                if curr_session is not None:
                    if dur > curr_session["max_dur"]:
                        curr_session["max_dur"] = dur
                    sessions.append(curr_session)
                    curr_session = None

        if curr_session is not None:
            sessions.append(curr_session)

        # Exact duration and energy calculation across all sessions in the period
        total_charging_mins = round(sum(s["max_dur"] for s in sessions), 1)
        if total_charging_mins == 0.0 and charging_intervals_count > 0:
            total_charging_mins = round(charging_intervals_count * config.CHECK_INTERVAL_MINUTES, 1)

        total_ev_kwh = round(sum((s["max_dur"] / 60.0) * s.get("power_kw", ev_power_kw) for s in sessions), 2)
        if total_ev_kwh == 0.0 and total_charging_mins > 0:
            total_ev_kwh = round((total_charging_mins / 60.0) * ev_power_kw, 2)
        total_grid_kwh = round(min(grid_energy_by_interval, total_ev_kwh), 2)
        total_self_powered_kwh = round(max(0.0, total_ev_kwh - total_grid_kwh), 2)
        total_solar_kwh = round(min(solar_energy_by_interval, total_self_powered_kwh), 2)
        total_battery_kwh = round(max(0.0, total_self_powered_kwh - total_solar_kwh), 2)
        total_grid_cost = round(grid_cost_by_interval, 2)

        grid_pct = round((total_grid_kwh / total_ev_kwh * 100.0), 1) if total_ev_kwh > 0 else 0.0
        self_powered_pct = round((total_self_powered_kwh / total_ev_kwh * 100.0), 1) if total_ev_kwh > 0 else 100.0
        current_rate = get_tou_rate(now)

        # Calculate driving range added using configured EV efficiency (miles / kWh)
        estimated_miles = round(total_ev_kwh * config.EV_MILES_PER_KWH, 1)

        return {
            "period": period_label,
            "total_charging_hours": round(total_charging_mins / 60.0, 1),
            "total_charging_minutes": total_charging_mins,
            "charging_intervals_count": charging_intervals_count,
            "ev_grid_kwh_pulled": total_grid_kwh,
            "solar_kwh_used": total_solar_kwh,
            "powerwall_battery_kwh_used": total_battery_kwh,
            "total_self_powered_kwh": total_self_powered_kwh,
            "total_kwh_added": total_ev_kwh,
            "estimated_miles_added": estimated_miles,
            "ev_grid_cost_dollars": total_grid_cost,
            "grid_percentage": grid_pct,
            "solar_percentage": self_powered_pct,
            "estimated_solar_savings_dollars": round(total_self_powered_kwh * current_rate, 2),
            "calculation_note": "Includes direct solar, Tesla Powerwall battery reserves, and grid energy delivered to vehicle."
        }
    except Exception as e:
        log_csv.error(f"Error calculating charging cost summary: {e}")
        return {"error": f"Failed to calculate charging cost: {e}"}

def get_home_energy_summary(period: str = "today") -> dict:
    """Calculates total home energy consumed (kWh), solar generated (kWh), grid imported (kWh), solar export credits ($), fixed fees ($), and utility bill breakdown."""
    now = datetime.now(config.TZ)
    start_date, end_date, period_label = _resolve_date_range(period, now)
    days_count = (end_date - start_date).days + 1

    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    total_home_kwh = 0.0
    total_solar_kwh = 0.0
    total_grid_import_kwh = 0.0
    total_solar_export_kwh = 0.0
    delivered_grid_cost = 0.0
    solar_export_credit = 0.0
    ev_grid_kwh = 0.0
    ev_solar_kwh = 0.0
    ev_total_kwh = 0.0
    ev_grid_cost = 0.0

    try:
        for row in rows:
            row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
            try:
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
            except Exception:
                continue

            if start_date <= row_date <= end_date:
                grid_kw = float(row.get("grid_kw", 0) or 0)
                solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
                home_kw = max(0.0, float(row.get("home_kw", 0) or 0))
                interval_h = config.CHECK_INTERVAL_MINUTES / 60.0

                total_home_kwh += home_kw * interval_h
                total_solar_kwh += solar_kw * interval_h
                
                ts_str = row.get("timestamp")
                try:
                    dt = datetime.fromisoformat(ts_str)
                    rate = get_tou_rate(dt)
                except Exception:
                    rate = float(row.get("tou_rate_per_kwh", 0) or 0.1706)

                if _is_ev_charging_row(row):
                    ev_power_kw = (config.DEFAULT_CHARGER_AMPERAGE * 240.0) / 1000.0
                    interval_ev_kwh = ev_power_kw * interval_h
                    ev_total_kwh += interval_ev_kwh

                    if grid_kw > 0:
                        ev_grid_kw_interval = min(grid_kw, ev_power_kw)
                        ev_kwh = ev_grid_kw_interval * interval_h
                        ev_grid_kwh += ev_kwh
                        ev_grid_cost += ev_kwh * rate
                    solar_surplus_kw = max(0.0, solar_kw - max(0.0, home_kw - ev_power_kw))
                    solar_kwh_interval = min(solar_surplus_kw, ev_power_kw) * interval_h
                    ev_solar_kwh += solar_kwh_interval

                if grid_kw > 0:
                    import_kwh = grid_kw * interval_h
                    total_grid_import_kwh += import_kwh
                    delivered_grid_cost += import_kwh * rate
                else:
                    export_kwh = abs(grid_kw) * interval_h
                    total_solar_export_kwh += export_kwh
                    solar_export_credit += export_kwh * (config.UTILITY_SOLAR_EXPORT_CREDIT_RATE * config.UTILITY_TAX_MULTIPLIER)

        fixed_service_fee = (config.UTILITY_FIXED_MONTHLY_FEE / 30.0) * days_count * config.UTILITY_TAX_MULTIPLIER
        estimated_bill_total = max(0.0, fixed_service_fee + delivered_grid_cost - solar_export_credit)
        non_ev_home_cost = max(0.0, delivered_grid_cost - ev_grid_cost)
        self_powered_pct = round(max(0.0, min(100.0, (1 - total_grid_import_kwh / max(total_home_kwh, 0.01)) * 100.0)), 1) if total_home_kwh > 0 else 100.0

        if ev_total_kwh > 0 and ev_grid_cost == 0.0:
            ev_summary_msg = f"{round(ev_total_kwh, 1)} kWh added (100% solar/battery self-powered, $0.00 grid cost)"
        elif ev_total_kwh > 0:
            ev_summary_msg = f"{round(ev_total_kwh, 1)} kWh added (${round(ev_grid_cost, 2):.2f} grid cost, {round(ev_solar_kwh, 1)} kWh direct solar)"
        else:
            ev_summary_msg = "No EV charging recorded in this period"

        provider_name = getattr(config, "UTILITY_PROVIDER", "MID")
        plan_label = f"Modesto Irrigation District (MID) Rate N2-EVD" if provider_name == "MID" else f"PG&E EV2-A Rate Schedule" if provider_name == "PGE" else f"Custom Utility Rate ({provider_name})"

        return {
            "period": period_label,
            "period_days_count": days_count,
            "total_home_consumption_kwh": round(total_home_kwh, 2),
            "total_solar_generated_kwh": round(total_solar_kwh, 2),
            "total_grid_imported_kwh": round(total_grid_import_kwh, 2),
            "total_solar_exported_kwh": round(total_solar_export_kwh, 2),
            "fixed_service_fee_dollars": round(fixed_service_fee, 2),
            "grid_delivered_energy_cost_dollars": round(delivered_grid_cost, 2),
            "solar_export_credit_dollars": round(solar_export_credit, 2),
            "estimated_total_mid_utility_bill_dollars": round(estimated_bill_total, 2),
            "ev_charging_share_of_bill_dollars": round(ev_grid_cost, 2),
            "ev_charging_total_kwh": round(ev_total_kwh, 2),
            "ev_estimated_miles_added": round(ev_total_kwh * config.EV_MILES_PER_KWH, 1),
            "ev_solar_kwh_used": round(ev_solar_kwh, 2),
            "ev_grid_kwh_used": round(ev_grid_kwh, 2),
            "ev_charging_summary": ev_summary_msg,
            "home_appliances_grid_energy_cost_dollars": round(non_ev_home_cost, 2),
            "home_appliances_share_of_bill_dollars": round(non_ev_home_cost, 2),
            "home_self_powered_percentage": self_powered_pct,
            "utility_rate_plan": plan_label
        }
    except Exception as e:
        log_csv.error(f"Error calculating home energy summary: {e}")
        return {"error": f"Failed to calculate home energy summary: {e}"}

def get_energy_saving_advice() -> dict:
    """Analyzes recent 7-day power usage logs to calculate solar generation windows, identify high-cost grid draws, and provide actionable tips to reduce electric bills."""
    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    now = datetime.now(config.TZ)
    seven_days_ago = (now - timedelta(days=7)).date()

    hourly_solar_surplus = {h: [] for h in range(24)}
    on_peak_grid_cost = 0.0
    ev_grid_draw_cost = 0.0

    try:
        for row in rows:
            row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
            try:
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
            except Exception:
                continue

            if row_date >= seven_days_ago:
                ts_str = row.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts_str)
                    hour = dt.hour
                    rate = get_tou_rate(dt)
                except Exception:
                    hour = int(row.get("time", "12:00").split(":")[0])
                    rate = 0.1706

                solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
                home_kw = max(0.0, float(row.get("home_kw", 0) or 0))
                grid_kw = float(row.get("grid_kw", 0) or 0)
                interval_h = config.CHECK_INTERVAL_MINUTES / 60.0

                surplus = max(0.0, solar_kw - home_kw)
                hourly_solar_surplus[hour].append(surplus)

                period = row.get("tou_period", "")
                if period == "on_peak" and grid_kw > 0:
                    on_peak_grid_cost += grid_kw * interval_h * rate

                state_str = row.get("charger_state", "")
                action_str = row.get("action", "")
                if ("CHARGING" in state_str or action_str == "start") and grid_kw > 0:
                    ev_grid_kw_val = min(grid_kw, 4.8)
                    ev_grid_draw_cost += ev_grid_kw_val * interval_h * rate

        # Calculate historical evening peak household load & evening solar (16:00 - 22:00 / 4 PM - 10 PM)
        evening_appliance_by_day = {}
        evening_solar_by_day = {}
        evening_net_deficit_by_day = {}
        for row in rows:
            row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
            try:
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
            except Exception:
                continue
            if row_date >= seven_days_ago:
                try:
                    hour = int(row.get("time", "12:00").split(":")[0])
                except Exception:
                    continue
                if 16 <= hour < 22:
                    home_kw = max(0.0, float(row.get("home_kw", 0) or 0))
                    solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
                    state_str = str(row.get("charger_state", "")).upper()
                    action_str = str(row.get("action", "")).lower()
                    if ("CHARGING" in state_str) or (action_str == "start"):
                        home_appliance_kw = max(0.0, home_kw - 4.8)
                    else:
                        home_appliance_kw = home_kw
                    
                    net_deficit_kw = max(0.0, home_appliance_kw - solar_kw)
                    evening_appliance_by_day[row_date] = evening_appliance_by_day.get(row_date, 0.0) + (home_appliance_kw * interval_h)
                    evening_solar_by_day[row_date] = evening_solar_by_day.get(row_date, 0.0) + (solar_kw * interval_h)
                    evening_net_deficit_by_day[row_date] = evening_net_deficit_by_day.get(row_date, 0.0) + (net_deficit_kw * interval_h)

        avg_evening_appliance_kwh = sum(evening_appliance_by_day.values()) / max(len(evening_appliance_by_day), 1) if evening_appliance_by_day else 5.0
        avg_evening_solar_kwh = sum(evening_solar_by_day.values()) / max(len(evening_solar_by_day), 1) if evening_solar_by_day else 2.0
        avg_evening_net_deficit_kwh = sum(evening_net_deficit_by_day.values()) / max(len(evening_net_deficit_by_day), 1) if evening_net_deficit_by_day else 3.5
        
        # Tesla Powerwall usable capacity is ~13.5 kWh. Add 10% safety buffer over net deficit.
        recommended_battery_reserve = min(60.0, max(25.0, (avg_evening_net_deficit_kwh / 13.5) * 100.0 + 10.0))
        rec_reserve_pct = round(recommended_battery_reserve, 1)

        avg_surplus = {h: (sum(vals)/len(vals) if vals else 0.0) for h, vals in hourly_solar_surplus.items()}
        best_hours = [h for h, avg in avg_surplus.items() if avg >= 1.0]

        solar_window_str = f"{min(best_hours)}:00 - {max(best_hours)+1}:00" if best_hours else "10:00 AM - 3:00 PM"

        rec1 = f"Run heavy appliances (AC, washing machine, dishwasher, dryer) during {solar_window_str} when solar generation is at its peak."
        rec2 = "Avoid running heavy appliances between 5:00 PM and 8:00 PM (On-Peak hours when MID grid rates are $0.35/kWh)."
        rec3 = "Consuming your solar power directly saves 4.5x more money than exporting it back to MID ($0.35/kWh avoided vs $0.076/kWh solar export credit)."
        rec4 = "Charge EV during daytime solar surplus hours (10:00 AM - 3:00 PM) to avoid pulling grid power at night."
        rec5 = f"Evening appliance load averages {round(avg_evening_appliance_kwh, 1)} kWh (offset by {round(avg_evening_solar_kwh, 1)} kWh late solar). Maintain at least {rec_reserve_pct}% Powerwall reserve at 4:00 PM for net {round(avg_evening_net_deficit_kwh, 1)} kWh battery deficit."

        return {
            "optimal_solar_appliance_window": solar_window_str,
            "cheapest_ev_charging_window": f"{solar_window_str} (Off-Peak solar surplus)",
            "hours_to_avoid_heavy_loads": "5:00 PM - 8:00 PM (On-Peak $0.35/kWh rate)",
            "avg_evening_appliance_load_kwh": round(avg_evening_appliance_kwh, 2),
            "avg_evening_solar_generation_kwh": round(avg_evening_solar_kwh, 2),
            "avg_evening_net_battery_deficit_kwh": round(avg_evening_net_deficit_kwh, 2),
            "recommended_evening_battery_reserve_pct": rec_reserve_pct,
            "on_peak_grid_cost_last_7_days": round(on_peak_grid_cost, 2),
            "ev_grid_charging_cost_last_7_days": round(ev_grid_draw_cost, 2),
            "actionable_recommendations": [rec1, rec2, rec3, rec4, rec5]
        }
    except Exception as e:
        log_csv.error(f"Error calculating energy saving advice: {e}")
        return {"error": f"Failed to calculate energy saving advice: {e}"}

def get_monthly_billing_data(period: str = "last_month") -> dict:
    """
    Aggregates log rows for a given month ('last_month', 'this_month', or 'YYYY-MM').
    Returns daily usage records (EXCLUDING fixed daily fee from daily cost) and monthly billing summary.
    """
    now = datetime.now(config.TZ)
    period_clean = str(period or "last_month").lower().strip()
    
    if period_clean in ["last_month", "previous_month", "last month"]:
        first_of_this_month = now.date().replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        start_date = last_of_prev_month.replace(day=1)
        end_date = last_of_prev_month
    elif period_clean in ["this_month", "month", "this month"]:
        start_date = now.date().replace(day=1)
        end_date = now.date()
    else:
        month_formats = [
            "%Y-%m", "%Y/%m", "%m/%Y", "%Y-%m-%d",
            "%B %Y", "%b %Y", "%B", "%b"
        ]
        parsed_date = None
        for fmt in month_formats:
            try:
                dt_cand = datetime.strptime(period_clean, fmt)
                y = dt_cand.year
                if fmt in ["%B", "%b"]:
                    y = now.year
                    if dt_cand.month > now.month:
                        y -= 1
                parsed_date = date(y, dt_cand.month, 1)
                break
            except Exception:
                continue

        if parsed_date:
            start_date = parsed_date
            if start_date.month == 12:
                end_date = date(start_date.year, 12, 31)
            else:
                end_date = date(start_date.year, start_date.month + 1, 1) - timedelta(days=1)
        else:
            first_of_this_month = now.date().replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            start_date = last_of_prev_month.replace(day=1)
            end_date = last_of_prev_month

    if start_date > now.date():
        return {"error": f"Cannot generate monthly report for a future month ({start_date.strftime('%B %Y')})."}

    days_count = (end_date - start_date).days + 1
    month_label = start_date.strftime("%B %Y")

    rows = get_all_log_rows(days=365)
    if not rows:
        return {"error": "No log data available for monthly report."}

    daily_map = {}
    curr = start_date
    while curr <= end_date:
        daily_map[curr.strftime("%Y-%m-%d")] = {
            "date": curr.strftime("%Y-%m-%d"),
            "date_short": curr.strftime("%b %d"),
            "day_num": curr.day,
            "home_kwh": 0.0,
            "solar_kwh": 0.0,
            "grid_import_kwh": 0.0,
            "solar_export_kwh": 0.0,
            "variable_grid_cost": 0.0,
            "solar_export_credit": 0.0,
            "ev_grid_kwh": 0.0,
            "ev_grid_cost": 0.0,
            "readings_count": 0
        }
        curr += timedelta(days=1)

    interval_h = config.CHECK_INTERVAL_MINUTES / 60.0

    for row in rows:
        row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
        try:
            row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        if start_date <= row_date <= end_date:
            d_key = row_date.strftime("%Y-%m-%d")
            if d_key not in daily_map:
                continue

            day_data = daily_map[d_key]
            day_data["readings_count"] += 1

            grid_kw = float(row.get("grid_kw", 0) or 0)
            solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
            home_kw = max(0.0, float(row.get("home_kw", 0) or 0))

            ts_str = row.get("timestamp")
            try:
                dt = datetime.fromisoformat(ts_str)
                rate = get_tou_rate(dt)
            except Exception:
                rate = float(row.get("tou_rate_per_kwh", 0) or 0.1706)

            day_data["home_kwh"] += home_kw * interval_h
            day_data["solar_kwh"] += solar_kw * interval_h

            if grid_kw > 0:
                imp_kwh = grid_kw * interval_h
                day_data["grid_import_kwh"] += imp_kwh
                day_data["variable_grid_cost"] += imp_kwh * rate

                if _is_ev_charging_row(row):
                    ev_grid_kw_val = min(grid_kw, 4.8)
                    ev_kwh = ev_grid_kw_val * interval_h
                    day_data["ev_grid_kwh"] += ev_kwh
                    day_data["ev_grid_cost"] += ev_kwh * rate
            else:
                exp_kwh = abs(grid_kw) * interval_h
                day_data["solar_export_kwh"] += exp_kwh
                credit = exp_kwh * (config.UTILITY_SOLAR_EXPORT_CREDIT_RATE * config.UTILITY_TAX_MULTIPLIER)
                day_data["solar_export_credit"] += credit

    daily_list = []
    tot_home = 0.0
    tot_solar = 0.0
    tot_grid_import = 0.0
    tot_solar_export = 0.0
    tot_variable_cost = 0.0
    tot_export_credit = 0.0
    tot_ev_kwh = 0.0
    tot_ev_cost = 0.0

    expected_daily_readings = int(24 * 60 / max(1, config.CHECK_INTERVAL_MINUTES))

    for d_key in sorted(daily_map.keys()):
        d = daily_map[d_key]
        readings = d["readings_count"]
        # Smart Normalization: If a few readings were missed (e.g. during app updates or restarts),
        # scale proportionally so missing a few 15-min rows does not skew totals.
        if 0 < readings < expected_daily_readings and (expected_daily_readings - readings) <= 12:
            scale = expected_daily_readings / float(readings)
            d["home_kwh"] *= scale
            d["solar_kwh"] *= scale
            d["grid_import_kwh"] *= scale
            d["solar_export_kwh"] *= scale
            d["variable_grid_cost"] *= scale
            d["solar_export_credit"] *= scale
            d["ev_grid_kwh"] *= scale
            d["ev_grid_cost"] *= scale

        d["home_kwh"] = round(d["home_kwh"], 2)
        d["solar_kwh"] = round(d["solar_kwh"], 2)
        d["grid_import_kwh"] = round(d["grid_import_kwh"], 2)
        d["solar_export_kwh"] = round(d["solar_export_kwh"], 2)
        d["variable_grid_cost"] = round(d["variable_grid_cost"], 2)
        d["solar_export_credit"] = round(d["solar_export_credit"], 2)
        d["net_variable_cost"] = round(max(0.0, d["variable_grid_cost"] - d["solar_export_credit"]), 2)
        d["ev_grid_kwh"] = round(d["ev_grid_kwh"], 2)
        d["ev_grid_cost"] = round(d["ev_grid_cost"], 2)

        daily_list.append(d)

        tot_home += d["home_kwh"]
        tot_solar += d["solar_kwh"]
        tot_grid_import += d["grid_import_kwh"]
        tot_solar_export += d["solar_export_kwh"]
        tot_variable_cost += d["variable_grid_cost"]
        tot_export_credit += d["solar_export_credit"]
        tot_ev_kwh += d["ev_grid_kwh"]
        tot_ev_cost += d["ev_grid_cost"]

    fixed_service_fee = (config.UTILITY_FIXED_MONTHLY_FEE / 30.0) * days_count * config.UTILITY_TAX_MULTIPLIER
    estimated_bill_total = max(0.0, fixed_service_fee + tot_variable_cost - tot_export_credit)
    appliance_cost = max(0.0, tot_variable_cost - tot_ev_cost)
    self_powered_pct = round(max(0.0, min(100.0, (1 - tot_grid_import / max(tot_home, 0.01)) * 100.0)), 1) if tot_home > 0 else 100.0

    provider_name = getattr(config, "UTILITY_PROVIDER", "MID")
    plan_label = f"Modesto Irrigation District (MID) Rate N2-EVD" if provider_name == "MID" else f"PG&E EV2-A Rate Schedule" if provider_name == "PGE" else f"Custom Rate ({provider_name})"

    return {
        "month_label": month_label,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "days_count": days_count,
        "total_home_kwh": round(tot_home, 2),
        "total_solar_kwh": round(tot_solar, 2),
        "total_grid_import_kwh": round(tot_grid_import, 2),
        "total_solar_export_kwh": round(tot_solar_export, 2),
        "total_variable_grid_cost_dollars": round(tot_variable_cost, 2),
        "total_solar_export_credit_dollars": round(tot_export_credit, 2),
        "fixed_service_fee_dollars": round(fixed_service_fee, 2),
        "estimated_net_bill_dollars": round(estimated_bill_total, 2),
        "ev_charging_kwh": round(tot_ev_kwh, 2),
        "ev_charging_cost_dollars": round(tot_ev_cost, 2),
        "home_appliances_cost_dollars": round(appliance_cost, 2),
        "self_powered_percentage": self_powered_pct,
        "utility_rate_plan": plan_label,
        "daily_records": daily_list
    }
