import asyncio
import config
from logger import log_chargepoint

_cp_client = None

async def get_cp_client():
    global _cp_client
    if _cp_client is None:
        from python_chargepoint import ChargePoint
        log_chargepoint.info("Initializing new ChargePoint client session")
        _cp_client = await ChargePoint.create(
            username=config.CHARGEPOINT_USERNAME,
            coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
        )
    return _cp_client

async def start_charger_async():
    global _cp_client
    try:
        client = await get_cp_client()
        session = await client.start_charging_session(device_id=config.CHARGEPOINT_DEVICE_ID)
        log_chargepoint.info(f"Session STARTED | session_id={session.session_id}")
    except Exception as e:
        if "ValidationError" in str(type(e)):
            log_chargepoint.warning("Session started, but ChargePoint returned empty status right away (eventual consistency bug in library). Ignoring.")
        else:
            # Reset client on general errors to force re-auth
            if _cp_client:
                try: await _cp_client.close()
                except Exception: pass
                _cp_client = None
            raise

async def stop_charger_async():
    global _cp_client
    max_retries = 3
    retry_delay_seconds = 60
    
    for attempt in range(1, max_retries + 1):
        try:
            client = await get_cp_client()
            status = await client.get_user_charging_status()
            if status:
                session = await client.get_charging_session(status.session_id)
                await session.stop()
                log_chargepoint.info(f"Session STOPPED via lookup | session_id={status.session_id}")
                return
            else:
                log_chargepoint.warning("No active session found to stop")
                return
        except Exception as e:
            log_chargepoint.warning(f"Stop attempt {attempt}/{max_retries} failed: {e}")
            if _cp_client:
                try: await _cp_client.close()
                except Exception: pass
                _cp_client = None
            if attempt < max_retries:
                log_chargepoint.info(f"Waiting {retry_delay_seconds}s before retrying stop...")
                await asyncio.sleep(retry_delay_seconds)
            else:
                raise

async def get_charger_status_async() -> dict:
    global _cp_client
    try:
        client = await get_cp_client()
        s = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
        
        # Fallback check: query active session status to bypass ChargePoint API synchronization lag
        user_status = await client.get_user_charging_status()
        is_charging = (s.charging_status == "CHARGING") or (user_status is not None)
        
        return {
            "charging_status": "CHARGING" if is_charging else s.charging_status,
            "is_plugged_in":   s.is_plugged_in,
            "is_connected":    s.is_connected,
            "amperage_limit":  s.amperage_limit,
        }
    except Exception as e:
        log_chargepoint.warning(f"Error getting charger status, resetting client session: {e}")
        if _cp_client:
            try: await _cp_client.close()
            except Exception: pass
            _cp_client = None
        raise

def start_charger():  asyncio.run(start_charger_async())
def stop_charger():   asyncio.run(stop_charger_async())
def get_charger_status() -> dict: return asyncio.run(get_charger_status_async())
