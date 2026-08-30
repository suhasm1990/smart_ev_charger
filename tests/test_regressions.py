"""Regression coverage for defects found and fixed during the refactor."""
import logging
import os
import time
import unittest

import reporting.csv_logger as csv_logger
import services.sheets_db as sheets_db
from agent import llm_client
from core import config, state
from reporting.logger import tail_lines


class FakeWorksheet:
    """Records what actually lands in the sheet, without touching the network."""

    def __init__(self, values=None):
        self.values = values or [["Key", "Value"]]
        self.reads = 0

    def get_all_values(self):
        self.reads += 1
        return [list(row) for row in self.values]

    def clear(self):
        self.values = []

    def update(self, range_name=None, values=None):
        self.values = [list(row) for row in values]

    def as_dict(self):
        return {row[0]: row[1] for row in self.values[1:] if len(row) >= 2}


class TestSettingsPersistence(unittest.TestCase):
    """A threshold save used to clear the whole Settings tab, losing other keys."""

    def setUp(self):
        self.sheet = FakeWorksheet()
        self._original = sheets_db.get_or_create_worksheet
        sheets_db.get_or_create_worksheet = lambda *a, **k: self.sheet
        sheets_db._settings_cache, sheets_db._settings_cache_time = None, 0.0

    def tearDown(self):
        sheets_db.get_or_create_worksheet = self._original
        sheets_db._settings_cache, sheets_db._settings_cache_time = None, 0.0

    def test_user_instruction_survives_a_threshold_save(self):
        sheets_db.add_user_instruction("Charge only after 10am")
        sheets_db._write_settings({"BATTERY_START_PCT": "40", "BATTERY_STOP_PCT": "25"})
        stored = self.sheet.as_dict()
        self.assertEqual(stored.get("USER_INSTRUCTION"), "Charge only after 10am")
        self.assertEqual(stored.get("BATTERY_START_PCT"), "40")

    def test_clear_user_instruction_removes_only_that_key(self):
        sheets_db.add_user_instruction("Charge only after 10am")
        sheets_db._write_settings({"BATTERY_START_PCT": "40"})
        sheets_db.clear_user_instruction()
        stored = self.sheet.as_dict()
        self.assertNotIn("USER_INSTRUCTION", stored)
        self.assertEqual(stored.get("BATTERY_START_PCT"), "40")

    def test_settings_reads_are_cached(self):
        """The control loop reads settings every cycle; it must not refetch each time."""
        sheets_db._settings_cache, sheets_db._settings_cache_time = None, 0.0
        for _ in range(20):
            sheets_db.get_settings()
        self.assertEqual(self.sheet.reads, 1)

    def test_force_refresh_bypasses_the_cache(self):
        sheets_db.get_settings()
        sheets_db.get_settings(force_refresh=True)
        self.assertEqual(self.sheet.reads, 2)

    def test_stale_cache_is_served_when_sheets_fails(self):
        sheets_db.get_settings(force_refresh=True)
        sheets_db._settings_cache_time = 0.0  # Expire it.
        sheets_db.get_or_create_worksheet = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
        self.assertIsInstance(sheets_db.get_settings(), dict)


class TestAmperageAccounting(unittest.TestCase):
    """Analytics used a hardcoded 4.8 kW and mispriced non-default amperages."""

    def _reading(self, **overrides):
        row = {
            "date": "2026-08-01", "timestamp": "2026-08-01T12:00:00",
            "solar_kw": "0", "home_kw": "8", "grid_kw": "8",
            "charger_state": "CHARGING", "action": "hold",
            "session_active_minutes": "60", **overrides,
        }
        return next(csv_logger._readings([row]))

    def test_32a_row_is_priced_at_its_own_power(self):
        self.assertAlmostEqual(self._reading(charger_amperage="32").ev_power_kw, 7.68, places=2)

    def test_20a_row_is_priced_at_its_own_power(self):
        self.assertAlmostEqual(self._reading(charger_amperage="20").ev_power_kw, 4.8, places=2)

    def test_legacy_rows_fall_back_to_the_configured_default(self):
        expected = state.charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE)
        self.assertAlmostEqual(self._reading(charger_amperage="").ev_power_kw, expected, places=2)

    def test_out_of_range_amperage_is_rejected(self):
        expected = state.charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE)
        self.assertAlmostEqual(self._reading(charger_amperage="999").ev_power_kw, expected, places=2)

    def test_idle_rows_draw_no_charger_power(self):
        reading = self._reading(charger_state="IDLE", action="hold")
        self.assertEqual(reading.ev_power_kw, 0.0)
        self.assertEqual(reading.ev_grid_kw, 0.0)


