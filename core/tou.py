"""Time-of-use rate periods and blackout windows for the configured utility."""
from datetime import date, datetime, timedelta
from functools import lru_cache

from core import config

# Rate schedules keyed by provider, then by season, then by TOU period.
RATE_SCHEDULES = {
    # PG&E EV2-A
    "PGE": {
        "summer": {"on_peak": 0.59251, "partial_peak": 0.44812, "off_peak": 0.28312},
        "winter": {"on_peak": 0.43512, "partial_peak": 0.41200, "off_peak": 0.26512},
    },
    # Modesto Irrigation District Rate N2-EVD
    "MID": {
        "summer": {"on_peak": 0.31235, "partial_peak": 0.20192, "off_peak": 0.14513},
        "winter": {"on_peak": 0.22401, "partial_peak": 0.14324, "off_peak": 0.14324},
    },
}

PROVIDER_LABELS = {
    "MID": "Modesto Irrigation District (MID) Rate N2-EVD",
    "PGE": "PG&E EV2-A Rate Schedule",
}

# Weekday TOU boundaries (24-hour): the single source for every place that
# names these hours (period lookup, bot schedule tool, savings advice).
ON_PEAK_HOURS = (17, 20)
PARTIAL_PEAK_HOURS = ((13, 17), (20, 23))


def provider() -> str:
    return getattr(config, "UTILITY_PROVIDER", "MID").upper()


def provider_label() -> str:
    name = provider()
    return PROVIDER_LABELS.get(name, f"Custom Utility Rate ({name})")


def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th given weekday (0=Mon) of a month, e.g. the 3rd Monday of February."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last given weekday of a month, e.g. the last Monday of May."""
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


@lru_cache(maxsize=8)
def utility_holidays(year: int) -> frozenset:
    """The utility's observed holidays (priced off-peak), computed for any year.

    Fixed-date holidays are not shifted to the nearest weekday, matching the
    published PG&E/MID calendars this replaced.
    """
    return frozenset({
        date(year, 1, 1),              # New Year's Day
        _nth_weekday(year, 2, 0, 3),   # Presidents Day
        _last_weekday(year, 5, 0),     # Memorial Day
        date(year, 7, 4),              # Independence Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        date(year, 11, 11),            # Veterans Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        date(year, 12, 25),            # Christmas Day
    })


def is_utility_holiday(day: date) -> bool:
    return day in utility_holidays(day.year)


def get_tou_period(now: datetime) -> str:
    if is_weekend(now) or is_utility_holiday(now.date()):
        return "off_peak"
    hour = now.hour
    if ON_PEAK_HOURS[0] <= hour < ON_PEAK_HOURS[1]:
        return "on_peak"
    if any(lo <= hour < hi for lo, hi in PARTIAL_PEAK_HOURS):
        return "partial_peak"
    return "off_peak"


def weekday_schedule_description() -> dict:
    """Human-readable weekday TOU windows, derived from the boundary constants."""
    (p1_lo, p1_hi), (p2_lo, p2_hi) = PARTIAL_PEAK_HOURS
    return {
        "off_peak": f"00:00 - {p1_lo}:00 and {p2_hi}:00 - 24:00",
        "partial_peak_1": f"{p1_lo}:00 - {p1_hi}:00",
        "on_peak": f"{ON_PEAK_HOURS[0]}:00 - {ON_PEAK_HOURS[1]}:00",
        "partial_peak_2": f"{p2_lo}:00 - {p2_hi}:00",
    }


def get_base_tou_rate(now: datetime) -> float:
    season = "summer" if 5 <= now.month <= 9 else "winter"
    schedule = RATE_SCHEDULES.get(provider(), RATE_SCHEDULES["MID"])
    return schedule[season][get_tou_period(now)]


def get_tou_rate(now: datetime) -> float:
    """Effective delivered rate per kWh, including surcharges and local taxes."""
    base = get_base_tou_rate(now)
    adder = getattr(config, "UTILITY_VOLUMETRIC_ADDER", 0.0151)
    tax = getattr(config, "UTILITY_TAX_MULTIPLIER", 1.065)
    return round((base + adder) * tax, 5)


