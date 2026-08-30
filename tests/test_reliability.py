"""Regression tests for the Stage-1 reliability fixes.

Each test pins one previously-observed failure mode: cycle wedges, state
mutated before hardware confirmation, blocking notifications, silent drops.
"""
import asyncio
import queue
import socket
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import requests
import schedule

import main
import services.chargepoint as cp
import services.netzero as netzero
import services.sheets_db as sheets_db
from core import config, state, tou
from services import ChargePointStartError
from tests.helpers import MockedCycle, charger, powerwall


class TestChargePointTimeout(unittest.TestCase):
    """A hung vendor call must abort inside the event loop, not wedge the cycle."""

    def test_timeout_raises_quickly_and_does_not_poison_the_loop(self):
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            cp._run(asyncio.sleep(10), timeout=0.2)
        self.assertLess(time.monotonic() - t0, 2.0, "timeout should fire in ~0.2s, not block")

        # The shared loop must still serve subsequent calls.
        async def quick():
            return "ok"
        self.assertEqual(cp._run(quick(), timeout=5.0), "ok")

    def test_call_timeout_stays_below_cycle_budget(self):
        self.assertLess(cp.CHARGEPOINT_CALL_TIMEOUT, main.CYCLE_TIMEOUT_SECONDS)


class TestPureDecision(unittest.TestCase):
    """evaluate() must not mutate state; transitions follow hardware success."""

    def setUp(self):
        self.mock = MockedCycle()
        self.mock.reset_state()
        self.addCleanup(self.mock.restore)

    def test_evaluate_start_leaves_state_untouched(self):
        from core.decision import evaluate
        stats = {"battery_pct": 80.0, "is_plugged_in": True}
        action, _ = evaluate(stats, datetime.now(config.TZ))
        self.assertEqual(action, "start")
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)
        self.assertEqual(state.session_count_today, 0)

    def test_rejected_start_does_not_inflate_session_counter(self):
        self.mock.install(powerwall(battery_pct=80.0), charger())

        def rejected(amperage=None):
            raise ChargePointStartError("start rejected")
        self.mock._patch(main, "start_charger", rejected)

        main.run_cycle()

        self.assertEqual(state.session_count_today, 0)
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)

    def test_successful_start_records_the_session(self):
        self.mock.install(powerwall(battery_pct=80.0), charger())
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertEqual(state.session_count_today, 1)
        self.assertIsNotNone(state.charge_session_start)

    def test_failed_stop_stays_charging_for_retry(self):
        session_start = datetime.now(config.TZ) - timedelta(minutes=45)
        self.mock.install(powerwall(battery_pct=20.0),
                          charger(charging=True, session_start=session_start))

        def broken_stop():
            raise RuntimeError("network down")
        self.mock._patch(main, "stop_charger", broken_stop)

        main.run_cycle()

        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertIsNone(state.session_stop_reason)

    def test_successful_stop_records_the_reason(self):
        session_start = datetime.now(config.TZ) - timedelta(minutes=45)
        self.mock.install(powerwall(battery_pct=20.0),
                          charger(charging=True, session_start=session_start))
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIn("safe limit", state.session_stop_reason)


class TestNonBlockingNotify(unittest.TestCase):
    def test_notify_returns_immediately_and_still_delivers(self):
        import reporting.notifications as notifications
        delivered = []

        def slow_deliver(message):
            time.sleep(0.3)
            delivered.append(message)

        with mock.patch.object(notifications, "_deliver", slow_deliver):
            t0 = time.monotonic()
            notifications.notify("hello")
            self.assertLess(time.monotonic() - t0, 0.1, "notify() must not block on delivery")
            notifications.notify_flush(timeout=2.0)
        self.assertEqual(delivered, ["hello"])


