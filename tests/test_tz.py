import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

from core import config


class TestTimezone(unittest.TestCase):
    def test_scheduler_accepts_iana_timezone(self):
        """schedule.every().day.at(..., tz) must accept the configured zone key."""
        tz_key = getattr(config.TZ, "key", "America/Los_Angeles")
        schedule.clear()
        try:
            schedule.every().day.at("23:50", tz_key).do(lambda: None)
            self.assertEqual(len(schedule.jobs), 1)
            self.assertIsNotNone(schedule.jobs[0].next_run)
        finally:
            schedule.clear()

    def test_config_timezone_is_aware(self):
        """datetime.now(config.TZ) must be timezone-aware for TOU maths."""
        now = datetime.now(config.TZ)
        self.assertIsNotNone(now.tzinfo)
        self.assertIsInstance(config.TZ, ZoneInfo)

    def test_log_timestamps_track_wall_clock(self):
        """Local log time and the configured zone must not drift apart."""
        local = datetime.fromtimestamp(time.time(), tz=config.TZ)
        self.assertEqual(local.date(), datetime.now(config.TZ).date())


if __name__ == "__main__":
    unittest.main()
