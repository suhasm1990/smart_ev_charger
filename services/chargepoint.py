"""ChargePoint Home Flex control.

The vendor library is async-only, so all coroutines run on one long-lived
background event loop and are exposed to the daemon as blocking calls.
"""
import asyncio
import re
import threading

from core import config
from reporting.logger import log_chargepoint

_client = None
_client_lock = threading.Lock()


class ChargePointStartError(Exception):
    """Raised when ChargePoint refuses to start a charging session."""


async def _get_client():
    global _client
    if _client is None:
        if not config.CHARGEPOINT_USERNAME or not config.CHARGEPOINT_COULOMB_TOKEN:
            raise RuntimeError("CHARGEPOINT_USERNAME and CHARGEPOINT_COULOMB_TOKEN must be configured.")
        from python_chargepoint import ChargePoint
        log_chargepoint.info("Initializing new ChargePoint client session")
        _client = await ChargePoint.create(
            username=config.CHARGEPOINT_USERNAME,
            coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
        )
    return _client


async def _drop_client():
    """Discards the cached client so the next call re-authenticates."""
    global _client
    stale, _client = _client, None
    if stale is not None:
        try:
            await stale.close()
        except Exception:
            pass


def _clean_error(e: Exception) -> str:
    """Reduces Cloudflare/HTML error bodies to a single readable line."""
    raw = str(e)
    if any(marker in raw for marker in ("<!DOCTYPE", "<html", "cf-error-details", "<div")):
        for code, label in (("502", "Bad Gateway"), ("503", "Service Unavailable")):
            if code in raw:
                return f"ChargePoint API {label} (Cloudflare {code})"
        return "ChargePoint API Server Error (Cloudflare Response)"
    lines = [ln.strip() for ln in re.sub(r"<[^>]+>", "", raw).splitlines() if ln.strip()]
    return (lines[0] if lines else raw)[:150]


# ── Coroutines ──────────────────────────────────────────────────────────────

async def _start_charger(amperage_limit: int):
    try:
        try:
            status = await _get_charger_status()
            if not status.get("is_plugged_in", True):
                raise ChargePointStartError("Vehicle is not plugged in. Please plug in the connector.")
            if status.get("charging_status") == "CHARGING":
                log_chargepoint.info("Charger is already charging. Skipping redundant start request.")
                return
        except ChargePointStartError:
            raise
        except Exception as e:
            log_chargepoint.warning(f"Charger pre-check failed, attempting start anyway: {_clean_error(e)}")

        client = await _get_client()
        try:
            await client.set_amperage_limit(charger_id=config.CHARGEPOINT_DEVICE_ID, amperage_limit=amperage_limit)
        except Exception as e:
            log_chargepoint.warning(f"Failed to set initial amperage limit: {_clean_error(e)}")

        session = await client.start_charging_session(device_id=config.CHARGEPOINT_DEVICE_ID)
        log_chargepoint.info(f"Session STARTED | session_id={session.session_id} | amperage={amperage_limit}A")
    except ChargePointStartError:
        raise
    except Exception as e:
        # The vendor library raises ValidationError when the session started but
        # the status endpoint has not caught up yet — that is a success, not a failure.
        if "ValidationError" in type(e).__name__:
            log_chargepoint.info("Session started; ChargePoint status not yet consistent. Ignoring.")
            return
        await _drop_client()
        message = str(e)
        if "Failed to start charging" in message or "422" in message or "CommunicationError" in type(e).__name__:
            raise ChargePointStartError(f"ChargePoint start rejected: {_clean_error(e)}") from e
        raise


async def _stop_charger():
    try:
        client = await _get_client()
        status = await client.get_user_charging_status()
        session_id = getattr(status, "session_id", None) if status else None
        if not session_id:
            home = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
            if home.charging_status != "CHARGING":
                log_chargepoint.info("No active charging session to stop (already idle).")
                return
            session_id = 0

        from python_chargepoint.session import _send_command
        await _send_command(
            client=client,
            action="stop",
            device_id=config.CHARGEPOINT_DEVICE_ID,
            port_number=1,
            session_id=session_id,
        )
        log_chargepoint.info(f"Session STOPPED and confirmed by hardware | session_id={session_id}")
    except Exception as e:
        message = _clean_error(e)
        if "already stopped" in message.lower() or "not in use" in message.lower():
            log_chargepoint.info(f"Charger session already stopped: {message}")
            return
        log_chargepoint.warning(f"Error executing stop command: {message}")
        await _drop_client()
        raise


