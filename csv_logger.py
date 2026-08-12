import csv
import os
import requests
from datetime import datetime

import state
import config
from logger import log_csv
from tou import get_tou_period, get_tou_rate, is_expensive_period, is_in_night_blackout, is_weekend

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
    return round((datetime.now(config.TZ) - state.charge_session_start).total_seconds() / 60, 1)

def log_to_csv(stats: dict, action: str, reason: str, now: datetime):
    tou    = get_tou_period(now)
    rate   = get_tou_rate(now)
    grid   = stats["grid_kw"]

    est_cost = round(max(0, grid) * rate / 60, 6)

    row = [
        now.isoformat(),
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        now.strftime("%A"),
        is_weekend(now),
        tou,
        rate,
        is_expensive_period(now),
        stats["solar_kw"],
        stats["home_kw"],
        stats["solar_surplus_kw"],
        stats["battery_kw"],
        stats["grid_kw"],
        stats["battery_pct"],
        stats["self_powered_pct"],
        config.BATTERY_START_PCT,
        config.BATTERY_STOP_PCT,
        0.0,
        0.0,
        state.charger_state,
        action,
        reason,
        get_session_minutes(),
        state.session_count_today,
        state.session_stop_reason or "",
        is_in_night_blackout(now),
        state.manual_mode,
        stats["island_mode"],
        stats["storm_mode"],
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
        from sheets_db import append_log_row
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
        from sheets_db import get_recent_logs
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

def get_daily_charging_cost(period: str = "today") -> dict:
    """Calculates total grid energy drawn (kWh), solar energy used (kWh), and cost ($) for EV charging for a period ('today', 'yesterday', 'this_week', 'this_month', or a specific date YYYY-MM-DD)."""
    from datetime import timedelta
    now = datetime.now(config.TZ)
    period_clean = str(period or "today").lower().strip()

    if period_clean in ["today", ""]:
        start_date = now.date()
        end_date = now.date()
        period_label = f"Today ({start_date})"
    elif period_clean == "yesterday":
        start_date = now.date() - timedelta(days=1)
        end_date = start_date
        period_label = f"Yesterday ({start_date})"
    elif period_clean in ["this_week", "week", "7days", "this week"]:
        start_date = now.date() - timedelta(days=now.weekday())
        end_date = now.date()
        period_label = f"This Week ({start_date} to {end_date})"
    elif period_clean in ["this_month", "month", "30days", "this month"]:
        start_date = now.date().replace(day=1)
        end_date = now.date()
        period_label = f"This Month ({start_date.strftime('%B %Y')})"
    else:
        try:
            parsed = datetime.strptime(period_clean, "%Y-%m-%d").date()
            start_date = parsed
            end_date = parsed
            period_label = f"Date ({start_date})"
        except Exception:
            start_date = now.date()
            end_date = now.date()
            period_label = f"Today ({start_date})"

    rows = get_all_log_rows()
    if not rows:
        return {"error": "No log data found yet."}

    total_grid_kwh = 0.0
    total_solar_kwh = 0.0
    total_grid_cost = 0.0
    total_charging_mins = 0
    charging_intervals_count = 0

    try:
        for row in rows:
            row_date_str = row.get("date") or (row.get("timestamp", "")[:10])
            try:
                row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
            except Exception:
                continue

            if start_date <= row_date <= end_date:
                state_str = row.get("charger_state", "")
                action_str = row.get("action", "")
                if "CHARGING" in state_str or action_str == "start":
                    charging_intervals_count += 1
                    grid_kw = max(0.0, float(row.get("grid_kw", 0) or 0))
                    solar_kw = max(0.0, float(row.get("solar_kw", 0) or 0))
                    home_kw = max(0.0, float(row.get("home_kw", 0) or 0))
                    ts_str = row.get("timestamp")
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        from tou import get_tou_rate
                        rate = get_tou_rate(dt)
                    except Exception:
                        rate = float(row.get("tou_rate_per_kwh", 0) or 0.1706)
                    
                    interval_h = config.CHECK_INTERVAL_MINUTES / 60.0
                    
                    # EV charger typical power (e.g. 20A = 4.8 kW, 32A = 7.68 kW)
                    ev_power_kw = 4.8
                    
                    # EV's share of grid draw (excluding AC / home base load)
                    ev_grid_kw = min(grid_kw, ev_power_kw)
                    house_other_grid_kw = max(0.0, grid_kw - ev_grid_kw)
                    
                    grid_kwh = ev_grid_kw * interval_h
                    solar_surplus_kw = max(0.0, solar_kw - (home_kw - ev_power_kw))
                    solar_kwh = min(max(0.0, solar_surplus_kw), ev_power_kw) * interval_h

                    cost = grid_kwh * rate
                    total_grid_kwh += grid_kwh
                    total_solar_kwh += solar_kwh
                    total_grid_cost += cost
                    total_charging_mins += config.CHECK_INTERVAL_MINUTES

        total_kwh = total_grid_kwh + total_solar_kwh
        grid_pct = (total_grid_kwh / total_kwh * 100.0) if total_kwh > 0 else 0.0
        solar_pct = (total_solar_kwh / total_kwh * 100.0) if total_kwh > 0 else 0.0

        from tou import get_tou_rate
        current_rate = get_tou_rate(now)

        return {
            "period": period_label,
            "total_charging_hours": round(total_charging_mins / 60.0, 1),
            "charging_intervals_count": charging_intervals_count,
            "ev_grid_kwh_pulled": round(total_grid_kwh, 2),
            "solar_kwh_used": round(total_solar_kwh, 2),
            "total_kwh_added": round(total_kwh, 2),
            "ev_grid_cost_dollars": round(total_grid_cost, 2),
            "grid_percentage": round(grid_pct, 1),
            "solar_percentage": round(solar_pct, 1),
            "estimated_solar_savings_dollars": round(total_solar_kwh * current_rate, 2),
            "calculation_note": "EV grid draw is isolated from home appliances (AC/washing machine) by capping grid draw at charger max power rate."
        }
    except Exception as e:
        log_csv.error(f"Error calculating charging cost summary: {e}")
        return {"error": f"Failed to calculate charging cost: {e}"}

def get_home_energy_summary(period: str = "today") -> dict:
    """Calculates total home energy consumed (kWh), total solar generated (kWh), total grid energy imported (kWh), solar export credits ($), fixed service fees ($), and exact estimated MID utility bill breakdown."""
    from datetime import timedelta
    from tou import MID_FIXED_MONTHLY_FEE, MID_SOLAR_EXPORT_CREDIT_RATE, MID_MOUNTAIN_HOUSE_TAX, get_tou_rate
    
    now = datetime.now(config.TZ)
    period_clean = str(period or "today").lower().strip()

    if period_clean in ["today", ""]:
        start_date = now.date()
        end_date = now.date()
        period_label = f"Today ({start_date})"
    elif period_clean == "yesterday":
        start_date = now.date() - timedelta(days=1)
        end_date = start_date
        period_label = f"Yesterday ({start_date})"
    elif period_clean in ["this_week", "week", "7days", "this week"]:
        start_date = now.date() - timedelta(days=now.weekday())
        end_date = now.date()
        period_label = f"This Week ({start_date} to {end_date})"
    elif period_clean in ["this_month", "month", "30days", "this month"]:
        start_date = now.date().replace(day=1)
        end_date = now.date()
        period_label = f"This Month ({start_date.strftime('%B %Y')})"
    else:
        try:
            parsed = datetime.strptime(period_clean, "%Y-%m-%d").date()
            start_date = parsed
            end_date = parsed
            period_label = f"Date ({start_date})"
        except Exception:
            start_date = now.date()
            end_date = now.date()
            period_label = f"Today ({start_date})"

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
                
                # Compute rate including MID EEA, CIA, State surcharges & 6.5% tax
                ts_str = row.get("timestamp")
                try:
                    dt = datetime.fromisoformat(ts_str)
                    rate = get_tou_rate(dt)
                except Exception:
                    rate = float(row.get("tou_rate_per_kwh", 0) or 0.1706)

                if grid_kw > 0:
                    import_kwh = grid_kw * interval_h
                    total_grid_import_kwh += import_kwh
                    delivered_grid_cost += import_kwh * rate

                    state_str = row.get("charger_state", "")
                    action_str = row.get("action", "")
                    if "CHARGING" in state_str or action_str == "start":
                        ev_grid_kw_interval = min(grid_kw, 4.8)
                        ev_kwh = ev_grid_kw_interval * interval_h
                        ev_grid_kwh += ev_kwh
                        ev_grid_cost += ev_kwh * rate
                else:
                    export_kwh = abs(grid_kw) * interval_h
                    total_solar_export_kwh += export_kwh
                    solar_export_credit += export_kwh * (config.UTILITY_SOLAR_EXPORT_CREDIT_RATE * config.UTILITY_TAX_MULTIPLIER)

        fixed_service_fee = (config.UTILITY_FIXED_MONTHLY_FEE / 30.0) * days_count * config.UTILITY_TAX_MULTIPLIER
        estimated_bill_total = max(0.0, fixed_service_fee + delivered_grid_cost - solar_export_credit)
        non_ev_home_cost = max(0.0, delivered_grid_cost - ev_grid_cost)
        self_powered_pct = round(max(0.0, min(100.0, (1 - total_grid_import_kwh / max(total_home_kwh, 0.01)) * 100.0)), 1) if total_home_kwh > 0 else 100.0

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
    from datetime import timedelta
    from tou import get_tou_rate
    
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

        avg_surplus = {h: (sum(vals)/len(vals) if vals else 0.0) for h, vals in hourly_solar_surplus.items()}
        best_hours = [h for h, avg in avg_surplus.items() if avg >= 1.0]

        solar_window_str = f"{min(best_hours)}:00 - {max(best_hours)+1}:00" if best_hours else "10:00 AM - 3:00 PM"

        rec1 = f"Run heavy appliances (AC, washing machine, dishwasher, dryer) during {solar_window_str} when solar generation is at its peak."
        rec2 = "Avoid running heavy appliances between 5:00 PM and 8:00 PM (On-Peak hours when MID grid rates are $0.35/kWh)."
        rec3 = "Consuming your solar power directly saves 4.5x more money than exporting it back to MID ($0.35/kWh avoided vs $0.076/kWh solar export credit)."
        rec4 = "Charge EV during daytime solar surplus hours (10:00 AM - 3:00 PM) to avoid pulling grid power at night."

        return {
            "optimal_solar_appliance_window": solar_window_str,
            "cheapest_ev_charging_window": f"{solar_window_str} (Off-Peak solar surplus)",
            "hours_to_avoid_heavy_loads": "5:00 PM - 8:00 PM (On-Peak $0.35/kWh rate)",
            "on_peak_grid_cost_last_7_days": round(on_peak_grid_cost, 2),
            "ev_grid_charging_cost_last_7_days": round(ev_grid_draw_cost, 2),
            "actionable_recommendations": [rec1, rec2, rec3, rec4]
        }
    except Exception as e:
        log_csv.error(f"Error calculating energy saving advice: {e}")
        return {"error": f"Failed to calculate energy saving advice: {e}"}


def get_monthly_billing_data(period: str = "last_month") -> dict:
    """
    Aggregates log rows for a given month ('last_month', 'this_month', or 'YYYY-MM').
    Returns daily usage records (EXCLUDING fixed daily fee from daily cost) and monthly billing summary.
    """
    from datetime import timedelta, date
    from tou import get_tou_rate
    
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
        try:
            parsed_dt = datetime.strptime(period_clean, "%Y-%m").date()
            start_date = parsed_dt.replace(day=1)
            if start_date.month == 12:
                end_date = date(start_date.year, 12, 31)
            else:
                end_date = date(start_date.year, start_date.month + 1, 1) - timedelta(days=1)
        except Exception:
            first_of_this_month = now.date().replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            start_date = last_of_prev_month.replace(day=1)
            end_date = last_of_prev_month

    days_count = (end_date - start_date).days + 1
    month_label = start_date.strftime("%B %Y")

    rows = get_all_log_rows(days=60)
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

                state_str = row.get("charger_state", "")
                action_str = row.get("action", "")
                if "CHARGING" in state_str or action_str == "start":
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







