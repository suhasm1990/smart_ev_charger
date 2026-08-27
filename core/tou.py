"""Time-of-use rate periods and blackout windows for the configured utility."""
from datetime import datetime

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


def provider() -> str:
    return getattr(config, "UTILITY_PROVIDER", "MID").upper()


def provider_label() -> str:
    name = provider()
    return PROVIDER_LABELS.get(name, f"Custom Utility Rate ({name})")


def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


def get_tou_period(now: datetime) -> str:
    if is_weekend(now) or now.date() in config.PGE_HOLIDAYS:
        return "off_peak"
    hour = now.hour
    if 17 <= hour < 20:
        return "on_peak"
    if 13 <= hour < 17 or 20 <= hour < 23:
        return "partial_peak"
    return "off_peak"


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