async def _get_charger_status() -> dict:
    try:
        client = await _get_client()
        home = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
        # The home-charger endpoint lags behind reality, so an active user
        # session is treated as authoritative evidence that charging is live.
        user_status = await client.get_user_charging_status()

        session_data = {"energy_kwh": 0.0, "power_kw": 0.0, "miles_added": 0.0,
                        "charging_time_seconds": 0, "session_start_time": None}
        session_id = getattr(user_status, "session_id", None) if user_status else None
        if session_id:
            try:
                session = await client.get_charging_session(session_id)
                if session:
                    raw_time = int(getattr(session, "charging_time", 0) or 0)
                    start = getattr(session, "start_time", None)
                    session_data = {
                        "energy_kwh": float(getattr(session, "energy_kwh", 0.0) or 0.0),
                        "power_kw": float(getattr(session, "power_kw", 0.0) or 0.0),
                        "miles_added": float(getattr(session, "miles_added", 0.0) or 0.0),
                        # The API reports milliseconds for long sessions and seconds for short ones.
                        "charging_time_seconds": raw_time // 1000 if raw_time > 1000 else raw_time,
                        "session_start_time": start.astimezone(config.TZ) if start else None,
                    }
            except Exception as e:
                log_chargepoint.warning(f"Error fetching active session details: {_clean_error(e)}")

        return {
            "charging_status": "CHARGING" if (home.charging_status == "CHARGING" or user_status) else home.charging_status,
            "is_plugged_in": home.is_plugged_in,
            "is_connected": home.is_connected,
            "amperage_limit": home.amperage_limit,
            **session_data,
        }
    except Exception as e:
        log_chargepoint.warning(f"Error getting charger status, resetting session: {_clean_error(e)}")
        await _drop_client()
        raise


async def _set_amperage_limit(amperage: int):
    try:
        client = await _get_client()
        await client.set_amperage_limit(charger_id=config.CHARGEPOINT_DEVICE_ID, amperage_limit=amperage)
        log_chargepoint.info(f"Amperage limit set to {amperage}A")
    except Exception:
        await _drop_client()
        raise


# ── Background event loop bridge ────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Starts (or restarts) the dedicated event loop thread on first use."""
    global _loop, _loop_thread
    with _client_lock:
        if _loop_thread is not None and _loop_thread.is_alive() and _loop is not None:
            return _loop
        ready = threading.Event()

        def run():
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop.call_soon(ready.set)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=run, daemon=True, name="ChargePointLoop")
        _loop_thread.start()
        ready.wait(timeout=10.0)
        return _loop


# Must stay comfortably below main.CYCLE_TIMEOUT_SECONDS (45s): a cycle makes up
# to two ChargePoint calls, and only cancellation *inside* the event loop can
# actually unblock a hung vendor call — future.cancel() cannot.
CHARGEPOINT_CALL_TIMEOUT = 30.0


def _run(coro, timeout: float = CHARGEPOINT_CALL_TIMEOUT):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout=timeout), loop)
    try:
        return future.result(timeout=timeout + 5.0)
    except TimeoutError:
        future.cancel()
        log_chargepoint.warning(f"ChargePoint API call timed out after {timeout}s")
        # The wedged client's connection state is suspect — re-auth on next call.
        asyncio.run_coroutine_threadsafe(_drop_client(), loop)
        raise


def start_charger(amperage_limit: int = None):
    _run(_start_charger(amperage_limit if amperage_limit is not None else config.DEFAULT_CHARGER_AMPERAGE))


def stop_charger():
    _run(_stop_charger())


def get_charger_status() -> dict:
    return _run(_get_charger_status())


def set_charger_amperage_limit(amperage: int):
    _run(_set_amperage_limit(amperage))