class TestNetZeroRobustness(unittest.TestCase):
    def setUp(self):
        patcher_site = mock.patch.object(netzero.config, "NETZERO_SITE_ID", "site")
        patcher_token = mock.patch.object(netzero.config, "NETZERO_API_TOKEN", "token")
        patcher_sleep = mock.patch.object(netzero.time, "sleep", lambda s: None)
        for p in (patcher_site, patcher_token, patcher_sleep):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _response(payload=None, status=200):
        resp = mock.Mock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        if status >= 400:
            error = requests.HTTPError(f"{status} error")
            error.response = resp
            resp.raise_for_status.side_effect = error
        else:
            resp.raise_for_status.return_value = None
        return resp

    @staticmethod
    def _live(**overrides):
        live = {"solar_power": 4000, "load_power": 1000, "grid_power": 500,
                "battery_power": -1500, "percentage_charged": 55.0}
        live.update(overrides)
        return {"live_status": live}

    def test_transient_5xx_is_retried(self):
        responses = [self._response(status=502), self._response(self._live())]
        with mock.patch.object(netzero._session, "get", side_effect=responses):
            stats = netzero.get_powerwall_stats()
        self.assertEqual(stats["solar_kw"], 4.0)
        self.assertEqual(stats["grid_kw"], 0.5)

    def test_4xx_raises_immediately(self):
        with mock.patch.object(netzero._session, "get",
                               side_effect=[self._response(status=401)]) as get:
            with self.assertRaises(requests.HTTPError):
                netzero.get_powerwall_stats()
            self.assertEqual(get.call_count, 1)

    def test_missing_live_status_raises_a_clean_error(self):
        with mock.patch.object(netzero._session, "get", return_value=self._response({"other": 1})):
            with self.assertRaisesRegex(ValueError, "live_status"):
                netzero.get_powerwall_stats()

    def test_missing_field_names_the_field(self):
        payload = self._live()
        del payload["live_status"]["grid_power"]
        with mock.patch.object(netzero._session, "get", return_value=self._response(payload)):
            with self.assertRaisesRegex(ValueError, "grid_power"):
                netzero.get_powerwall_stats()

    def test_zero_home_load_does_not_divide_by_zero(self):
        payload = self._live(load_power=0)
        with mock.patch.object(netzero._session, "get", return_value=self._response(payload)):
            stats = netzero.get_powerwall_stats()
        self.assertEqual(stats["self_powered_pct"], 100.0)

    def test_solar_noise_floor_reads_as_zero(self):
        payload = self._live(solar_power=30)  # 0.03 kW of inverter noise
        with mock.patch.object(netzero._session, "get", return_value=self._response(payload)):
            stats = netzero.get_powerwall_stats()
        self.assertEqual(stats["solar_kw"], 0.0)


class TestLoudConfigSync(unittest.TestCase):
    """Sheets is the primary source of truth — sync failures must be logged."""

    def setUp(self):
        config._last_sync_warning = None
        self.addCleanup(lambda: setattr(config, "_last_sync_warning", None))

    def test_load_failure_warns_and_still_applies_local_layers(self):
        with mock.patch("services.sheets_db.get_settings", side_effect=RuntimeError("offline")):
            with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
                config.load_dynamic_config(remote=True)
        self.assertTrue(any("Config load" in line for line in captured.output))
        self.assertIsInstance(config.BATTERY_START_PCT, float)

    def test_save_failure_warns(self):
        with mock.patch("services.sheets_db.update_settings", side_effect=RuntimeError("offline")):
            with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
                config.save_dynamic_config()
        self.assertTrue(any("Config save" in line for line in captured.output))

    def test_first_warning_fires_even_just_after_boot(self):
        """time.monotonic() counts from boot; a 0.0 'never warned' sentinel
        swallowed the first warning on hosts up for less than the rate-limit
        interval (exactly what fresh CI runners are)."""
        with mock.patch("time.monotonic", return_value=5.0), \
             mock.patch("services.sheets_db.update_settings", side_effect=RuntimeError("offline")):
            with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
                config.save_dynamic_config()
        self.assertTrue(any("Config save" in line for line in captured.output))

    def test_warning_is_rate_limited(self):
        with mock.patch("services.sheets_db.update_settings", side_effect=RuntimeError("offline")):
            with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
                config.save_dynamic_config()
                config.save_dynamic_config()  # within the rate-limit window
        warnings = [line for line in captured.output if "Config save" in line]
        self.assertEqual(len(warnings), 1)


class TestSheetsFlushAndDrops(unittest.TestCase):
    def test_flush_waits_for_an_inflight_write(self):
        q = queue.Queue()
        done = []

        def worker():
            item = q.get()
            time.sleep(0.3)  # simulate the network write after the pop
            done.append(item)
            q.task_done()

        with mock.patch.object(sheets_db, "_telemetry_queue", q), \
             mock.patch.object(sheets_db, "_syslog_queue", queue.Queue()), \
             mock.patch.object(sheets_db, "_settings_queue", queue.Queue()):
            q.put("row")
            threading.Thread(target=worker, daemon=True).start()
            sheets_db.flush(timeout=2.0)
        self.assertEqual(done, ["row"], "flush() returned while the write was still in flight")

    def test_unavailable_worksheet_drop_is_logged_not_silent(self):
        with mock.patch.object(sheets_db, "get_or_create_worksheet", return_value=None), \
             mock.patch.object(sheets_db.time, "sleep", lambda s: None):
            with self.assertLogs("EV_CHARGER", level="WARNING") as captured:
                result = sheets_db._write_batch("Telemetry", [["row"]], None, None, 6000, 100)
        self.assertIsNone(result)
        self.assertTrue(any("unavailable" in line for line in captured.output))
        self.assertTrue(any("Dropped 1 row" in line for line in captured.output))


