"""Powerwall live data: Tesla Fleet API when configured, NetZero as the fallback."""
import logging
import threading
import time
from datetime import datetime

from core import config
from services import netzero, tesla_fleet

log = logging.getLogger("TESLA")

# One alert per problem, at most every 6 hours, so a dead token does not
# notify every 15-minute cycle.
_ALERT_INTERVAL_S = 6 * 3600
_last_alert: dict[str, float] = {}
_alert_lock = threading.Lock()


def _alert_once(kind: str, message: str):
    with _alert_lock:
        now = time.monotonic()
        last = _last_alert.get(kind)
        if last is not None and now - last < _ALERT_INTERVAL_S:
            return
        _last_alert[kind] = now
    try:
        from reporting.notifications import notify
        notify(message)
    except Exception as e:  # never let an alert break the cycle
        log.warning(f"Could not send Tesla alert: {e}")


def _netzero_configured() -> bool:
    return bool(config.NETZERO_SITE_ID and config.NETZERO_API_TOKEN)


def active_source() -> str:
    return "tesla" if tesla_fleet.is_configured() else "netzero"


def get_powerwall_stats(now: datetime = None) -> dict:
    if not tesla_fleet.is_configured():
        return netzero.get_powerwall_stats(now)

    try:
        stats = tesla_fleet.get_powerwall_stats(now)
        _last_alert.pop("auth", None)
        return stats
    except tesla_fleet.TeslaAuthError as e:
        log.error(f"TESLA AUTH | {e}")
        _alert_once("auth", f"⚠️ <b>Tesla sign-in needed</b>\n{e}")
        error = e
    except Exception as e:
        log.warning(f"Tesla live_status failed: {type(e).__name__}: {e}")
        error = e

    if _netzero_configured():
        log.info("Using NetZero for this cycle (Tesla unavailable)")
        return netzero.get_powerwall_stats(now)
    raise error
