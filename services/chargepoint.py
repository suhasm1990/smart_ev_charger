import asyncio
import re
import threading
from core import config
from reporting.logger import log_chargepoint

_cp_client = None

async def get_cp_client():
    global _cp_client
    if _cp_client is None:
        if not config.CHARGEPOINT_USERNAME or not config.CHARGEPOINT_COULOMB_TOKEN:
            raise RuntimeError("CHARGEPOINT_USERNAME or CHARGEPOINT_COULOMB_TOKEN is not configured in environment variables.")
        from python_chargepoint import ChargePoint
        log_chargepoint.info("Initializing new ChargePoint client session")
        _cp_client = await ChargePoint.create(
            username=config.CHARGEPOINT_USERNAME,
            coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
        )
    return _cp_client

class ChargePointStartError(Exception):
    """Raised when ChargePoint API fails to start a charging session."""
    pass

def _clean_error_str(e: Exception) -> str:
    raw = str(e)
    if "<!DOCTYPE" in raw or "<html" in raw or "cf-error-details" in raw or "<div" in raw:
        if "502" in raw:
            return "ChargePoint API Bad Gateway (Cloudflare 502)"
        if "503" in raw:
            return "ChargePoint API Service Unavailable (Cloudflare 503)"
        return "ChargePoint API Server Error (Cloudflare Response)"
    # Strip any residual HTML tags
    clean = re.sub(r'<[^>]+>', '', raw)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return lines[0][:150] if lines else clean[:150]

async def start_charger_async(amperage_limit: int = 20):
    global _cp_client
    try:
        # Pre-check status before calling start_charging_session
        try:
            status = await get_charger_status_async()
            if not status.get("is_plugged_in", True):
                log_chargepoint.warning("Vehicle is NOT plugged in. Aborting start request.")
                raise ChargePointStartError("Vehicle is not plugged in. Please plug in the connector.")
            if status.get("charging_status") == "CHARGING":
                log_chargepoint.info("Charger is already actively charging. Skipping redundant start request.")
                return
        except ChargePointStartError:
            raise
        except Exception as check_err:
            log_chargepoint.warning(f"Charger pre-check warning (proceeding with start attempt): {check_err}")

        client = await get_cp_client()
        try:
            await client.set_amperage_limit(charger_id=config.CHARGEPOINT_DEVICE_ID, amperage_limit=amperage_limit)
            log_chargepoint.info(f"Amperage limit set to {amperage_limit}A")
        except Exception as err:
            log_chargepoint.warning(f"Failed to set initial amperage limit: {err}")
            
        session = await client.start_charging_session(device_id=config.CHARGEPOINT_DEVICE_ID)
        log_chargepoint.info(f"Session STARTED | session_id={session.session_id} | amperage_limit={amperage_limit}A")
    except ChargePointStartError:
        raise
    except Exception as e:
        err_msg = str(e)
        if "ValidationError" in str(type(e)):
            log_chargepoint.warning("Session started, but ChargePoint returned empty status right away (eventual consistency bug in library). Ignoring.")
        else:
            # Reset client on general errors to force re-auth
            if _cp_client:
                try: await _cp_client.close()
                except Exception: pass
                _cp_client = None
            if "Failed to start charging" in err_msg or "422" in err_msg or "CommunicationError" in str(type(e)):
                raise ChargePointStartError(f"ChargePoint start rejected (422/CommunicationError): {err_msg}")
            raise

