"""Direct branch coverage for core.decision.evaluate().

Unlike the control-loop tests, nothing is stubbed here: the real
is_in_night_blackout runs, driven by config, including the midnight wrap.
"""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)
import unittest
from datetime import datetime, timedelta

from core import config, state
from core.decision import evaluate, is_in_charge_window

# A plain Wednesday, well clear of weekends and utility holidays.
WEDNESDAY_NOON = datetime(2026, 8, 26, 12, 0, tzinfo=config.TZ)
WEDNESDAY_23H = datetime(2026, 8, 26, 23, 0, tzinfo=config.TZ)
SATURDAY_23H = datetime(2026, 8, 29, 23, 0, tzinfo=config.TZ)


def stats(battery_pct=50.0, plugged_in=True, solar_kw=4.0, surplus_kw=3.0, goal_night_kwh=0.0):
    """Live readings; defaults to a sunny noon with 3 kW of solar left over after the house."""
    return {"battery_pct": battery_pct, "is_plugged_in": plugged_in, "solar_kw": solar_kw,
            "solar_surplus_kw": surplus_kw, "goal_night_kwh": goal_night_kwh}


class DecisionCase(unittest.TestCase):
    def setUp(self):
        state.reset_for_tests()
        self.addCleanup(state.reset_for_tests)
        config._apply({key: default for key, (_, default) in config.DYNAMIC_CONFIG_SCHEMA.items()})
        self.addCleanup(config._apply,
                        {key: default for key, (_, default) in config.DYNAMIC_CONFIG_SCHEMA.items()})
        # Neutral baseline: no blackout, full-day charge window.
        config.NIGHT_BLACKOUT_START_HOUR = config.NIGHT_BLACKOUT_END_HOUR = 0
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 0, 24
        config.BATTERY_START_PCT, config.BATTERY_STOP_PCT = 40.0, 25.0
        config.BATTERY_LOW_RESERVE_PCT = 15.0

    def charging(self, minutes_ago=45):
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ) - timedelta(minutes=minutes_ago)


