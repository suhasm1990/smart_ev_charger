import unittest
from datetime import datetime

import main
from agent import llm_client, telegram_bot
from core import config, state
from tests.helpers import MockedCycle, charger, frozen_now, powerwall


class TestManualGuardrails(unittest.TestCase):
    def setUp(self):
        self.mock = MockedCycle()
        self.mock.reset_state()
        telegram_bot.RUN_CYCLE_CALLBACK = None
        self.addCleanup(self.mock.restore)
        self.mock.install_bot()

    def test_start_charging_records_every_guardrail(self):
        result = telegram_bot.start_charging(
            amperage=32, stop_battery_pct=30.0, stop_at_hour=16, duration_hours=2.0)
        self.assertIn("Success", result)
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(state.manual_guard_stop_battery_pct, 30.0)
        self.assertEqual(state.manual_guard_stop_at_hour, 16)
        self.assertIsNotNone(state.manual_guard_stop_time)
        self.assertEqual(state.active_amperage, 32)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "manual")

    def test_stop_charging_clears_guardrails(self):
        telegram_bot.start_charging(amperage=32, stop_battery_pct=30.0, stop_at_hour=16, duration_hours=2.0)
        self.assertIn("Success", telegram_bot.stop_charging())
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.manual_guard_stop_battery_pct)
        self.assertIsNone(state.manual_guard_stop_at_hour)
        self.assertIsNone(state.manual_guard_stop_time)

    def test_returning_to_auto_clears_guardrails(self):
        telegram_bot.start_charging(amperage=32, stop_battery_pct=35.0)
        self.assertEqual(state.manual_guard_stop_battery_pct, 35.0)
        telegram_bot.set_override_mode("auto")
        self.assertIsNone(state.manual_guard_stop_battery_pct)
        self.assertEqual(state.active_amperage, config.DEFAULT_CHARGER_AMPERAGE)

    def test_battery_guard_stops_and_restores_auto(self):
        self.mock.install(powerwall(battery_pct=28.0), charger(charging=True, amperage=32))
        self.mock.enter_manual()
        state.manual_guard_stop_battery_pct = 30.0

        main.run_cycle()

        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "auto")
        self.assertIsNone(state.manual_guard_stop_battery_pct)
        self.assertIn(("stop", None), self.mock.calls)
        self.assertIn(("amperage", config.DEFAULT_CHARGER_AMPERAGE), self.mock.calls)

    def test_hour_cutoff_guard_stops_and_restores_auto(self):
        self.mock.install(powerwall(battery_pct=50.0), charger(charging=True, amperage=32))
        self.mock.enter_manual()
        state.manual_guard_stop_at_hour = 16

        original = main.datetime
        main.datetime = frozen_now(datetime(2026, 8, 17, 16, 5, tzinfo=config.TZ))
        try:
            main.run_cycle()
        finally:
            main.datetime = original

        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "auto")
        self.assertIsNone(state.manual_guard_stop_at_hour)

    def test_low_battery_does_not_stop_an_unguarded_manual_charge(self):
        """Manual mode is deliberate: without an explicit guard, keep charging.

        The automatic low-reserve rule must not override a manual session, or a
        'charge anyway' instruction would be silently cancelled.
        """
        self.mock.install(powerwall(battery_pct=12.7, solar_kw=2.45, home_kw=0.19),
                          charger(charging=True, amperage=32))
        self.mock.enter_manual()
        state.manual_guard_stop_battery_pct = None

        main.run_cycle()

        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "manual")
        self.assertNotIn(("stop", None), self.mock.calls)

    def test_unplugging_ends_a_manual_charge(self):
        self.mock.install(powerwall(battery_pct=60.0), charger(charging=True, plugged_in=False))
        self.mock.enter_manual()
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "auto")

    def test_off_grid_ends_a_manual_charge(self):
        self.mock.install(powerwall(battery_pct=60.0, island_mode="off_grid"),
                          charger(charging=True))
        self.mock.enter_manual()
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertEqual(config.MANUAL_MODE_OVERRIDE, "auto")

    def test_amperage_is_clamped_to_the_supported_range(self):
        for requested, expected in ((32, 32), (8, 8), (99, config.DEFAULT_CHARGER_AMPERAGE),
                                    (0, config.DEFAULT_CHARGER_AMPERAGE),
                                    ("24", 24), ("bogus", config.DEFAULT_CHARGER_AMPERAGE)):
            self.assertEqual(telegram_bot._clamp_amperage(requested), expected, f"input {requested!r}")

    def test_tool_schema_exposes_guardrail_parameters(self):
        props = llm_client.function_to_openai_tool(telegram_bot.start_charging)["function"]["parameters"]["properties"]
        self.assertEqual(props["amperage"]["type"], "integer")
        self.assertEqual(props["stop_battery_pct"]["type"], "number")
        self.assertEqual(props["stop_at_hour"]["type"], "integer")
        self.assertEqual(props["duration_hours"]["type"], "number")


if __name__ == "__main__":
    unittest.main()
