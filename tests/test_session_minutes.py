import os
import sys
from datetime import datetime, timedelta

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config, state
from reporting.csv_logger import get_session_minutes, log_to_csv
import services.chargepoint
import main

def test_session_minutes_unit():
    print("--- 1. Testing get_session_minutes unit behavior ---")
    state.charger_state = state.State.IDLE
    state.charge_session_start = None
    assert get_session_minutes() == 0.0, "Should be 0.0 when start time is None"

    # Set start time 30 mins ago while charging
    now = datetime.now(config.TZ)
    state.charge_session_start = now - timedelta(minutes=30)
    state.charger_state = state.State.CHARGING
    assert abs(get_session_minutes() - 30.0) < 0.2, "Should return ~30.0 minutes"
    print("✅ Unit get_session_minutes tests passed!")

def test_session_lifecycle_in_cycle():
    print("\n--- 2. Testing session lifecycle during run_cycle ---")
    # Mock external calls
    main.stop_charger = lambda: None
    main.start_charger = lambda amp=20: None
    main.notify = lambda msg: None
    
    # Mock sheets to avoid slow network during tests
    import services.sheets_db
    services.sheets_db.append_log_row = lambda row: None
    config.MANUAL_MODE_OVERRIDE = "auto"
    state.clear_manual_guards()
    state.manual_mode = False
    state.prev_manual_mode = False
    config.load_dynamic_config = lambda: None

    now = datetime.now(config.TZ)
    
    # Simulate active charging
    state.charger_state = state.State.CHARGING
    state.charge_session_start = now - timedelta(minutes=45)
    
    main.get_powerwall_stats = lambda: {
        "battery_pct": 24.0,  # Below stop threshold (25%)
        "solar_kw": 1.0,
        "solar_surplus_kw": -0.5,
        "home_kw": 1.5,
        "grid_kw": 0.5,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "CHARGING",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 20,
        "session_start_time": now - timedelta(minutes=45)
    }

    # Run cycle which should trigger STOP
    main.run_cycle()

    assert state.charger_state == state.State.IDLE, "State should be IDLE after stop"
    assert state.charge_session_start is None, "Session start must be cleared to None after stop"
    assert get_session_minutes() == 0.0, "Session minutes should be 0.0 after stop"
    print("✅ Stop session cleared charge_session_start successfully!")

    # Simulate next cycle while IDLE
    main.get_powerwall_stats = lambda: {
        "battery_pct": 25.0,  # Below start threshold (50%)
        "solar_kw": 1.0,
        "solar_surplus_kw": 0.0,
        "home_kw": 1.0,
        "grid_kw": 0.0,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "AVAILABLE",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 20,
        "session_start_time": None
    }

    main.run_cycle()
    assert state.charger_state == state.State.IDLE
    assert state.charge_session_start is None
    assert get_session_minutes() == 0.0
    print("✅ Idle cycles maintain 0.0 session minutes!")

def test_physical_sync():
    print("\n--- 3. Testing physical charger status sync ---")
    import services.sheets_db
    services.sheets_db.append_log_row = lambda row: None

    now = datetime.now(config.TZ)
    # Case A: External charge start detected
    state.charger_state = state.State.IDLE
    state.charge_session_start = None
    config.ALLOWED_CHARGE_START_HOUR = 0
    config.ALLOWED_CHARGE_END_HOUR = 24

    now = datetime.now(config.TZ)
    main.get_powerwall_stats = lambda: {
        "battery_pct": 60.0,
        "solar_kw": 4.0,
        "solar_surplus_kw": 3.0,
        "home_kw": 1.0,
        "grid_kw": 0.0,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "CHARGING",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 20,
        "session_start_time": now - timedelta(minutes=15)
    }

    main.run_cycle()
    assert state.charger_state == state.State.CHARGING
    assert state.charge_session_start is not None
    assert abs(get_session_minutes() - 15.0) < 0.5, "Should adopt 15 min session start from ChargePoint"
    print("✅ Physical start synchronization passed!")

    # Case B: External charge stop detected
    main.get_powerwall_stats = lambda: {
        "battery_pct": 40.0,
        "solar_kw": 1.0,
        "solar_surplus_kw": 0.0,
        "home_kw": 1.0,
        "grid_kw": 0.0,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "AVAILABLE",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 20,
        "session_start_time": None
    }

    main.run_cycle()
    assert state.charger_state == state.State.IDLE
    assert state.charge_session_start is None
    assert get_session_minutes() == 0.0
    print("✅ Physical stop synchronization passed!")

if __name__ == "__main__":
    test_session_minutes_unit()
    test_session_lifecycle_in_cycle()
    test_physical_sync()
    print("\n🎉 ALL SESSION MINUTES TESTS PASSED!")
