import asyncio
import config
from python_chargepoint import ChargePoint, client, session

async def main():
    client = await ChargePoint.create(
    username=config.CHARGEPOINT_USERNAME,
    coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
    )
    
    charger_ids = await client.get_home_chargers()
    print("Home Charger IDs:", charger_ids)
    
    charger_id = charger_ids[0]
    
    # Get charger status, technical info, and config
    status = await client.get_home_charger_status(charger_id)
    print("Home Charger Status:", status)

    tech = await client.get_home_charger_technical_info(charger_id)
    print("Home Charger Technical Info:", tech)

    charger_config = await client.get_home_charger_config(charger_id)
    print("Home Charger Config:", charger_config)

    # print(status.possible_amperage_limits)

    # Get user charging status and session details
    status = await client.get_user_charging_status()
    print("User Charging Status:", status)
    if status:
        print(status.state)       # "fully_charged"
        print(status.session_id)  # 1234567890

        session = await client.get_charging_session(status.session_id)
        print(session.charging_state)  # "fully_charged"
        print(session.energy_kwh)      # 6.42
        print(session.miles_added)     # 22.3

        # Stop the current session
        # session = await client.get_charging_session(status.session_id)
        # await session.stop()

    # Start a new session on any device
    # new_session = await client.start_charging_session(device_id=charger_id)
    # print(new_session.session_id)

    await client.close()

asyncio.run(main())