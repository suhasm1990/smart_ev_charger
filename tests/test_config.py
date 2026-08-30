"""Tests for the locked dynamic-config layer (update/snapshot/precedence)."""
import json
import os
import threading
import unittest
from unittest import mock

from core import config


def _restore_dynamic_defaults():
    config._apply({key: default for key, (_, default) in config.DYNAMIC_CONFIG_SCHEMA.items()})


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        # Stub persistence and snapshot the local JSON mirror.
        self._save_patch = mock.patch.object(config, "save_dynamic_config", lambda blocking=True: None)
        self.save_stub = self._save_patch.start()
        self.addCleanup(self._save_patch.stop)
        self._json_backup = None
        if os.path.exists(config.DYNAMIC_CONFIG_FILE):
            with open(config.DYNAMIC_CONFIG_FILE) as f:
                self._json_backup = f.read()
        self.addCleanup(self._restore_json)
        self.addCleanup(_restore_dynamic_defaults)

    def _restore_json(self):
        if self._json_backup is not None:
            with open(config.DYNAMIC_CONFIG_FILE, "w") as f:
                f.write(self._json_backup)
        elif os.path.exists(config.DYNAMIC_CONFIG_FILE):
            os.remove(config.DYNAMIC_CONFIG_FILE)


class TestUpdate(ConfigTestCase):
    def test_update_casts_via_the_schema(self):
        config.update(BATTERY_START_PCT="55", NIGHT_BLACKOUT_START_HOUR="17")
        self.assertEqual(config.BATTERY_START_PCT, 55.0)
        self.assertEqual(config.NIGHT_BLACKOUT_START_HOUR, 17)

    def test_update_enforces_the_hysteresis_guard(self):
        config.update(BATTERY_START_PCT=30.0, BATTERY_STOP_PCT=40.0)
        self.assertGreater(config.BATTERY_START_PCT, config.BATTERY_STOP_PCT)

    def test_update_rejects_unknown_keys(self):
        with self.assertRaises(KeyError):
            config.update(NOT_A_REAL_KEY=1)

    def test_update_persists(self):
        calls = []
        with mock.patch.object(config, "save_dynamic_config", lambda blocking=True: calls.append(1)):
            config.update(BATTERY_START_PCT=55.0)
        self.assertEqual(calls, [1])


class TestPrecedence(ConfigTestCase):
    """Layering, lowest to highest: env defaults -> local JSON -> Google Sheets."""

    def test_sheets_wins_over_json_wins_over_default(self):
        with open(config.DYNAMIC_CONFIG_FILE, "w") as f:
            json.dump({"BATTERY_START_PCT": 50.0}, f)

        # With Sheets empty, the JSON layer applies over the env/schema default.
        with mock.patch("services.sheets_db.get_settings", return_value={}):
            config.load_dynamic_config(remote=True)
        self.assertEqual(config.BATTERY_START_PCT, 50.0)

        # A Sheets value wins over the JSON layer (and is mirrored back to it).
        with mock.patch("services.sheets_db.get_settings", return_value={"BATTERY_START_PCT": "60"}):
            config.load_dynamic_config(remote=True)
        self.assertEqual(config.BATTERY_START_PCT, 60.0)
        with open(config.DYNAMIC_CONFIG_FILE) as f:
            self.assertEqual(json.load(f)["BATTERY_START_PCT"], 60.0, "Sheets value mirrors to JSON")

        os.remove(config.DYNAMIC_CONFIG_FILE)
        config.load_dynamic_config(remote=False)
        self.assertEqual(config.BATTERY_START_PCT, 40.0, "schema default applies with no other layer")


class TestSnapshotConsistency(ConfigTestCase):
    def test_snapshot_never_observes_a_half_applied_update(self):
        stop = threading.Event()
        violations = []

        def writer():
            while not stop.is_set():
                config.update(BATTERY_START_PCT=60.0, BATTERY_STOP_PCT=50.0)
                config.update(BATTERY_START_PCT=30.0, BATTERY_STOP_PCT=20.0)

        def reader():
            while not stop.is_set():
                snap = config.snapshot()
                if snap.BATTERY_START_PCT <= snap.BATTERY_STOP_PCT:
                    violations.append((snap.BATTERY_START_PCT, snap.BATTERY_STOP_PCT))

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        threading.Event().wait(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(violations, [],
                         "snapshot() observed BATTERY_START_PCT <= BATTERY_STOP_PCT mid-update")


if __name__ == "__main__":
    unittest.main()
