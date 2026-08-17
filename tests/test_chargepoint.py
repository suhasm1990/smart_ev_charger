import os
import sys
import asyncio

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config

async def test_chargepoint_connection():
    if not config.CHARGEPOINT_USERNAME or not config.CHARGEPOINT_COULOMB_TOKEN:
        print("ChargePoint credentials not set. Skipping live integration test.")
        return
        
    from python_chargepoint import ChargePoint
    client = await ChargePoint.create(
        username=config.CHARGEPOINT_USERNAME,
        coulomb_token=config.CHARGEPOINT_COULOMB_TOKEN,
    )
    
    charger_ids = await client.get_home_chargers()
    print("Home Charger IDs:", charger_ids)
    
    if charger_ids:
        charger_id = charger_ids[0]
        status = await client.get_home_charger_status(charger_id)
        print("Home Charger Status:", status)
    
    await client.close()
    print("✅ ChargePoint connection test completed!")

if __name__ == "__main__":
    asyncio.run(test_chargepoint_connection())
