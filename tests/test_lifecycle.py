"""Startup notice: planned restart vs unexpected stop vs first run."""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core import config, lifecycle

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=config.TZ)


class TestStartupMessage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = (config.HEARTBEAT_FILE, lifecycle.EXIT_MARKER_FILE)
        config.HEARTBEAT_FILE = os.path.join(self.dir, "heartbeat")
        lifecycle.EXIT_MARKER_FILE = os.path.join(self.dir, "last_exit.json")

    def tearDown(self):
        config.HEARTBEAT_FILE, lifecycle.EXIT_MARKER_FILE = self._saved

    def _heartbeat(self, at: datetime):
        open(config.HEARTBEAT_FILE, "w").close()
        os.utime(config.HEARTBEAT_FILE, (at.timestamp(), at.timestamp()))

    def test_first_run(self):
        self.assertIn("first time", lifecycle.startup_message(NOW))

    def test_planned_restart_is_reported_and_marker_consumed(self):
        lifecycle.record_exit("Telegram /update")
        msg = lifecycle.startup_message(datetime.now(config.TZ))
        self.assertIn("planned restart (Telegram /update)", msg)
        self.assertFalse(os.path.exists(lifecycle.EXIT_MARKER_FILE))

    def test_no_marker_after_a_previous_run_is_unexpected(self):
        self._heartbeat(NOW - timedelta(minutes=49))
        msg = lifecycle.startup_message(NOW)
        self.assertIn("UNEXPECTED stop", msg)
        self.assertIn("down 49 min", msg)


if __name__ == "__main__":
    unittest.main()
