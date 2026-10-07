import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import dashboard.data as dashboard_data
import dashboard.server as dashboard_server
from core import config, state
from reporting import csv_logger
from tests.helpers import MockedCycle, charger, powerwall, seed_telemetry


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.mock = MockedCycle()
        self.mock.reset_state()
        self.addCleanup(self.mock.restore)
        self.rows = seed_telemetry(days_back_start=0, days=1)
        self.row_reads = 0

        def rows(days=7, force_refresh=False):
            self.row_reads += 1
            return self.rows
        self.mock._patch(csv_logger, "get_all_log_rows", rows)
        self.mock._patch(dashboard_data, "_live", None)
        self.mock._patch(dashboard_data, "_summary", None)
        self.mock._patch(dashboard_data, "_summary_key", None)


class TestDashboardData(DashboardTestCase):
    def test_live_readings_and_today_totals(self):
        now = datetime.now(config.TZ)
        dashboard_data.record_live(powerwall(battery_pct=72.0, solar_kw=5.5, home_kw=1.5), charger(), now)
        data = dashboard_data.get_dashboard_data()

        self.assertEqual(data["power"]["battery_pct"], 72.0)
        self.assertEqual(data["power"]["solar_kw"], 5.5)
        self.assertEqual(data["updated_at"], now.isoformat())
        self.assertFalse(data["ev"]["charging"])
        self.assertTrue(data["ev"]["plugged_in"])
        self.assertGreater(data["energy_today"]["solar_kwh"], 0)
        self.assertGreater(data["energy_today"]["home_kwh"], 0)
        self.assertEqual(data["ev"]["today"]["sessions"], 1)
        self.assertGreater(data["ev"]["today"]["miles"], 0)
        self.assertEqual(len(data["hourly"]), 24)
        json.dumps(data, default=str)  # must be serialisable for the API

    def test_active_session_uses_charger_meter(self):
        now = datetime.now(config.TZ)
        start = now - timedelta(minutes=40)
        state.begin_session(start, 32)
        cp = charger(charging=True, amperage=32, session_start=start)
        cp.update(energy_kwh=5.1, miles_added=18.0, power_kw=7.6)
        dashboard_data.record_live(powerwall(), cp, now)

        session = dashboard_data.get_dashboard_data()["ev"]["session"]
        self.assertEqual(session["energy_kwh"], 5.1)
        self.assertEqual(session["miles"], 18.0)
        self.assertEqual(session["amperage"], 32)
        self.assertAlmostEqual(session["minutes"], 40, delta=1)

    def test_refresh_does_not_reread_telemetry_until_a_new_row(self):
        dashboard_data.record_live(powerwall(), charger(), datetime.now(config.TZ))
        dashboard_data.get_dashboard_data()
        reads = self.row_reads
        for _ in range(5):
            dashboard_data.get_dashboard_data()
        self.assertEqual(self.row_reads, reads)

        csv_logger.log_to_csv(powerwall(), "hold", "test", datetime.now(config.TZ))
        dashboard_data.get_dashboard_data()
        self.assertGreater(self.row_reads, reads)

    def test_falls_back_to_last_logged_row_after_restart(self):
        data = dashboard_data.get_dashboard_data()
        self.assertEqual(data["power"]["battery_pct"], 55.0)
        self.assertEqual(data["updated_at"], self.rows[-1]["timestamp"])

    def test_cycle_records_live_readings(self):
        import main
        self.mock.install(powerwall(battery_pct=33.0), charger())
        main.run_cycle()
        self.assertEqual(dashboard_data._live["stats"]["battery_pct"], 33.0)


