import os
import sys
import unittest
from datetime import datetime, date, timedelta

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config, state, evaluate, check_manual_mode, get_tou_period, get_tou_rate, is_in_night_blackout, is_weekend
from reporting.csv_logger import (
    get_session_minutes, _resolve_date_range, _is_ev_charging_row,
    get_daily_charging_cost, get_home_energy_summary, get_energy_saving_advice, get_monthly_billing_data
)
from reporting.report_generator import generate_monthly_report_image
from agent.alerts import add_alert, remove_alert, list_alerts, check_alerts, check_recent_log_errors
from agent import llm_client, telegram_bot
import services.chargepoint
import services.netzero

class TestFullSystem(unittest.TestCase):

    def setUp(self):
        # Mock external APIs and notifications
        services.chargepoint.start_charger = lambda amp=20: None
        services.chargepoint.stop_charger = lambda: None
        services.chargepoint.set_charger_amperage_limit = lambda amp: None
        telegram_bot.start_charger = lambda amp=20: None
        telegram_bot.stop_charger = lambda: None
        telegram_bot.set_charger_amperage_limit = lambda amp: None
        telegram_bot.notify = lambda msg: None
        
        # Reset runtime state
        state.charger_state = state.State.IDLE
        state.charge_session_start = None
        state.session_stop_reason = None
        state.manual_mode = False
        state.manual_mode_set_at = None
        state.prev_manual_mode = False
        state.clear_manual_guards()
        config.MANUAL_MODE_OVERRIDE = "auto"

    def tearDown(self):
        config.MANUAL_MODE_OVERRIDE = "auto"
        config.save_dynamic_config()
        state.clear_manual_guards()
        state.charger_state = state.State.IDLE
        state.charge_session_start = None

    def test_imports_and_no_circular_dependencies(self):
        """Ensures all top-level package modules import cleanly without circular dependency errors."""
        import core
        import services
        import reporting
        import agent
        import main
        self.assertIsNotNone(core)
        self.assertIsNotNone(services)
        self.assertIsNotNone(reporting)
        self.assertIsNotNone(agent)
        self.assertIsNotNone(main)

    def test_date_range_resolution(self):
        """Tests natural language date and period parsing."""
        now = datetime(2026, 8, 19, 12, 0, tzinfo=config.TZ)
        
        # Today
        s, e, lbl = _resolve_date_range("today", now)
        self.assertEqual(s, date(2026, 8, 19))
        self.assertEqual(e, date(2026, 8, 19))
        
        # Yesterday
        s, e, lbl = _resolve_date_range("yesterday", now)
        self.assertEqual(s, date(2026, 8, 18))
        self.assertEqual(e, date(2026, 8, 18))
        
        # Past 7 Days
        s, e, lbl = _resolve_date_range("last 7 days", now)
        self.assertEqual(s, date(2026, 8, 12))
        self.assertEqual(e, date(2026, 8, 19))
        
        # Last Month
        s, e, lbl = _resolve_date_range("last_month", now)
        self.assertEqual(s, date(2026, 7, 1))
        self.assertEqual(e, date(2026, 7, 31))
        
        # Specific month string
        s, e, lbl = _resolve_date_range("July 2026", now)
        self.assertEqual(s, date(2026, 7, 1))
        self.assertEqual(e, date(2026, 7, 31))

        # YYYY-MM
        s, e, lbl = _resolve_date_range("2026-06", now)
        self.assertEqual(s, date(2026, 6, 1))
        self.assertEqual(e, date(2026, 6, 30))

    def test_ev_charging_isolation(self):
        """Verifies that high home usage (e.g. AC 4.5kW) while charger is IDLE is not falsely classified as EV charging."""
        non_ev_row = {
            "charger_state": "IDLE",
            "action": "hold",
            "home_kw": 4.5,
            "grid_kw": 2.0
        }
        self.assertFalse(_is_ev_charging_row(non_ev_row))

        ev_row_charging = {
            "charger_state": "CHARGING",
            "action": "hold",
            "home_kw": 4.8,
            "grid_kw": 0.0
        }
        self.assertTrue(_is_ev_charging_row(ev_row_charging))

        ev_row_start = {
            "charger_state": "IDLE",
            "action": "start",
            "home_kw": 1.0,
            "grid_kw": 0.0
        }
        self.assertTrue(_is_ev_charging_row(ev_row_start))

    def test_energy_analytics_and_report_generation(self):
        """Tests calculation of energy summaries and PNG infographic generation."""
        summary = get_home_energy_summary("last_month")
        self.assertNotIn("error", summary)
        self.assertIn("total_home_consumption_kwh", summary)
        self.assertIn("estimated_total_mid_utility_bill_dollars", summary)

        monthly_data = get_monthly_billing_data("last_month")
        self.assertNotIn("error", monthly_data)
        self.assertIn("daily_records", monthly_data)

        # Generate actual PNG report
        img_path = generate_monthly_report_image("last_month")
        self.assertTrue(os.path.exists(img_path))
        self.assertTrue(os.path.getsize(img_path) > 10000)

    def test_custom_alerts_crud_and_evaluation(self):
        """Tests adding, listing, matching, and removing custom dynamic alerts."""
        res_add = add_alert(field="battery_pct", operator="gte", value=80.0, message="Battery reached 80%", once=True)
        self.assertIn("Success", res_add)
        
        alerts_text = list_alerts()
        self.assertIn("battery_pct", alerts_text)
        
        # Test alert match
        mock_notified = []
        import agent.alerts
        agent.alerts.notify = lambda msg: mock_notified.append(msg)
        
        check_alerts({"battery_pct": 85.0})
        self.assertTrue(any("Battery reached 80%" in m for m in mock_notified))

    def test_telegram_bot_tools_and_type_casting(self):
        """Tests all bot tools with string and numeric parameters for robustness."""
        res_thresh = telegram_bot.set_battery_thresholds(start_pct="45.5", stop_pct="22.0")
        self.assertIn("Success", res_thresh)
        self.assertEqual(config.BATTERY_START_PCT, 45.5)
        self.assertEqual(config.BATTERY_STOP_PCT, 22.0)

        res_blackout = telegram_bot.set_blackout_hours(start_hour="17", end_hour="8")
        self.assertIn("Success", res_blackout)
        self.assertEqual(config.NIGHT_BLACKOUT_START_HOUR, 17)
        self.assertEqual(config.NIGHT_BLACKOUT_END_HOUR, 8)

        res_amp = telegram_bot.set_charger_amperage(amperage="24")
        self.assertIn("Success", res_amp)

        res_start = telegram_bot.start_charging(amperage="32", stop_battery_pct="30.0", stop_at_hour="16", duration_hours="1.5")
        self.assertIn("Success", res_start)
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(state.manual_guard_stop_battery_pct, 30.0)
        self.assertEqual(state.manual_guard_stop_at_hour, 16)
        self.assertIsNotNone(state.manual_guard_stop_time)

        res_stop = telegram_bot.stop_charging()
        self.assertIn("Success", res_stop)
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.manual_guard_stop_battery_pct)

    def test_llm_tool_schema_and_signature_filtering(self):
        """Tests LLM tool conversion and safe arg filtering."""
        schema = llm_client.function_to_openai_tool(telegram_bot.start_charging)
        props = schema["function"]["parameters"]["properties"]
        self.assertEqual(props["amperage"]["type"], "integer")
        self.assertEqual(props["stop_battery_pct"]["type"], "number")
        self.assertEqual(props["stop_at_hour"]["type"], "integer")
        self.assertEqual(props["duration_hours"]["type"], "number")

if __name__ == "__main__":
    unittest.main()
