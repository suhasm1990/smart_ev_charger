"""Read-only web dashboard over the daemon's live readings and telemetry log."""
from dashboard.data import get_dashboard_data, record_live
from dashboard.server import start_dashboard_server

__all__ = ["get_dashboard_data", "record_live", "start_dashboard_server"]
