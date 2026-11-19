"""Manual mode always lasts long enough to finish a late-night charge."""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import unittest
from datetime import datetime

from core import config
from core.manual_override import manual_reset_at


class TestManualResetTime(unittest.TestCase):
    def setUp(self):
        self._hour = config.NIGHT_BLACKOUT_END_HOUR
        self.addCleanup(setattr, config, "NIGHT_BLACKOUT_END_HOUR", self._hour)

    def at(self, day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=config.TZ)

    def test_set_just_before_midnight_runs_to_next_midnight(self):
        config.NIGHT_BLACKOUT_END_HOUR = 0
        self.assertEqual(manual_reset_at(self.at(18, 23, 51)), self.at(20, 0))

    def test_set_in_the_morning_resets_that_midnight(self):
        config.NIGHT_BLACKOUT_END_HOUR = 0
        self.assertEqual(manual_reset_at(self.at(18, 9)), self.at(19, 0))

    def test_evening_override_still_resets_next_morning(self):
        config.NIGHT_BLACKOUT_END_HOUR = 6
        self.assertEqual(manual_reset_at(self.at(18, 20)), self.at(19, 6))


if __name__ == "__main__":
    unittest.main()
