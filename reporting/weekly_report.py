"""Weekly car and home energy report, sent to Telegram every Monday morning.

Car figures come from the charger log: ChargePoint's metered kWh, split per
15-minute slot into solar, Powerwall and grid, with the grid share priced at
the TOU rate (the same pricing as the per-session cost message).

Whole-home use is an estimate. The Tesla Gateway under-reads grid power, and
so the home load derived from it, mostly while the car charges. So the
Gateway's home reading is used only while the car is not charging, and the
car's own ChargePoint kWh is added on top.
"""
from datetime import date, datetime, timedelta

from core import config
from reporting import energy_history
from reporting.csv_logger import _ev_intervals_between, _resolve_date_range, sessions_between

_MAX_SESSIONS_LISTED = 10


def _car_totals(start: date, end: date) -> dict:
    t = dict.fromkeys(("kwh", "solar_kwh", "battery_kwh", "grid_kwh", "cost",
                       "peak_grid_kwh", "peak_cost", "tesla_home_while_charging_kwh"), 0.0)
    for iv in _ev_intervals_between(start, end):
        r = iv.reading
        if iv.session < 0 or r.day < start:
            continue
        t["kwh"] += iv.kwh
        t["solar_kwh"] += iv.solar_kwh
        t["battery_kwh"] += iv.battery_kwh
        t["grid_kwh"] += iv.grid_kwh
        t["cost"] += iv.grid_cost
        if r.tou_period in ("on_peak", "partial_peak"):
            t["peak_grid_kwh"] += iv.grid_kwh
            t["peak_cost"] += iv.grid_cost
        t["tesla_home_while_charging_kwh"] += r.home_kw * r.interval_h
    return t


def week_summary(start: date, end: date) -> dict:
    """Car and whole-home totals for `start`..`end` (inclusive)."""
    car = _car_totals(start, end)
    metered = energy_history.daily_totals(start, end)
    tesla_home = sum(d["home_kwh"] for d in metered.values())
    house = max(0.0, tesla_home - car["tesla_home_while_charging_kwh"])
    return {
        "start": start, "end": end,
        "car": car,
        "sessions": sessions_between(start, end),
        "has_home_data": bool(metered),
        "house_kwh": house,
        "home_kwh": house + car["kwh"],
        "solar_kwh": sum(d["solar_kwh"] for d in metered.values()),
        "export_kwh": sum(d["solar_export_kwh"] for d in metered.values()),
    }


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole > 0 else "0%"


def _change(now: float, before: float, unit: str) -> str:
    diff = now - before
    sign = "+" if diff >= 0 else "−"
    return f"{sign}${abs(diff):.2f}" if unit == "$" else f"{sign}{abs(diff):.1f} {unit}"


def _session_line(s: dict) -> str:
    try:
        when = datetime.fromisoformat(s["start_time"]).strftime("%a %-m/%-d %-I:%M %p")
    except ValueError:
        when = s["start_time"]
    return f"• {when} · {s['energy_kwh']:.1f} kWh · ${s['cost']:.2f}"


def weekly_report_text(period: str = "last_week") -> str:
    """The weekly report as Telegram HTML; `period` is 'last_week' or 'this_week'."""
    start, end, _ = _resolve_date_range(period, default="last_week")
    this, prev = week_summary(start, end), week_summary(start - timedelta(days=7), end - timedelta(days=7))
    car, prev_car = this["car"], prev["car"]
    miles = car["kwh"] * config.EV_MILES_PER_KWH
    gas_cost = miles / config.GAS_CAR_MPG * config.GAS_PRICE_PER_GALLON
    n = len(this["sessions"])

    lines = [f"📅 <b>Weekly energy report</b>\n{start:%b %-d} – {end:%b %-d}", "", "🚗 <b>Car</b>"]
    if car["kwh"]:
        lines += [
            f"{n} session{'' if n == 1 else 's'} · {car['kwh']:.1f} kWh · ~{miles:.0f} mi",
            f"Cost ≈ <b>${car['cost']:.2f}</b> · {car['cost'] / car['kwh'] * 100:.1f}¢/kWh"
            f" · {car['cost'] / miles * 100:.1f}¢/mi",
            f"☀️ Solar {car['solar_kwh']:.1f} · 🔋 Powerwall {car['battery_kwh']:.1f}"
            f" · 🔌 Grid {car['grid_kwh']:.1f} kWh ({_pct(car['solar_kwh'] + car['battery_kwh'], car['kwh'])} free)",
            f"Grid at peak hours: {car['peak_grid_kwh']:.1f} kWh (${car['peak_cost']:.2f})"
            f" · off-peak {car['grid_kwh'] - car['peak_grid_kwh']:.1f} kWh",
            f"⛽ A {config.GAS_CAR_MPG:g} mpg gas car: ≈ ${gas_cost:.2f} at ${config.GAS_PRICE_PER_GALLON:.2f}/gal",
        ]
    else:
        lines.append("No charging this week.")
    lines.append(f"vs previous week: {_change(car['kwh'], prev_car['kwh'], 'kWh')}, "
                 f"{_change(car['cost'], prev_car['cost'], '$')}")
    if this["sessions"]:
        lines += ["", "<b>Sessions</b>"] + [_session_line(s) for s in this["sessions"][-_MAX_SESSIONS_LISTED:]]

    lines += ["", "🏠 <b>Home</b>"]
    if this["has_home_data"]:
        lines += [
            f"Whole home ≈ <b>{this['home_kwh']:.0f} kWh</b>: car {car['kwh']:.0f} + rest of house {this['house_kwh']:.0f}",
            f"☀️ Solar made {this['solar_kwh']:.1f} kWh: {_pct(car['solar_kwh'], this['solar_kwh'])} went into the car,"
            f" {this['export_kwh']:.1f} kWh sent to the grid",
        ]
        if prev["has_home_data"]:
            lines.append(f"vs previous week: {_change(this['home_kwh'], prev['home_kwh'], 'kWh')}")
        lines.append("<i>Estimate: the Tesla Gateway under-reads grid power, so the car's kWh come from "
                     "ChargePoint, and the rest of the house likely reads a little low.</i>")
    else:
        lines.append("No Tesla energy history for this week yet.")
    return "\n".join(lines)