def is_expensive_period(now: datetime) -> bool:
    return get_tou_period(now) in ("on_peak", "partial_peak")


def is_in_night_blackout(now: datetime) -> bool:
    start, end = config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR
    if start > end:  # Window wraps past midnight.
        return now.hour >= start or now.hour < end
    return start <= now.hour < end


def calculate_mid_bill_components(
    on_peak_kwh: float,
    partial_peak_kwh: float,
    off_peak_kwh: float,
    export_kwh: float,
    month: int = 8,
    days: int = 31,
) -> dict:
    """Calculates exact line-item components for Modesto Irrigation District Rate N2-EVD.

    Matches the official MID billing statement:
      - Fixed customer service fee ($32.00 flat per monthly billing cycle)
      - Energy charges: On-Peak, Partial-Peak, Off-Peak
      - Mandated volumetric adjustments: EEA ($0.0120/kWh), CIA ($0.0028/kWh), State Surcharge ($0.0003/kWh)
      - Mountain House Surcharge: 6.5% on pre-tax subtotal
      - Excess generation solar export credit: $0.076/kWh flat
    """
    season = "summer" if 5 <= month <= 9 else "winter"
    schedule = RATE_SCHEDULES.get(provider(), RATE_SCHEDULES["MID"])[season]

    # Fixed fee: flat monthly fee (prorated only if short cycle < 28 days)
    fixed_fee = config.UTILITY_FIXED_MONTHLY_FEE
    if days < 28:
        fixed_fee = round(config.UTILITY_FIXED_MONTHLY_FEE / 30.0 * days, 2)

    total_delivered_kwh = on_peak_kwh + partial_peak_kwh + off_peak_kwh

    on_peak_cost = round(on_peak_kwh * schedule["on_peak"], 2)
    partial_peak_cost = round(partial_peak_kwh * schedule["partial_peak"], 2)
    off_peak_cost = round(off_peak_kwh * schedule["off_peak"], 2)
    energy_cost = round(on_peak_cost + partial_peak_cost + off_peak_cost, 2)

    eea_cost = round(total_delivered_kwh * getattr(config, "UTILITY_EEA_RATE", 0.0120), 2)
    cia_cost = round(total_delivered_kwh * getattr(config, "UTILITY_CIA_RATE", 0.0028), 2)
    state_surcharge = round(total_delivered_kwh * getattr(config, "UTILITY_STATE_SURCHARGE_RATE", 0.0003), 2)
    volumetric_adjustments = round(eea_cost + cia_cost + state_surcharge, 2)

    # Mountain House Surcharge applies to MID utility charges (excludes State surcharge)
    mid_subtotal = round(fixed_fee + energy_cost + eea_cost + cia_cost, 2)
    local_surcharge_rate = getattr(config, "UTILITY_LOCAL_SURCHARGE_PCT", 0.065)
    local_surcharge = round(mid_subtotal * local_surcharge_rate, 2)

    export_credit_rate = getattr(config, "UTILITY_SOLAR_EXPORT_CREDIT_RATE", 0.076)
    export_credit = round(export_kwh * export_credit_rate, 2)

    net_bill = round(max(0.0, mid_subtotal + local_surcharge + state_surcharge - export_credit), 2)

    return {
        "fixed_fee": fixed_fee,
        "on_peak_cost": on_peak_cost,
        "partial_peak_cost": partial_peak_cost,
        "off_peak_cost": off_peak_cost,
        "energy_cost": energy_cost,
        "eea_cost": eea_cost,
        "cia_cost": cia_cost,
        "state_surcharge": state_surcharge,
        "volumetric_adjustments": volumetric_adjustments,
        "subtotal_pre_tax": mid_subtotal,
        "local_surcharge": local_surcharge,
        "export_credit": export_credit,
        "export_credit_rate": export_credit_rate,
        "net_bill": net_bill,
        "total_delivered_kwh": round(total_delivered_kwh, 2),
        "total_export_kwh": round(export_kwh, 2),
    }

