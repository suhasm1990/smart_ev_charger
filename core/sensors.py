"""Night-time clean-up of Powerwall readings, shared by live telemetry and log analytics."""
from core import config

_IDLE_KW = 0.05    # Battery flow below this is treated as resting.
_NOISE_KW = 0.25   # Night solar / export below this is inverter noise.


def is_night(hour: int) -> bool:
    return hour >= config.SOLAR_NIGHT_CUTOFF_HOUR_START or hour < config.SOLAR_NIGHT_CUTOFF_HOUR_END


def clamp_night_noise(hour: int, solar_kw: float, home_kw: float, grid_kw: float,
                      battery_kw: float) -> tuple[float, float, float]:
    """Returns (solar, home, grid) kW with night sensor noise removed.

    At night: phantom solar and phantom export are zeroed, and while the
    Powerwall rests the house and grid are floored at the overnight baseline.
    """
    if not is_night(hour):
        return solar_kw, home_kw, grid_kw
    if solar_kw <= _NOISE_KW:
        solar_kw = 0.0
    if solar_kw == 0.0:
        if battery_kw >= -_IDLE_KW and -_NOISE_KW <= grid_kw < 0.0:
            grid_kw = 0.0
        if abs(battery_kw) < _IDLE_KW:
            baseline = config.OVERNIGHT_BASELINE_KW
            home_kw, grid_kw = max(home_kw, baseline), max(grid_kw, baseline)
    return solar_kw, home_kw, grid_kw
