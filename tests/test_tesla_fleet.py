"""Tesla Fleet API: token rotation, call budget, NetZero fallback, metered history and bills."""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import requests

from core import config, tou
from reporting import csv_logger, energy_history
from services import powerwall, tesla_fleet

LIVE = {"solar_power": 480, "load_power": 1450.3, "grid_power": 970.3, "battery_power": 0,
        "percentage_charged": 10.57, "island_status": "on_grid", "storm_mode_active": False,
        "timestamp": "2026-11-16T11:54:53-08:00"}


def _response(status=200, body=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    if status >= 400:
        err = requests.HTTPError(f"{status}")
        err.response = resp
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _token(n: int) -> dict:
    return {"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": 28800}


class _TeslaCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patches = {
            "TESLA_TOKEN_FILE": os.path.join(self.dir, "tesla_fleet.json"),
            "ENERGY_HISTORY_FILE": os.path.join(self.dir, "energy_history.csv"),
            "TESLA_CLIENT_ID": "client",
            "TESLA_REFRESH_TOKEN": "seed-1",
            "TESLA_ENERGY_SITE_ID": "123",
            "TESLA_MONTHLY_CALL_BUDGET": 4500,
            "NETZERO_SITE_ID": "",
            "NETZERO_API_TOKEN": "",
        }
        for key, value in patches.items():
            p = mock.patch.object(config, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tesla_fleet.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        powerwall._last_alert.clear()

    def store(self) -> dict:
        with open(config.TESLA_TOKEN_FILE) as f:
            return json.load(f)


class TestTokenRotation(_TeslaCase):
    def test_seed_is_used_once_and_rotated_token_is_saved(self):
        with mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(2))) as post, \
             mock.patch.object(tesla_fleet._session, "get", return_value=_response(body={"response": LIVE})):
            stats = tesla_fleet.get_powerwall_stats()
            tesla_fleet.get_powerwall_stats()  # access token still fresh: no second refresh
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "seed-1")
        self.assertEqual(self.store()["refresh_token"], "refresh-2")
        self.assertEqual(stats["solar_kw"], 0.48)
        self.assertEqual(stats["grid_kw"], 0.97)
        self.assertEqual(stats["data_source"], "Tesla")

    def test_next_refresh_uses_the_rotated_token_not_the_seed(self):
        with mock.patch.object(tesla_fleet._session, "post",
                               side_effect=[_response(body=_token(2)), _response(body=_token(3))]) as post:
            tesla_fleet._access_token(tesla_fleet._store(), force=False)
            tesla_fleet._access_token(tesla_fleet._store(), force=True)
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "refresh-2")
        self.assertEqual(self.store()["refresh_token"], "refresh-3")

    def test_a_new_seed_in_env_replaces_the_chain(self):
        with mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(2))):
            tesla_fleet._access_token(tesla_fleet._store(), force=False)
        with mock.patch.object(config, "TESLA_REFRESH_TOKEN", "seed-NEW"), \
             mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(9))) as post:
            tesla_fleet._access_token(tesla_fleet._store(), force=False)
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "seed-NEW")

    def test_fresh_container_restores_the_rotated_token_from_sheets(self):
        sheet = {}
        with mock.patch.object(tesla_fleet.sheets_db, "is_disabled", return_value=False), \
             mock.patch.object(tesla_fleet.sheets_db, "update_settings",
                               side_effect=lambda patch, blocking=False: sheet.update(patch)), \
             mock.patch.object(tesla_fleet.sheets_db, "get_settings", side_effect=lambda **_: dict(sheet)):
            with mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(2))):
                tesla_fleet._access_token(tesla_fleet._store(), force=False)
            os.remove(config.TESLA_TOKEN_FILE)  # a new image starts with an empty logs/
            with mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(3))) as post:
                tesla_fleet._access_token(tesla_fleet._store(), force=True)
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "refresh-2")
        self.assertEqual(json.loads(sheet[tesla_fleet.SHEETS_KEY])["refresh_token"], "refresh-3")

    def test_401_refreshes_once_and_retries(self):
        with mock.patch.object(tesla_fleet._session, "post",
                               side_effect=[_response(body=_token(2)), _response(body=_token(3))]), \
             mock.patch.object(tesla_fleet._session, "get",
                               side_effect=[_response(401), _response(body={"response": LIVE})]) as get:
            tesla_fleet.get_powerwall_stats()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer access-3")

    def test_rejected_refresh_token_raises_auth_error(self):
        with mock.patch.object(tesla_fleet._session, "post", return_value=_response(400)):
            with self.assertRaises(tesla_fleet.TeslaAuthError):
                tesla_fleet._access_token(tesla_fleet._store(), force=False)


