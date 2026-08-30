"""Smoke tests for the service-layer locking added around shared caches."""
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import agent.alerts as alerts
import reporting.csv_logger as csv_logger
import services.sheets_db as sheets_db
from core import config
from tests.helpers import powerwall


class TestWorksheetHandleLocking(unittest.TestCase):
    def test_concurrent_get_and_reset_do_not_corrupt_handles(self):
        class StubSheet:
            def worksheet(self, title):
                time.sleep(0.001)  # widen the race window
                return f"ws:{title}"

        errors = []

        def getter():
            for _ in range(50):
                try:
                    ws = sheets_db.get_or_create_worksheet("Telemetry")
                    if ws is not None and ws != "ws:Telemetry":
                        errors.append(ws)
                except Exception as e:
                    errors.append(e)

        def resetter():
            for _ in range(50):
                sheets_db._reset_connection()

        with mock.patch.object(sheets_db, "get_sheet", return_value=StubSheet()):
            threads = [threading.Thread(target=getter) for _ in range(3)]
            threads.append(threading.Thread(target=resetter))
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        sheets_db._reset_connection()
        self.assertEqual(errors, [])


class TestAlertsCacheLocking(unittest.TestCase):
    def tearDown(self):
        alerts.save_alerts([])

    def test_concurrent_crud_and_load_do_not_corrupt(self):
        errors = []
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                alerts.save_alerts([{"id": str(i), "field": "battery_pct",
                                     "operator": "gte", "value": 80.0,
                                     "message": "m", "once": True}])
                i += 1

        def reader():
            while not stop.is_set():
                result = alerts.load_alerts()
                if not isinstance(result, list):
                    errors.append(result)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        threading.Event().wait(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])


class TestLogRowsCacheInvalidation(unittest.TestCase):
    def test_log_to_csv_invalidates_the_analytics_cache(self):
        with mock.patch("services.sheets_db.get_recent_logs", return_value=[{"date": "2026-08-01"}]), \
             mock.patch("services.sheets_db.append_log_row", return_value=True):
            rows = csv_logger.get_all_log_rows(force_refresh=True)
            self.assertEqual(rows, [{"date": "2026-08-01"}])
            with csv_logger._log_rows_cache_lock:
                self.assertIsNotNone(csv_logger._log_rows_cache)

            csv_logger.log_to_csv(powerwall(), "hold", "test row", datetime.now(config.TZ))
            with csv_logger._log_rows_cache_lock:
                self.assertIsNone(csv_logger._log_rows_cache,
                                  "a new telemetry row must invalidate the cache")


if __name__ == "__main__":
    unittest.main()