async def stop_charger_async():
    global _cp_client
    try:
        client = await get_cp_client()
        status = await client.get_user_charging_status()
        if not status or not getattr(status, "session_id", None):
            log_chargepoint.info("No active charging session found to stop (already idle).")
            return

        session_id = status.session_id
        # Send stop command directly
        req = {
            "deviceId": config.CHARGEPOINT_DEVICE_ID,
            "portNumber": 1,
            "sessionId": session_id
        }
        stop_url = client.global_config.endpoints.accounts_endpoint / "v1/driver/station/stopSession"
        resp = await client._request("POST", stop_url, json=req)
        
        if resp.status == 200:
            data = await resp.json()
            ack_id = data.get("ackId")
            if ack_id:
                ack_url = client.global_config.endpoints.accounts_endpoint / "v1/driver/station/session/ack"
                ack_resp = await client._request("POST", ack_url, json={"ackId": ack_id, "action": "stop_session"})
                if ack_resp.status == 200:
                    log_chargepoint.info(f"Session STOPPED and confirmed | session_id={session_id}")
                    return
                else:
                    try:
                        ack_data = await ack_resp.json(content_type=None) or {}
                    except Exception:
                        ack_data = {}
                    err_msg = ack_data.get("errorMessage", "")
                    log_chargepoint.info(f"Session stop acknowledged (session already stopped/finalized by vehicle): status={ack_resp.status} msg={err_msg}")
                    return
            log_chargepoint.info(f"Stop command accepted for session_id={session_id}")
            return
        else:
            txt = await resp.text()
            log_chargepoint.warning(f"Stop command returned status={resp.status}: {txt[:100]}")
    except Exception as e:
        err_clean = _clean_error_str(e)
        if "422" in err_clean or "Failed to stop session" in err_clean:
            log_chargepoint.info(f"Charger session already stopped or finalized: {err_clean}")
            return
        log_chargepoint.warning(f"Error executing stop command: {err_clean}")
        if _cp_client:
            try: await _cp_client.close()
            except Exception: pass
            _cp_client = None

async def get_charger_status_async() -> dict:
    global _cp_client
    try:
        client = await get_cp_client()
        s = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
        
        # Fallback check: query active session status to bypass ChargePoint API synchronization lag
        user_status = await client.get_user_charging_status()
        is_charging = (s.charging_status == "CHARGING") or (user_status is not None)
        
        energy_kwh = 0.0
        power_kw = 0.0
        miles_added = 0.0
        charging_time_seconds = 0
        session_start_time = None
        
        if user_status and getattr(user_status, "session_id", None):
            try:
                session = await client.get_charging_session(user_status.session_id)
                if session:
                    energy_kwh = float(getattr(session, "energy_kwh", 0.0) or 0.0)
                    power_kw = float(getattr(session, "power_kw", 0.0) or 0.0)
                    miles_added = float(getattr(session, "miles_added", 0.0) or 0.0)
                    raw_time = int(getattr(session, "charging_time", 0) or 0)
                    charging_time_seconds = raw_time // 1000 if raw_time > 1000 else raw_time
                    if getattr(session, "start_time", None):
                        session_start_time = session.start_time.astimezone(config.TZ)
            except Exception as sess_err:
                log_chargepoint.warning(f"Error fetching active charging session details: {sess_err}")

        return {
            "charging_status": "CHARGING" if is_charging else s.charging_status,
            "is_plugged_in":   s.is_plugged_in,
            "is_connected":    s.is_connected,
            "amperage_limit":  s.amperage_limit,
            "energy_kwh":      energy_kwh,
            "power_kw":        power_kw,
            "miles_added":     miles_added,
            "charging_time_seconds": charging_time_seconds,
            "session_start_time": session_start_time,
        }
    except Exception as e:
        err_clean = _clean_error_str(e)
        log_chargepoint.warning(f"Error getting charger status, resetting client session: {err_clean}")
        if _cp_client:
            try: await _cp_client.close()
            except Exception: pass
            _cp_client = None
        raise

async def set_charger_amperage_limit_async(amperage: int):
    global _cp_client
    try:
        client = await get_cp_client()
        await client.set_amperage_limit(charger_id=config.CHARGEPOINT_DEVICE_ID, amperage_limit=amperage)
        log_chargepoint.info(f"Amperage limit set to {amperage}A")
    except Exception as e:
        if _cp_client:
            try: await _cp_client.close()
            except Exception: pass
            _cp_client = None
        raise

_loop = None
_thread = None

def _start_background_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def _run_sync(coro, timeout: float = 60.0):
    global _loop, _thread
    if _thread is None or not _thread.is_alive():
        _loop = None
        _thread = threading.Thread(target=_start_background_loop, daemon=True)
        _thread.start()
        while _loop is None or not _loop.is_running():
            import time
            time.sleep(0.01)
    
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except Exception as err:
        if "TimeoutError" in type(err).__name__:
            log_chargepoint.warning(f"ChargePoint API call timed out after {timeout}s")
        raise

def start_charger(amperage_limit: int = 20):  _run_sync(start_charger_async(amperage_limit))
def stop_charger():   _run_sync(stop_charger_async())
def get_charger_status() -> dict: return _run_sync(get_charger_status_async())
def set_charger_amperage_limit(amperage: int): _run_sync(set_charger_amperage_limit_async(amperage))
