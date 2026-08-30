"""Shared charger operations composed from the low-level ChargePoint calls."""
from core import config
from reporting.logger import log_chargepoint
from services.chargepoint import set_charger_amperage_limit, stop_charger


def stop_and_restore_defaults():
    """Stops charging, then best-effort restores the default amperage limit.

    A failed amperage reset is only logged: the stop already succeeded, and the
    limit self-corrects on the next start.
    """
    stop_charger()
    try:
        set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
    except Exception as e:
        log_chargepoint.warning(f"Could not reset amperage to {config.DEFAULT_CHARGER_AMPERAGE}A: {e}")
