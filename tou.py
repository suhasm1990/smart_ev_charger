from datetime import datetime
import config

def get_tou_period(now: datetime) -> str:
    hour = now.hour
    if now.weekday() >= 5 or now.date() in config.PGE_HOLIDAYS:
        return "off_peak"
    if 17 <= hour < 20:
        return "on_peak"
    if (13 <= hour < 17) or (20 <= hour < 23):
        return "partial_peak"
    return "off_peak"

# MID (Modesto Irrigation District) Rate N2-EVD Constants
MID_FIXED_MONTHLY_FEE = 32.00
MID_VOLUMETRIC_ADDER = 0.0151  # EEA ($0.012) + CIA ($0.0028) + State Surcharge ($0.0003)
MID_MOUNTAIN_HOUSE_TAX = 1.065 # 6.5% Mountain House Surcharge
MID_SOLAR_EXPORT_CREDIT_RATE = 0.076 # $0.076/kWh Excess Generation Credit

def get_base_tou_rate(now: datetime) -> float:
    period = get_tou_period(now)
    month  = now.month
    summer = 5 <= month <= 9

    rates = {
        "on_peak":      0.31235 if summer else 0.22401,
        "partial_peak": 0.20192 if summer else 0.14324,
        "off_peak":     0.14513 if summer else 0.14324,
    }
    return rates[period]

def get_tou_rate(now: datetime) -> float:
    """Returns the effective MID delivered rate per kWh including volumetric surcharges and 6.5% Mountain House tax."""
    base = get_base_tou_rate(now)
    return round((base + MID_VOLUMETRIC_ADDER) * MID_MOUNTAIN_HOUSE_TAX, 5)


def is_expensive_period(now: datetime) -> bool:
    return get_tou_period(now) in ("on_peak", "partial_peak")

def is_in_night_blackout(now: datetime) -> bool:
    hour = now.hour
    if config.NIGHT_BLACKOUT_START_HOUR > config.NIGHT_BLACKOUT_END_HOUR:
        return hour >= config.NIGHT_BLACKOUT_START_HOUR or hour < config.NIGHT_BLACKOUT_END_HOUR
    return config.NIGHT_BLACKOUT_START_HOUR <= hour < config.NIGHT_BLACKOUT_END_HOUR

def is_morning_window(now: datetime) -> bool:
    return now.weekday() < 5 and config.MORNING_CHARGE_START_HOUR <= now.hour < config.MORNING_CHARGE_END_HOUR

def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5