class TestAnalyticsRobustness(unittest.TestCase):
    def setUp(self):
        self._original = csv_logger.get_all_log_rows

    def tearDown(self):
        csv_logger.get_all_log_rows = self._original

    def test_advice_handles_rows_outside_the_window(self):
        csv_logger.get_all_log_rows = lambda days=7, force_refresh=False: [{
            "date": "2020-01-01", "timestamp": "2020-01-01T12:00:00", "solar_kw": "3",
            "home_kw": "1", "grid_kw": "0", "charger_state": "IDLE", "action": "hold",
        }]
        advice = csv_logger.get_energy_saving_advice()
        self.assertNotIn("error", advice)
        self.assertIn("recommended_evening_battery_reserve_pct", advice)

    def test_malformed_rows_are_skipped_not_fatal(self):
        csv_logger.get_all_log_rows = lambda days=7, force_refresh=False: [
            {"date": "not-a-date", "solar_kw": "x"},
            {"timestamp": "", "grid_kw": None},
            {},
        ]
        for result in (csv_logger.get_daily_charging_cost("today"),
                       csv_logger.get_home_energy_summary("today"),
                       csv_logger.get_energy_saving_advice()):
            self.assertNotIn("error", result)

    def test_empty_history_reports_cleanly(self):
        csv_logger.get_all_log_rows = lambda days=7, force_refresh=False: []
        self.assertIn("error", csv_logger.get_daily_charging_cost("today"))
        self.assertIn("error", csv_logger.get_monthly_billing_data("last_month"))

    def test_future_month_is_rejected(self):
        csv_logger.get_all_log_rows = lambda days=365, force_refresh=False: [
            {"date": "2026-08-01", "timestamp": "2026-08-01T12:00:00"}]
        self.assertIn("error", csv_logger.get_monthly_billing_data("2099-01"))


class TestConversationHistory(unittest.TestCase):
    """Blind truncation could orphan a tool result and get rejected by the API."""

    HISTORY = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
    ]

    def _has_orphan_tool_message(self, messages):
        return any(
            m.get("role") == "tool" and not any(p.get("tool_calls") for p in messages[:i])
            for i, m in enumerate(messages)
        )

    def test_no_orphan_tool_results_at_any_limit(self):
        for limit in range(1, len(self.HISTORY) + 2):
            trimmed = llm_client.trim_history(self.HISTORY, limit)
            self.assertFalse(self._has_orphan_tool_message(trimmed), f"limit={limit}")

    def test_system_prompt_is_preserved(self):
        trimmed = llm_client.trim_history(self.HISTORY, 2)
        self.assertEqual(trimmed[0]["role"], "system")

    def test_short_history_is_returned_unchanged(self):
        self.assertEqual(llm_client.trim_history(self.HISTORY, 50), self.HISTORY)


class TestLogTailing(unittest.TestCase):
    """Error scanning used to read the entire multi-megabyte log every cycle."""

    def setUp(self):
        self.path = f"{config.TEXT_LOG_FILE}.tailtest"
        with open(self.path, "w") as f:
            for i in range(50_000):
                level = "ERROR" if i % 1000 == 0 else "INFO"
                f.write(f"2026-08-27 12:00:00,000 | {level:<8} | EV_CHARGER   | line {i}\n")

    def tearDown(self):
        import os
        os.remove(self.path)

    def test_returns_the_last_lines(self):
        lines = tail_lines(self.path, 100)
        self.assertEqual(len(lines), 100)
        self.assertIn("line 49999", lines[-1])

    def test_level_filter_applies(self):
        self.assertTrue(all("ERROR" in line for line in tail_lines(self.path, 5, level="ERROR")))

    def test_cost_is_bounded_not_proportional_to_file_size(self):
        start = time.perf_counter()
        tail_lines(self.path, 100)
        self.assertLess(time.perf_counter() - start, 0.05)

    def test_missing_file_returns_empty(self):
        self.assertEqual(tail_lines("/nonexistent/path.log", 10), [])


if __name__ == "__main__":
    unittest.main()


