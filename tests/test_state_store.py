"""Tests for the locked StateStore that replaced the bare module globals."""
import threading
import unittest
from datetime import datetime, timedelta

from core import config, state


class TestStateStoreBasics(unittest.TestCase):
    def setUp(self):
        state.reset_for_tests()
        self.addCleanup(state.reset_for_tests)

    def test_attribute_access_keeps_working(self):
        state.charger_state = state.State.CHARGING
        self.assertEqual(state.charger_state, state.State.CHARGING)

    def test_unknown_field_write_raises(self):
        with self.assertRaises(AttributeError):
            state.chargr_state = state.State.IDLE  # typo'd names must not vanish silently

    def test_unknown_field_read_raises(self):
        with self.assertRaises(AttributeError):
            _ = state.nonexistent_field

    def test_bump_increments_and_returns(self):
        self.assertEqual(state.bump("grid_draw_count"), 1)
        self.assertEqual(state.bump("grid_draw_count"), 2)
        self.assertEqual(state.grid_draw_count, 2)

    def test_reset_daily_returns_old_counters(self):
        state.session_count_today = 3
        state.grid_draw_count = 7
        self.assertEqual(state.reset_daily(), (3, 7))
        self.assertEqual(state.session_count_today, 0)
        self.assertEqual(state.grid_draw_count, 0)

    def test_begin_and_end_session(self):
        now = datetime.now(config.TZ)
        state.begin_session(now, 32)
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(state.charge_session_start, now)
        self.assertEqual(state.active_amperage, 32)
        self.assertEqual(state.session_count_today, 1)
        self.assertIsNone(state.session_stop_reason)

        state.end_session("battery low")
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)
        self.assertEqual(state.session_stop_reason, "battery low")
        self.assertEqual(state.session_count_today, 1, "ending must not touch the counter")


class TestSyncWithHardware(unittest.TestCase):
    """The single reconciliation used by the cycle, startup, and the bot."""

    def setUp(self):
        state.reset_for_tests()
        self.addCleanup(state.reset_for_tests)
        self.now = datetime.now(config.TZ)

    def test_adopts_an_externally_started_session(self):
        reported = self.now - timedelta(minutes=20)
        result = state.sync_with_hardware(
            {"charging_status": "CHARGING", "session_start_time": reported, "amperage_limit": 32},
            self.now)
        self.assertEqual(result, "adopted")
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(state.charge_session_start, reported)
        self.assertEqual(state.active_amperage, 32)
        self.assertIsNone(state.session_stop_reason)

    def test_adopts_with_now_when_hardware_reports_no_start_time(self):
        result = state.sync_with_hardware({"charging_status": "CHARGING"}, self.now)
        self.assertEqual(result, "adopted")
        self.assertEqual(state.charge_session_start, self.now)

    def test_corrects_a_drifted_session_start(self):
        state.begin_session(self.now, 20)
        reported = self.now - timedelta(hours=3)
        result = state.sync_with_hardware(
            {"charging_status": "CHARGING", "session_start_time": reported}, self.now)
        self.assertEqual(result, "drift")
        self.assertEqual(state.charge_session_start, reported)

    def test_small_drift_is_a_no_op(self):
        state.begin_session(self.now, 20)
        reported = self.now - timedelta(minutes=5)
        result = state.sync_with_hardware(
            {"charging_status": "CHARGING", "session_start_time": reported}, self.now)
        self.assertIsNone(result)
        self.assertEqual(state.charge_session_start, self.now)

    def test_clears_a_stale_session_when_hardware_is_idle(self):
        state.begin_session(self.now, 20)
        result = state.sync_with_hardware({"charging_status": "AVAILABLE"}, self.now)
        self.assertEqual(result, "cleared")
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)

    def test_idle_on_both_sides_is_a_no_op(self):
        result = state.sync_with_hardware({"charging_status": "AVAILABLE"}, self.now)
        self.assertIsNone(result)


class TestSnapshotConsistency(unittest.TestCase):
    """Snapshots must never expose a half-applied session transition."""

    def setUp(self):
        state.reset_for_tests()
        self.addCleanup(state.reset_for_tests)

    def test_snapshot_is_internally_consistent_under_concurrent_writes(self):
        stop = threading.Event()
        inconsistencies = []

        def writer():
            now = datetime.now(config.TZ)
            while not stop.is_set():
                state.begin_session(now, 20)
                state.end_session("test stop")

        def reader():
            while not stop.is_set():
                snap = state.snapshot()
                charging = snap.charger_state == state.State.CHARGING
                has_start = snap.charge_session_start is not None
                if charging != has_start:
                    inconsistencies.append(snap)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        threading.Event().wait(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(inconsistencies, [],
                         "snapshot() observed CHARGING without a session start (or vice versa)")


if __name__ == "__main__":
    unittest.main()