class TestUnplugged(DecisionCase):
    def test_unplugged_while_charging_stops(self):
        self.charging()
        action, reason = evaluate(stats(80.0, plugged_in=False), WEDNESDAY_NOON)
        self.assertEqual(action, "stop")
        self.assertIn("unplugged", reason.lower())

    def test_unplugged_while_idle_holds(self):
        action, _ = evaluate(stats(80.0, plugged_in=False), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")


class TestNightBlackout(DecisionCase):
    """Exercises the REAL blackout window, including the midnight wrap."""

    def setUp(self):
        super().setUp()
        config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR = 22, 6

    def test_weekday_night_stops_an_active_charge(self):
        self.charging()
        action, reason = evaluate(stats(80.0), WEDNESDAY_23H)
        self.assertEqual(action, "stop")
        self.assertIn("blackout", reason.lower())

    def test_weekday_night_blocks_a_start(self):
        action, _ = evaluate(stats(80.0), WEDNESDAY_23H)
        self.assertEqual(action, "blackout")

    def test_wrap_covers_early_morning(self):
        three_am = WEDNESDAY_23H.replace(hour=3)
        action, _ = evaluate(stats(80.0), three_am)
        self.assertEqual(action, "blackout")

    def test_daytime_is_outside_the_wrapped_window(self):
        action, _ = evaluate(stats(80.0), WEDNESDAY_NOON)
        self.assertEqual(action, "start")

    def test_weekend_night_is_exempt(self):
        action, _ = evaluate(stats(80.0), SATURDAY_23H)
        self.assertEqual(action, "start")


class TestBatteryThresholds(DecisionCase):
    def test_critical_low_reserve_stops_even_below_min_session(self):
        self.charging(minutes_ago=2)
        action, reason = evaluate(stats(10.0), WEDNESDAY_NOON)
        self.assertEqual(action, "stop")
        self.assertIn("Critical", reason)

    def test_below_stop_threshold_respects_min_session(self):
        self.charging(minutes_ago=2)
        action, reason = evaluate(stats(20.0), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")
        self.assertIn("min", reason)

    def test_below_stop_threshold_stops_after_min_session(self):
        self.charging(minutes_ago=45)
        action, _ = evaluate(stats(20.0), WEDNESDAY_NOON)
        self.assertEqual(action, "stop")

    def test_healthy_battery_starts_when_idle(self):
        action, _ = evaluate(stats(50.0), WEDNESDAY_NOON)
        self.assertEqual(action, "start")

    def test_healthy_battery_holds_when_already_charging(self):
        self.charging()
        action, _ = evaluate(stats(50.0), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")

    def test_hysteresis_band_continues_charging_but_never_starts(self):
        # 30% is above stop (25) but below start (40).
        self.charging()
        action, reason = evaluate(stats(30.0), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")
        self.assertIn("Charging (battery 30.0%", reason)

        state.reset_for_tests()
        action, reason = evaluate(stats(30.0), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")
        self.assertIn("Idle", reason)


class TestBatteryAndWindowOnly(DecisionCase):
    """Inside the window only the Powerwall % starts and stops charging; solar readings are ignored."""

    def setUp(self):
        super().setUp()
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 10, 15

    def test_starts_on_battery_even_with_no_solar(self):
        action, reason = evaluate(stats(45.0, solar_kw=0.0, surplus_kw=-1.0), WEDNESDAY_NOON)
        self.assertEqual(action, "start")
        self.assertIn("within charge window", reason)

    def test_a_solar_dip_does_not_stop_a_charge(self):
        self.charging()
        action, _ = evaluate(stats(35.0, solar_kw=0.1, surplus_kw=-4.6), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")

    def test_big_surplus_does_not_start_below_start_pct(self):
        action, _ = evaluate(stats(30.0, solar_kw=8.0, surplus_kw=7.0), WEDNESDAY_NOON)
        self.assertEqual(action, "hold")

    def test_window_end_stops_a_charge(self):
        self.charging()
        action, reason = evaluate(stats(80.0), WEDNESDAY_NOON.replace(hour=15))
        self.assertEqual(action, "stop")
        self.assertIn("Outside", reason)


class TestMilesGoalNightCharging(DecisionCase):
    """A goal that solar can't cover charges at the day's cheapest rate, even outside the window."""

    def setUp(self):
        super().setUp()
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 10, 15

    def test_goal_starts_outside_the_window_at_the_cheapest_rate(self):
        action, reason = evaluate(stats(30.0, solar_kw=0.0, surplus_kw=-0.4, goal_night_kwh=12.0), WEDNESDAY_23H)
        self.assertEqual(action, "start")
        self.assertIn("Miles goal", reason)

    def test_goal_keeps_charging_overnight(self):
        self.charging()
        action, reason = evaluate(stats(60.0, solar_kw=0.0, surplus_kw=-5.2, goal_night_kwh=12.0), WEDNESDAY_23H)
        self.assertEqual(action, "hold")
        self.assertIn("Miles goal", reason)

    def test_goal_never_charges_on_peak(self):
        action, _ = evaluate(stats(60.0, solar_kw=0.0, surplus_kw=-0.4, goal_night_kwh=12.0),
                             WEDNESDAY_NOON.replace(hour=18))
        self.assertEqual(action, "hold")

    def test_goal_stops_when_the_powerwall_drains_to_stop_pct(self):
        self.charging()
        action, _ = evaluate(stats(20.0, solar_kw=0.0, surplus_kw=-5.2, goal_night_kwh=12.0), WEDNESDAY_23H)
        self.assertEqual(action, "stop")

    def test_no_goal_no_night_charging(self):
        action, _ = evaluate(stats(90.0, solar_kw=0.0, surplus_kw=-0.4), WEDNESDAY_23H)
        self.assertEqual(action, "hold")


class TestChargeWindow(DecisionCase):
    def test_outside_window_blocks_a_start(self):
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 10, 16
        action, reason = evaluate(stats(80.0), WEDNESDAY_NOON.replace(hour=8))
        self.assertEqual(action, "hold")
        self.assertIn("Outside", reason)

    def test_outside_window_stops_after_min_session(self):
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 10, 16
        self.charging(minutes_ago=45)
        action, _ = evaluate(stats(80.0), WEDNESDAY_NOON.replace(hour=17))
        self.assertEqual(action, "stop")

    def test_wrapped_window_spans_midnight(self):
        self.assertTrue(is_in_charge_window(23, 22, 6))
        self.assertTrue(is_in_charge_window(3, 22, 6))
        self.assertFalse(is_in_charge_window(12, 22, 6))
        self.assertTrue(is_in_charge_window(12, 0, 24), "0-24 means always")


class TestPurity(DecisionCase):
    def test_stop_decisions_do_not_mutate_state(self):
        self.charging(minutes_ago=45)
        before = state.snapshot()
        action, _ = evaluate(stats(20.0), WEDNESDAY_NOON)
        self.assertEqual(action, "stop")
        self.assertEqual(state.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