class TestTelegramAuthorization(unittest.TestCase):
    """The bot reaches shell execution via the dev agent, so authorisation is
    enforced once in middleware rather than repeated in each handler."""

    OWNER = 111
    ATTACKER = 999

    def setUp(self):
        import telebot

        from agent.telegram_bot import AuthMiddleware

        self.reached = []
        self.replies = []
        self.bot = telebot.TeleBot("1:fake", validate_token=False,
                                   use_class_middlewares=True, threaded=False)
        self.bot.reply_to = lambda message, text, **kw: self.replies.append(text)
        self.bot.send_chat_action = lambda *a, **k: None
        self.bot.setup_middleware(AuthMiddleware(self.bot))

        self._original_allowed = config.TELEGRAM_ALLOWED_USER_ID
        config.TELEGRAM_ALLOWED_USER_ID = self.OWNER

        # Registered exactly as a future contributor would, with no auth code:
        # if the middleware is the real choke point, these stay unreachable.
        @self.bot.message_handler(commands=["logs"])
        def _logs(message):
            self.reached.append("command")

        @self.bot.message_handler(func=lambda message: True)
        def _chat(message):
            self.reached.append("catchall")

    def tearDown(self):
        config.TELEGRAM_ALLOWED_USER_ID = self._original_allowed

    def _send(self, user_id, text):
        from types import SimpleNamespace
        self.reached.clear()
        self.bot.process_new_messages([SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="t", username="t"),
            chat=SimpleNamespace(id=user_id, type="private"), text=text, content_type="text",
            message_id=1, date=0, json={}, entities=None, caption=None, html_text=text,
        )])
        return list(self.reached)

    def test_owner_reaches_commands_and_chat(self):
        self.assertEqual(self._send(self.OWNER, "/logs"), ["command"])
        self.assertEqual(self._send(self.OWNER, "charge at 32A"), ["catchall"])

    def test_unauthorized_user_reaches_no_handler(self):
        self.assertEqual(self._send(self.ATTACKER, "/logs"), [])
        self.assertEqual(self._send(self.ATTACKER, "charge at 32A"), [])
        self.assertEqual(self._send(self.ATTACKER, "/update"), [])

    def test_unauthorized_user_is_told_why(self):
        self.replies.clear()
        self._send(self.ATTACKER, "/logs")
        self.assertTrue(any("Unauthorized" in r for r in self.replies))

    def test_bot_refuses_to_start_without_an_allowlist(self):
        """An unset allowlist must deny everyone, not allow everyone."""
        from agent import telegram_bot
        started = []
        original_token, original_allowed = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_ALLOWED_USER_ID
        original_thread = telegram_bot.threading.Thread
        config.TELEGRAM_BOT_TOKEN = "1:fake"
        telegram_bot.threading.Thread = lambda *a, **k: type(
            "T", (), {"start": lambda self: started.append(1)})()
        try:
            for bad_value in (None,):
                config.TELEGRAM_ALLOWED_USER_ID = bad_value
                telegram_bot.start_telegram_bot(lambda: None)
                self.assertEqual(started, [], f"bot started with allowlist={bad_value!r}")
        finally:
            telegram_bot.threading.Thread = original_thread
            config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_ALLOWED_USER_ID = original_token, original_allowed

    def test_invalid_allowlist_values_parse_to_none(self):
        """A non-numeric ID must become None (deny) rather than an unmatchable string."""
        import importlib

        import core.config as config_module
        original = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
        try:
            for raw, expected in (("", None), ("   ", None), ("not-a-number", None), ("12345", 12345)):
                os.environ["TELEGRAM_ALLOWED_USER_ID"] = raw
                importlib.reload(config_module)
                self.assertEqual(config_module.TELEGRAM_ALLOWED_USER_ID, expected, f"input {raw!r}")
        finally:
            if original is None:
                os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
            else:
                os.environ["TELEGRAM_ALLOWED_USER_ID"] = original
            importlib.reload(config_module)


class TestSheetsLoggingFeedbackLoop(unittest.TestCase):
    """A warning raised while writing to Sheets must not be queued for Sheets.

    Without a guard, a missing service_account.json produced an unbounded loop:
    warn -> log handler -> syslog queue -> worker -> warn again.
    """

    def setUp(self):
        self._creds = sheets_db.CREDS_FILE
        self._warned = sheets_db._credentials_warned
        self._active = getattr(sheets_db._worker_local, "active", False)

    def tearDown(self):
        sheets_db.CREDS_FILE = self._creds
        sheets_db._credentials_warned = self._warned
        sheets_db._worker_local.active = self._active

    def test_worker_threads_do_not_queue_their_own_logs(self):
        sheets_db._worker_local.active = True
        self.assertTrue(sheets_db.in_worker())
        self.assertFalse(sheets_db.append_system_log("t", "WARNING", "m", "would loop"))

    def test_missing_credentials_warn_only_once(self):
        sheets_db.CREDS_FILE = "/definitely/not/here.json"
        sheets_db._credentials_warned = False
        sheets_db._client = None
        with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
            sheets_db.get_client()
            for _ in range(50):
                sheets_db.get_client()
        warnings = [line for line in captured.output if "not found" in line]
        self.assertEqual(len(warnings), 1, f"expected 1 warning, got {len(warnings)}")

    def test_disabled_integration_does_not_queue_work(self):
        sheets_db.CREDS_FILE = "/definitely/not/here.json"
        self.assertTrue(sheets_db.is_disabled())
        self.assertFalse(sheets_db.append_log_row(["row"]))
        self.assertFalse(sheets_db.append_system_log("t", "INFO", "m", "msg"))

    def test_log_handler_bails_out_inside_a_worker(self):
        from reporting.logger import GoogleSheetsLogHandler
        sheets_db._worker_local.active = True
        record = logging.LogRecord("EV_CHARGER", logging.WARNING, __file__, 1,
                                   "credentials not found", (), None)
        queued = []
        original = sheets_db.append_system_log
        sheets_db.append_system_log = lambda **kw: queued.append(kw)
        try:
            GoogleSheetsLogHandler().emit(record)
        finally:
            sheets_db.append_system_log = original
        self.assertEqual(queued, [], "handler re-entered the Sheets pipeline")