class TestBudgetAndFallback(_TeslaCase):
    def test_calls_are_counted_and_the_budget_stops_them(self):
        with mock.patch.object(config, "TESLA_MONTHLY_CALL_BUDGET", 2), \
             mock.patch.object(tesla_fleet._session, "post", return_value=_response(body=_token(2))), \
             mock.patch.object(tesla_fleet._session, "get", return_value=_response(body={"response": LIVE})) as get:
            tesla_fleet.get_powerwall_stats()
            tesla_fleet.get_powerwall_stats()
            with self.assertRaises(tesla_fleet.TeslaBudgetError):
                tesla_fleet.get_powerwall_stats()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(tesla_fleet.calls_this_month(), 2)

    def test_budget_falls_back_to_netzero(self):
        with mock.patch.object(config, "TESLA_MONTHLY_CALL_BUDGET", 0), \
             mock.patch.object(config, "NETZERO_SITE_ID", "site"), \
             mock.patch.object(config, "NETZERO_API_TOKEN", "token"), \
             mock.patch.object(powerwall.netzero, "get_powerwall_stats", return_value={"src": "nz"}):
            self.assertEqual(powerwall.get_powerwall_stats(), {"src": "nz"})

    def test_auth_failure_alerts_once_and_falls_back(self):
        sent = []
        with mock.patch.object(config, "NETZERO_SITE_ID", "site"), \
             mock.patch.object(config, "NETZERO_API_TOKEN", "token"), \
             mock.patch.object(powerwall.netzero, "get_powerwall_stats", return_value={"src": "nz"}), \
             mock.patch.object(tesla_fleet._session, "post", return_value=_response(401)), \
             mock.patch("reporting.notifications.notify", sent.append):
            powerwall.get_powerwall_stats()
            powerwall.get_powerwall_stats()
        self.assertEqual(len(sent), 1)
        self.assertIn("Tesla sign-in needed", sent[0])

    def test_without_tesla_config_netzero_is_used(self):
        with mock.patch.object(config, "TESLA_REFRESH_TOKEN", ""), \
             mock.patch.object(powerwall.netzero, "get_powerwall_stats", return_value={"src": "nz"}):
            self.assertEqual(powerwall.get_powerwall_stats(), {"src": "nz"})
            self.assertEqual(powerwall.active_source(), "netzero")


def _interval(when: datetime, **wh) -> dict:
    entry = {"timestamp": when.isoformat(), "grid_energy_imported": 0, "solar_energy_exported": 0,
             "grid_energy_exported_from_solar": 0, "grid_energy_exported_from_battery": 0,
             "battery_energy_exported": 0, "total_home_usage": 0}
    entry.update(wh)
    return entry


# A Wednesday, not a holiday.
DAY = date(2026, 11, 4)  # in the past, so it is never "today" (which counts while partial)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=config.TZ)