class TestRecentSessionEnergy(DashboardTestCase):
    def test_sessions_report_energy_and_miles(self):
        session = csv_logger.get_recent_sessions(limit=1)[0]
        # 60 minutes at 20 A x 240 V = 4.8 kWh
        self.assertAlmostEqual(session["energy_kwh"], 4.8, places=2)
        self.assertAlmostEqual(session["miles_added"], round(4.8 * config.EV_MILES_PER_KWH, 1))

    def test_metered_energy_wins_over_estimate(self):
        for row in self.rows:
            if row["charger_state"] == "CHARGING":
                row["cp_session_energy_kwh"] = "6.25"
        self.assertEqual(csv_logger.get_recent_sessions(limit=1)[0]["energy_kwh"], 6.25)


class TestBillBreakdown(DashboardTestCase):
    def test_mid_itemised_bill_adds_up(self):
        bill = csv_logger.get_bill_breakdown("today")
        energy, fixed, credits = bill["groups"]
        self.assertEqual([g["title"] for g in bill["groups"]], ["Energy used", "Fixed charges & taxes", "Credits"])
        self.assertEqual(fixed["lines"][0]["amount"], round(config.UTILITY_FIXED_MONTHLY_FEE / 30.0, 2))
        self.assertGreater(fixed["lines"][-1]["amount"], 0)  # local surcharge applied
        self.assertLess(credits["subtotal"], 0)
        self.assertAlmostEqual(sum(g["subtotal"] for g in bill["groups"]), bill["total_dollars"], delta=0.02)

    def test_reports_actual_usage_without_scaling_a_partial_day(self):
        full = csv_logger.get_bill_breakdown("today")["grid_import_kwh"]
        self.rows = self.rows[: len(self.rows) // 2]
        half = csv_logger.get_bill_breakdown("today")["grid_import_kwh"]
        self.assertLess(half, full)

    def test_dashboard_shows_cost_with_fixed_charges_and_credit(self):
        bill = csv_logger.get_bill_breakdown("today")
        energy = dashboard_data.get_dashboard_data()["energy_today"]
        self.assertEqual(energy["energy_cost"], bill["groups"][0]["subtotal"])
        self.assertEqual(energy["fixed_charges"], bill["groups"][1]["subtotal"])
        self.assertEqual(energy["solar_credit"], -bill["groups"][2]["subtotal"])


class TestDashboardServer(DashboardTestCase):
    def setUp(self):
        super().setUp()
        dashboard_data.record_live(powerwall(), charger(), datetime.now(config.TZ))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server._Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as res:
            return res.status, res.read()

    def test_serves_page_and_api(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Home Energy", body)
        status, body = self.get("/api/dashboard")
        self.assertEqual(json.loads(body)["power"]["battery_pct"], 50.0)

    def test_token_is_required_when_configured(self):
        self.mock._patch(config, "DASHBOARD_TOKEN", "s3cret")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/dashboard")
        self.assertEqual(ctx.exception.code, 401)
        status, _ = self.get("/api/dashboard?token=s3cret")
        self.assertEqual(status, 200)

    def test_home_screen_manifest_and_icons(self):
        _, body = self.get("/manifest.webmanifest")
        manifest = json.loads(body)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "./")
        for icon in manifest["icons"] + [{"src": "icon-180.png"}]:
            status, png = self.get("/" + icon["src"])
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_home_screen_app_keeps_the_token(self):
        self.mock._patch(config, "DASHBOARD_TOKEN", "s3cret")
        _, page = self.get("/?token=s3cret")
        self.assertIn(b'href="manifest.webmanifest?token=s3cret"', page)
        _, body = self.get("/manifest.webmanifest?token=s3cret")
        self.assertEqual(json.loads(body)["start_url"], "./?token=s3cret")
        status, _ = self.get("/icon-192.png")  # icons load without the token
        self.assertEqual(status, 200)
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/manifest.webmanifest")

    def test_disabled_when_port_is_zero(self):
        self.mock._patch(config, "DASHBOARD_PORT", 0)
        self.assertIsNone(dashboard_server.start_dashboard_server())


if __name__ == "__main__":
    unittest.main()