class TestUtilityHolidays(unittest.TestCase):
    OLD_TABLE = {
        date(2025, 1, 1), date(2025, 2, 17), date(2025, 5, 26),
        date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 11),
        date(2025, 11, 27), date(2025, 12, 25),
        date(2026, 1, 1), date(2026, 2, 16), date(2026, 5, 25),
        date(2026, 7, 4), date(2026, 9, 7), date(2026, 11, 11),
        date(2026, 11, 26), date(2026, 12, 25),
    }

    def test_computed_holidays_match_the_old_hardcoded_table(self):
        computed = set(tou.utility_holidays(2025)) | set(tou.utility_holidays(2026))
        self.assertEqual(computed, self.OLD_TABLE)

    def test_2027_holidays_are_computed_correctly(self):
        holidays = tou.utility_holidays(2027)
        self.assertIn(date(2027, 2, 15), holidays)   # Presidents Day
        self.assertIn(date(2027, 5, 31), holidays)   # Memorial Day
        self.assertIn(date(2027, 9, 6), holidays)    # Labor Day
        self.assertIn(date(2027, 11, 25), holidays)  # Thanksgiving

    def test_holiday_evening_is_priced_off_peak(self):
        # Thanksgiving 2027 at 17:30 would be on_peak on a normal Thursday.
        moment = datetime(2027, 11, 25, 17, 30, tzinfo=config.TZ)
        self.assertEqual(tou.get_tou_period(moment), "off_peak")


class TestDynamicCycleInterval(unittest.TestCase):
    def tearDown(self):
        schedule.clear("cycle")
        main._scheduled_interval = None
        config.CHECK_INTERVAL_MINUTES = 15

    def test_interval_change_reschedules_the_cycle_job(self):
        main._register_cycle_job(15)
        self.assertEqual(schedule.get_jobs("cycle")[0].interval, 15)

        config.CHECK_INTERVAL_MINUTES = 7
        main._sync_cycle_interval()

        jobs = schedule.get_jobs("cycle")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].interval, 7)

    def test_interval_is_clamped_to_a_sane_range(self):
        self.assertEqual(config._interval_minutes(999), 60)
        self.assertEqual(config._interval_minutes(0), 1)
        self.assertEqual(config._interval_minutes("30"), 30)


class TestNoGlobalSocketTimeout(unittest.TestCase):
    def test_no_process_wide_socket_default(self):
        import reporting.logger  # noqa: F401 — importing must not set a default
        self.assertIsNone(socket.getdefaulttimeout())


class TestLLMRetry(unittest.TestCase):
    def _fake_litellm(self, errors):
        calls = []

        class FakeLiteLLM:
            def completion(self, **kwargs):
                calls.append(kwargs)
                if errors:
                    raise errors.pop(0)
                return "response"
        return FakeLiteLLM(), calls

    def test_server_errors_are_retried(self):
        from agent import llm_client
        fake, calls = self._fake_litellm([RuntimeError("500 Internal Server Error"),
                                          RuntimeError("502 Bad Gateway")])
        with mock.patch.object(llm_client, "litellm", fake), \
             mock.patch.object(llm_client.time, "sleep", lambda s: None):
            self.assertEqual(llm_client._complete(model="m"), "response")
        self.assertEqual(len(calls), 3)

    def test_auth_errors_raise_immediately(self):
        from agent import llm_client
        fake, calls = self._fake_litellm([RuntimeError("401 invalid api key")])
        with mock.patch.object(llm_client, "litellm", fake), \
             mock.patch.object(llm_client.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                llm_client._complete(model="m")
        self.assertEqual(len(calls), 1)


class TestGracefulShutdown(unittest.TestCase):
    def test_signal_handler_sets_the_event_instead_of_exiting(self):
        import services
        self.addCleanup(main._shutdown_event.clear)
        with mock.patch.object(services, "flush") as flush:
            main.handle_shutdown(15, None)
        self.assertTrue(main._shutdown_event.is_set())
        flush.assert_called_once()
        self.assertGreaterEqual(flush.call_args.kwargs.get("timeout", 0), 10.0)


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.mock = MockedCycle()
        self.mock.reset_state()
        self.addCleanup(self.mock.restore)
        import os
        if os.path.exists(config.HEARTBEAT_FILE):
            os.remove(config.HEARTBEAT_FILE)

    def _heartbeat_exists(self):
        import os
        return os.path.exists(config.HEARTBEAT_FILE)

    def test_successful_cycle_touches_the_heartbeat(self):
        self.mock.install(powerwall(), charger())
        main.run_cycle()
        self.assertTrue(self._heartbeat_exists())

    def test_failed_cycle_does_not_touch_the_heartbeat(self):
        self.mock.install(powerwall(), charger())

        def broken():
            raise RuntimeError("telemetry down")
        self.mock._patch(main, "get_powerwall_stats", broken)

        main.run_cycle()
        self.assertFalse(self._heartbeat_exists())


if __name__ == "__main__":
    unittest.main()