class TestBatteryThresholdGuards(unittest.TestCase):
    """Guards against unreachable or inverted start/stop battery thresholds."""

    def test_daily_agent_corrects_inverted_thresholds(self):
        from agent.daily_agent import _apply_plan
        from core import config
        bad_plan = {
            "battery_start_pct": 40.0,
            "battery_stop_pct": 43.0,
            "charge_window_start_hour": 10,
            "charge_window_end_hour": 16,
        }
        _apply_plan(bad_plan)
        self.assertGreater(config.BATTERY_START_PCT, config.BATTERY_STOP_PCT)
        self.assertEqual(config.BATTERY_STOP_PCT, 43.0)
        self.assertEqual(config.BATTERY_START_PCT, 53.0)

    def test_telegram_bot_rejects_inverted_thresholds(self):
        from agent.telegram_bot import set_battery_thresholds
        response = set_battery_thresholds(40.0, 43.0)
        self.assertTrue(response.startswith("Error:"))

    def test_config_apply_enforces_start_greater_than_stop(self):
        from core import config
        config._apply({"BATTERY_START_PCT": 30.0, "BATTERY_STOP_PCT": 35.0})
        self.assertGreater(config.BATTERY_START_PCT, config.BATTERY_STOP_PCT)


class TestSettingsSyncAndPersistence(unittest.TestCase):
    """Guards Google Sheets as the single source of truth for runtime settings."""

    def test_sheets_settings_sync_to_local_config(self):
        from unittest.mock import patch

        import services.sheets_db as sheets_db
        from core import config

        fake_sheets_settings = {
            "BATTERY_START_PCT": "60.0",
            "BATTERY_STOP_PCT": "35.0",
            "MANUAL_MODE_OVERRIDE": "manual",
            "LLM_PROVIDER": "nvidia",
            "LLM_MODEL": "nvidia/nemotron-3-super-120b-a12b",
        }

        with patch.object(sheets_db, "get_settings", return_value=fake_sheets_settings):
            config.load_dynamic_config(remote=True, force_refresh=True)
            self.assertEqual(config.MANUAL_MODE_OVERRIDE, "manual")
            self.assertEqual(config.BATTERY_START_PCT, 60.0)
            self.assertEqual(config.BATTERY_STOP_PCT, 35.0)
            self.assertEqual(config.LLM_PROVIDER, "nvidia")
            self.assertEqual(config.LLM_MODEL, "nvidia/nemotron-3-super-120b-a12b")




class TestMeteredEnergyColumn(unittest.TestCase):
    """The metered kWh column is appended last so legacy rows stay parseable."""

    def test_log_to_csv_records_the_metered_energy(self):
        import csv
        import tempfile
        from datetime import datetime
        from unittest.mock import patch

        import reporting.csv_logger as csv_logger
        from core import config
        from tests.helpers import powerwall

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "log.csv")
            stats = powerwall()
            stats["cp_session_energy_kwh"] = 4.25
            with patch.object(config, "CSV_LOG_FILE", csv_path), \
                 patch("services.sheets_db.append_log_row", return_value=True):
                csv_logger.log_to_csv(stats, "hold", "test", datetime.now(config.TZ))
            with open(csv_path, newline="") as f:
                row = list(csv.DictReader(f))[0]
        self.assertEqual(row["cp_session_energy_kwh"], "4.25")

    def test_legacy_rows_without_the_column_read_as_zero(self):
        from reporting.csv_logger import _num
        legacy_row = {"date": "2026-01-01"}
        self.assertEqual(_num(legacy_row.get("cp_session_energy_kwh")), 0.0)
