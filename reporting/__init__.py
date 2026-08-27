"""Telemetry logging, notifications, and energy analytics."""
from reporting.logger import (
    log, log_csv, log_chargepoint, log_decision, log_mode, log_netzero, tail_lines,
)
from reporting.notifications import notify
from reporting.csv_logger import (
    get_all_log_rows, get_daily_charging_cost, get_energy_saving_advice,
    get_home_energy_summary, get_monthly_billing_data, get_recent_sessions,
    get_session_minutes, log_to_csv,
)

__all__ = [
    "log", "log_csv", "log_chargepoint", "log_decision", "log_mode", "log_netzero",
    "tail_lines", "notify", "log_to_csv", "get_session_minutes", "get_all_log_rows",
    "get_recent_sessions", "get_daily_charging_cost", "get_home_energy_summary",
    "get_energy_saving_advice", "get_monthly_billing_data", "generate_monthly_report_image",
]


def generate_monthly_report_image(period: str = "last_month", data: dict = None) -> str:
    """Lazy proxy so importing this package does not pull in matplotlib."""
    from reporting.report_generator import generate_monthly_report_image as _impl
    return _impl(period, data)
