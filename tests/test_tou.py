"""Direct coverage for the TOU rate math and blackout-window logic."""
import unittest
from datetime import datetime
from unittest import mock

from core import config, tou

# Wednesdays, no holidays.
SUMMER_PEAK = datetime(2026, 7, 15, 18, 0, tzinfo=config.TZ)
WINTER_PEAK = datetime(2026, 1, 14, 18, 0, tzinfo=config.TZ)
SATURDAY_PEAK = datetime(2026, 7, 18, 18, 0, tzinfo=config.TZ)


class TestTouPeriods(unittest.TestCase):
    def test_weekday_period_boundaries(self):
        expectations = {0: "off_peak", 12: "off_peak", 13: "partial_peak", 16: "partial_peak",
                        17: "on_peak", 19: "on_peak", 20: "partial_peak", 22: "partial_peak",
                        23: "off_peak"}
        for hour, expected in expectations.items():
            moment = SUMMER_PEAK.replace(hour=hour)
            self.assertEqual(tou.get_tou_period(moment), expected, f"hour {hour}")

    def test_weekend_is_always_off_peak(self):
        self.assertEqual(tou.get_tou_period(SATURDAY_PEAK), "off_peak")

    def test_schedule_description_matches_the_constants(self):
        desc = tou.weekday_schedule_description()
        (p1_lo, p1_hi), (p2_lo, p2_hi) = tou.PARTIAL_PEAK_HOURS
        self.assertIn(f"{tou.ON_PEAK_HOURS[0]}:00", desc["on_peak"])
        self.assertIn(f"{p1_lo}:00 - {p1_hi}:00", desc["partial_peak_1"])
        self.assertIn(f"{p2_lo}:00 - {p2_hi}:00", desc["partial_peak_2"])


class TestRates(unittest.TestCase):
    def test_seasonal_split_may_through_september_is_summer(self):
        schedule = tou.RATE_SCHEDULES[tou.provider()]
        self.assertEqual(tou.get_base_tou_rate(SUMMER_PEAK), schedule["summer"]["on_peak"])
        self.assertEqual(tou.get_base_tou_rate(WINTER_PEAK), schedule["winter"]["on_peak"])
        # Boundary months.
        may = SUMMER_PEAK.replace(month=5, day=13)
        october = SUMMER_PEAK.replace(month=10, day=14)
        self.assertEqual(tou.get_base_tou_rate(may), schedule["summer"]["on_peak"])
        self.assertEqual(tou.get_base_tou_rate(october), schedule["winter"]["on_peak"])

    def test_unknown_provider_falls_back_to_mid(self):
        with mock.patch.object(config, "UTILITY_PROVIDER", "UNKNOWN_UTILITY"):
            self.assertEqual(tou.get_base_tou_rate(SUMMER_PEAK),
                             tou.RATE_SCHEDULES["MID"]["summer"]["on_peak"])

    def test_effective_rate_applies_adder_and_tax(self):
        base = tou.get_base_tou_rate(SUMMER_PEAK)
        expected = round((base + config.UTILITY_VOLUMETRIC_ADDER) * config.UTILITY_TAX_MULTIPLIER, 5)
        self.assertEqual(tou.get_tou_rate(SUMMER_PEAK), expected)

    def test_expensive_period_covers_on_and_partial_peak(self):
        self.assertTrue(tou.is_expensive_period(SUMMER_PEAK))
        self.assertTrue(tou.is_expensive_period(SUMMER_PEAK.replace(hour=14)))
        self.assertFalse(tou.is_expensive_period(SUMMER_PEAK.replace(hour=8)))


class TestNightBlackoutWindow(unittest.TestCase):
    def setUp(self):
        self._original = (config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR)
        self.addCleanup(self._restore)

    def _restore(self):
        config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR = self._original

    def test_wrapping_window_covers_both_sides_of_midnight(self):
        config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR = 22, 6
        self.assertTrue(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=23)))
        self.assertTrue(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=3)))
        self.assertFalse(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=12)))

    def test_non_wrapping_window(self):
        config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR = 16, 21
        self.assertTrue(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=17)))
        self.assertFalse(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=22)))
        self.assertFalse(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=15)))

    def test_zero_width_window_never_matches(self):
        config.NIGHT_BLACKOUT_START_HOUR = config.NIGHT_BLACKOUT_END_HOUR = 0
        for hour in range(24):
            self.assertFalse(tou.is_in_night_blackout(SUMMER_PEAK.replace(hour=hour)))


if __name__ == "__main__":
    unittest.main()
