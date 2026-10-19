"""Morning planner: prompt context, forecast scaling, and plan validation."""
import unittest
from datetime import datetime

from agent import daily_agent
from core import config

WEEKDAY = datetime(2026, 10, 19, 7, 0, tzinfo=config.TZ)   # Monday
SATURDAY = datetime(2026, 10, 17, 7, 0, tzinfo=config.TZ)

PROFILE = [
    {"hour": 9, "solar_kw": 2.0, "house_kw": 0.8},
    {"hour": 12, "solar_kw": 6.0, "house_kw": 1.0},
    {"hour": 18, "solar_kw": 0.0, "house_kw": 2.5},
]


class PlannerTestCase(unittest.TestCase):
    KEYS = ("NIGHT_BLACKOUT_START_HOUR", "NIGHT_BLACKOUT_END_HOUR", "BATTERY_START_PCT",
            "BATTERY_STOP_PCT", "BATTERY_LOW_RESERVE_PCT",
            "ALLOWED_CHARGE_START_HOUR", "ALLOWED_CHARGE_END_HOUR")

    def setUp(self):
        self._saved = {key: getattr(config, key) for key in self.KEYS}
        self._update = config.update
        config.update = lambda **changes: config._apply(changes)
        config._apply({"NIGHT_BLACKOUT_START_HOUR": 16, "NIGHT_BLACKOUT_END_HOUR": 9,
                       "BATTERY_LOW_RESERVE_PCT": 15.0})

    def tearDown(self):
        config.update = self._update
        config._apply(self._saved)


class TestChargeableHours(PlannerTestCase):
    def test_weekday_blackout_leaves_daytime_hours(self):
        self.assertEqual(daily_agent._chargeable_hours(WEEKDAY), list(range(9, 16)))

    def test_weekend_ignores_blackout(self):
        self.assertEqual(daily_agent._chargeable_hours(SATURDAY), list(range(24)))

    def test_ranges_merge_contiguous_hours(self):
        self.assertEqual(daily_agent._format_ranges([9, 10, 11, 14]), "09:00-12:00, 14:00-15:00")


class TestForecastScaling(unittest.TestCase):
    def test_hours_scale_by_irradiance_ratio_with_cap(self):
        forecast = {
            "radiation_wm2": {9: 100.0, 12: 900.0, 18: 0.0},
            "past_avg_radiation_wm2": {9: 200.0, 12: 500.0, 18: 0.0},
            "cloud_cover_pct": {9: 80, 12: 10, 18: 0},
        }
        solar = daily_agent._forecast_solar(PROFILE, forecast)
        self.assertEqual(solar[9], 1.0)    # half as bright
        self.assertEqual(solar[12], 9.0)   # 1.8x, capped at 1.5x
        self.assertEqual(solar[18], 0.0)   # dark hour uses the day ratio on 0 kW

    def test_prompt_marks_forecast_unavailable(self):
        advice = {"hourly_profile_7_days": PROFILE, "error": "No log data found yet."}
        prompt = daily_agent.build_prompt(WEEKDAY, advice, {}, None, "")
        self.assertIn("Solar forecast: unavailable", prompt)
        self.assertIn("evening history unavailable", prompt)
        self.assertIn("Yesterday's totals unavailable", prompt)
        self.assertIn("Monday 2026-10-19", prompt)


class TestApplyPlanValidation(PlannerTestCase):
    def test_equal_window_hours_fall_back_to_full_day(self):
        daily_agent._apply_plan({"charge_window_start_hour": 10, "charge_window_end_hour": 10}, WEEKDAY)
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR), (0, 24))

    def test_window_inside_weekday_blackout_falls_back(self):
        daily_agent._apply_plan({"charge_window_start_hour": 22, "charge_window_end_hour": 6}, WEEKDAY)
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR), (0, 24))

    def test_overnight_window_is_kept_on_weekends(self):
        daily_agent._apply_plan({"charge_window_start_hour": 22, "charge_window_end_hour": 6}, SATURDAY)
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR), (22, 6))

    def test_thresholds_clamped_to_prompt_bounds_and_gap(self):
        daily_agent._apply_plan({"battery_start_pct": 95.0, "battery_stop_pct": 5.0}, WEEKDAY)
        self.assertEqual(config.BATTERY_STOP_PCT, 20.0)
        self.assertEqual(config.BATTERY_START_PCT, 80.0)

    def test_stop_never_below_critical_reserve(self):
        config._apply({"BATTERY_LOW_RESERVE_PCT": 30.0})
        daily_agent._apply_plan({"battery_start_pct": 60.0, "battery_stop_pct": 22.0}, WEEKDAY)
        self.assertEqual(config.BATTERY_STOP_PCT, 30.0)

    def test_start_too_close_to_stop_is_widened(self):
        daily_agent._apply_plan({"battery_start_pct": 48.0, "battery_stop_pct": 45.0}, WEEKDAY)
        self.assertEqual(config.BATTERY_START_PCT, 55.0)


if __name__ == "__main__":
    unittest.main()
