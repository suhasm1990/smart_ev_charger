import os
import sys
from datetime import datetime, timedelta

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config, state
from core.manual_override import check_manual_mode
from agent import telegram_bot, llm_client
import services.chargepoint
from reporting import notifications

def test_guardrails_unit():
    print("--- 1. Testing start_charging guardrails setup ---")
    # Mock chargepoint and notifications
    services.chargepoint.start_charger = lambda amp=20: None
    services.chargepoint.stop_charger = lambda: None
    telegram_bot.start_charger = lambda amp=20: None
    telegram_bot.stop_charger = lambda: None
    telegram_bot.notify = lambda msg: print(f"[MOCK NOTIFY] {msg[:40]}...")
    notifications.notify = lambda msg: print(f"[MOCK NOTIFY] {msg[:40]}...")

    state.clear_manual_guards()
    res = telegram_bot.start_charging(amperage=32, stop_battery_pct=30.0, stop_at_hour=16, duration_hours=2.0)
    print("start_charging result:", res)
    assert state.charger_state == state.State.CHARGING
    assert state.manual_guard_stop_battery_pct == 30.0
    assert state.manual_guard_stop_at_hour == 16
    assert state.manual_guard_stop_time is not None
    assert config.MANUAL_MODE_OVERRIDE == "manual"
    print("✅ Setup assertions passed!")

    print("\n--- 2. Testing stop_charging guardrails cleanup ---")
    res_stop = telegram_bot.stop_charging()
    print("stop_charging result:", res_stop)
    assert state.charger_state == state.State.IDLE
    assert state.manual_guard_stop_battery_pct is None
    assert state.manual_guard_stop_at_hour is None
    assert state.manual_guard_stop_time is None
    print("✅ Cleanup assertions passed!")

    print("\n--- 3. Testing manual_override reset clears guards ---")
    telegram_bot.start_charging(amperage=32, stop_battery_pct=35.0)
    assert state.manual_guard_stop_battery_pct == 35.0
    telegram_bot.set_override_mode("auto")
    assert state.manual_guard_stop_battery_pct is None
    print("✅ Auto-reset assertions passed!")

def test_cycle_guardrails():
    print("\n--- 4. Testing main.py cycle guardrail enforcement ---")
    import main
    telegram_bot.RUN_CYCLE_CALLBACK = None
    main.stop_charger = lambda: None
    main.notify = lambda msg: print(f"[MOCK NOTIFY MAIN] {msg[:40]}...")
    
    # 4A: Test Battery Guardrail Trigger
    telegram_bot.start_charging(amperage=32, stop_battery_pct=30.0)
    assert config.MANUAL_MODE_OVERRIDE == "manual"
    assert state.manual_guard_stop_battery_pct == 30.0
    
    main.get_powerwall_stats = lambda: {
        "battery_pct": 28.0,
        "solar_kw": 1.0,
        "solar_surplus_kw": 0.5,
        "home_kw": 0.5,
        "grid_kw": 0.0,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "CHARGING",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 32
    }
    
    main.run_cycle()
    
    assert state.charger_state == state.State.IDLE
    assert config.MANUAL_MODE_OVERRIDE == "auto"
    assert state.manual_guard_stop_battery_pct is None
    print("✅ Battery guardrail triggered stop and restored auto mode successfully!")

    # 4B: Test Hour Cutoff Guardrail Trigger
    telegram_bot.start_charging(amperage=32, stop_at_hour=16)
    assert config.MANUAL_MODE_OVERRIDE == "manual"
    assert state.manual_guard_stop_at_hour == 16
    
    main.get_powerwall_stats = lambda: {
        "battery_pct": 50.0,
        "solar_kw": 1.0,
        "solar_surplus_kw": 0.5,
        "home_kw": 0.5,
        "grid_kw": 0.0,
        "grid_export_kw": 0.0,
        "island_mode": "on_grid",
        "storm_mode": False
    }
    main.get_charger_status = lambda: {
        "charging_status": "CHARGING",
        "is_plugged_in": True,
        "is_connected": True,
        "amperage_limit": 32
    }
    
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 17, 16, 5, tzinfo=config.TZ)
            
    old_datetime = main.datetime
    main.datetime = MockDatetime
    try:
        main.run_cycle()
        assert state.charger_state == state.State.IDLE
        assert config.MANUAL_MODE_OVERRIDE == "auto"
        assert state.manual_guard_stop_at_hour is None
        print("✅ Hour cutoff guardrail triggered stop and restored auto mode successfully!")
    finally:
        main.datetime = old_datetime

def test_ai_schema():
    print("\n--- 5. Testing LLM tool schema conversion ---")
    schema = llm_client.function_to_openai_tool(telegram_bot.start_charging)
    props = schema["function"]["parameters"]["properties"]
    assert "amperage" in props
    assert "stop_battery_pct" in props
    assert "stop_at_hour" in props
    assert "duration_hours" in props
    print("✅ Tool schema assertions passed!")

if __name__ == "__main__":
    test_guardrails_unit()
    test_cycle_guardrails()
    test_ai_schema()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")
