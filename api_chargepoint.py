import asyncio
import config
from logger import log_chargepoint

async def get_cp_client():
    from python_chargepoint import ChargePoint
    client = await ChargePoint.create(
        username=config.CHARGEPOINT_USERNAME,
        coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
    )
    return client

async def start_charger_async():
    client = await get_cp_client()
    try:
        session = await client.start_charging_session(device_id=config.CHARGEPOINT_DEVICE_ID)
        log_chargepoint.info(f"Session STARTED | session_id={session.session_id}")
    except Exception as e:
        if "ValidationError" in str(type(e)):
            log_chargepoint.warning("Session started, but ChargePoint returned empty status right away (eventual consistency bug in library). Ignoring.")
        else:
            raise
    finally:
        await client.close()

async def stop_charger_async():
    client = await get_cp_client()
    try:
        status = await client.get_user_charging_status()
        if status:
            session = await client.get_charging_session(status.session_id)
            await session.stop()
            log_chargepoint.info(f"Session STOPPED via lookup | session_id={status.session_id}")
        else:
            log_chargepoint.warning("No active session found to stop")
    except Exception as e:
        log_chargepoint.warning(f"Stop failed: {e}")
    finally:
        await client.close()

async def get_charger_status_async() -> dict:
    client = await get_cp_client()
    try:
        s = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
        return {
            "charging_status": s.charging_status,
            "is_plugged_in":   s.is_plugged_in,
            "is_connected":    s.is_connected,
            "amperage_limit":  s.amperage_limit,
        }
    finally:
        await client.close()

def start_charger():  asyncio.run(start_charger_async())
def stop_charger():   asyncio.run(stop_charger_async())
def get_charger_status() -> dict: return asyncio.run(get_charger_status_async())
