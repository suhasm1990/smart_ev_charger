import asyncio
import state
import config
from logger import log_chargepoint

async def get_cp_client():
    if state.cp_client is None:
        from python_chargepoint import ChargePoint
        state.cp_client = await ChargePoint.create(
            username=config.CHARGEPOINT_USERNAME,
            coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
        )
        log_chargepoint.info("Client created successfully")
    return state.cp_client

async def start_charger_async():
    client = await get_cp_client()
    state.active_session = await client.start_charging_session(device_id=config.CHARGEPOINT_DEVICE_ID)
    log_chargepoint.info(f"Session STARTED | session_id={state.active_session.session_id}")

async def stop_charger_async():
    client = await get_cp_client()
    if state.active_session:
        try:
            await state.active_session.stop()
            log_chargepoint.info(f"Session STOPPED | session_id={state.active_session.session_id}")
            state.active_session = None
            return
        except Exception as e:
            log_chargepoint.warning(f"Cached session stop failed ({e}) — trying live lookup")

    status = await client.get_user_charging_status()
    if status:
        session = await client.get_charging_session(status.session_id)
        await session.stop()
        log_chargepoint.info(f"Session STOPPED via lookup | session_id={status.session_id}")
        state.active_session = None
    else:
        log_chargepoint.warning("No active session found to stop")

async def get_charger_status_async() -> dict:
    client = await get_cp_client()
    s = await client.get_home_charger_status(config.CHARGEPOINT_DEVICE_ID)
    return {
        "charging_status": s.charging_status,
        "is_plugged_in":   s.is_plugged_in,
        "is_connected":    s.is_connected,
        "amperage_limit":  s.amperage_limit,
    }

def start_charger():  asyncio.run(start_charger_async())
def stop_charger():   asyncio.run(stop_charger_async())
def get_charger_status() -> dict: return asyncio.run(get_charger_status_async())