class TestEnergyHistory(_TeslaCase):
    def _full_day(self, day=DAY, on_peak_wh=1000, off_peak_wh=2000, export_wh=500):
        start = _midnight(day)
        entries = [_interval(start + timedelta(minutes=5 * i), total_home_usage=100) for i in range(288)]
        entries[2 * 12] = _interval(start.replace(hour=2), grid_energy_imported=off_peak_wh)
        entries[17 * 12] = _interval(start.replace(hour=17), grid_energy_imported=on_peak_wh)
        entries[12 * 12] = _interval(start.replace(hour=12), solar_energy_exported=4000,
                                     grid_energy_exported_from_solar=export_wh)
        return entries

    def test_daily_totals_price_each_interval_at_its_own_tou_rate(self):
        energy_history.store(self._full_day())
        totals = energy_history.daily_totals(DAY, DAY)[DAY]
        self.assertAlmostEqual(totals["on_peak_grid_kwh"], 1.0)
        self.assertAlmostEqual(totals["off_peak_grid_kwh"], 2.0)
        self.assertAlmostEqual(totals["solar_export_kwh"], 0.5)
        self.assertAlmostEqual(totals["solar_kwh"], 4.0)
        expected = (1.0 * tou.get_tou_rate(_midnight(DAY).replace(hour=17))
                    + 2.0 * tou.get_tou_rate(_midnight(DAY).replace(hour=2)))
        self.assertAlmostEqual(totals["variable_grid_cost"], expected)

    def test_export_uses_tesla_total_over_the_attributed_split(self):
        entry = _interval(_midnight(DAY).replace(hour=12), total_grid_energy_exported=2000,
                          grid_energy_exported_from_solar=500)
        self.assertEqual(energy_history.normalise(entry)["grid_export_wh"], 2000)

    def test_storing_twice_does_not_duplicate(self):
        energy_history.store(self._full_day())
        self.assertEqual(energy_history.store(self._full_day()), 0)
        self.assertAlmostEqual(energy_history.daily_totals(DAY, DAY)[DAY]["grid_import_kwh"], 3.0)

    def test_partial_day_is_not_counted_as_metered(self):
        energy_history.store(self._full_day()[:200])  # stops before 23:00
        self.assertEqual(energy_history.daily_totals(DAY, DAY), {})

    def test_plan_uses_one_call_per_month_and_day_calls_for_single_gaps(self):
        today = date(2026, 11, 16)
        have = {date(2026, 10, 1) + timedelta(days=i) for i in range(31)} - {date(2026, 10, 7)}
        plan = energy_history.plan_fetches(today, have, months=2)
        self.assertEqual(plan, [
            (date(2026, 9, 1), date(2026, 9, 30)),     # whole month missing: one call
            (date(2026, 10, 7), date(2026, 10, 7)),    # single gap: day call
            (date(2026, 11, 1), date(2026, 11, 15)),   # this month to yesterday
        ])

    def test_nothing_missing_means_no_calls(self):
        today = date(2026, 11, 16)
        have = {date(2026, 11, 1) + timedelta(days=i) for i in range(15)}
        self.assertEqual(energy_history.plan_fetches(today, have, months=0), [])

    def test_nightly_sync_is_one_call_for_yesterday_even_if_partly_stored(self):
        yesterday = DAY - timedelta(days=1)
        with mock.patch.object(config, "ENERGY_HISTORY_BACKFILL_MONTHS", 0), \
             mock.patch.object(energy_history, "metered_days",
                               return_value={DAY.replace(day=d) for d in range(1, DAY.day)}), \
             mock.patch.object(tesla_fleet, "get_energy_intervals",
                               return_value=self._full_day(yesterday)) as fetch:
            result = energy_history.sync(now=_midnight(DAY).replace(hour=0, minute=30))
        fetch.assert_called_once_with(yesterday, yesterday)
        self.assertEqual(result["calls"], 1)


    def test_today_is_fetched_on_demand_at_most_hourly(self):
        today = datetime.now(config.TZ).date()
        with mock.patch.object(energy_history, "_today_fetched_at", None), \
             mock.patch.object(tesla_fleet, "get_energy_intervals", return_value=[]) as fetch:
            energy_history.daily_totals(today, today)
            energy_history.daily_totals(today - timedelta(days=3), today)
            energy_history.daily_totals(today - timedelta(days=3), today - timedelta(days=1))
        fetch.assert_called_once_with(today, today)


class TestBillsUseMeteredDays(_TeslaCase):
    def test_bill_breakdown_prefers_metered_history(self):
        energy_history.store(self._day())
        with mock.patch.object(csv_logger, "_rows_since", return_value=[{"date": str(DAY)}]), \
             mock.patch.object(csv_logger, "_readings", return_value=iter([])):
            bill = csv_logger.get_bill_breakdown(str(DAY))
        self.assertEqual(bill["grid_import_kwh"], 3.0)
        self.assertEqual(bill["data_note"], "Metered by Tesla.")
        energy = {line["label"]: line["detail"] for line in bill["groups"][0]["lines"]}
        self.assertEqual(energy["On-peak"], "1.0 kWh" if config._IS_MID else "1.0 kWh, incl. taxes")

    def test_home_summary_prefers_metered_history(self):
        energy_history.store(self._day())
        with mock.patch.object(csv_logger, "_rows_since", return_value=[{"date": str(DAY)}]), \
             mock.patch.object(csv_logger, "_ev_intervals_between", return_value=[]):
            summary = csv_logger.get_home_energy_summary(str(DAY))
        self.assertEqual(summary["total_grid_imported_kwh"], 3.0)
        self.assertEqual(summary["total_solar_exported_kwh"], 0.5)
        self.assertEqual(summary["data_note"], "Metered by Tesla.")

    @staticmethod
    def _day():
        return TestEnergyHistory._full_day(None)


if __name__ == "__main__":
    unittest.main()
