import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import unittest
from datetime import date, datetime
from unittest import mock

import reporting.csv_logger as csv_logger
from core.tou import get_tou_rate
from reporting import energy_history, weekly_report


def _row(ts, charger_state, meter_kwh, solar_kw=0.0, battery_kw=0.0, home_kw=2.7, action="charging"):
    return {"timestamp": ts, "date": ts[:10], "charger_state": charger_state, "action": action,
            "charger_amperage": "32", "solar_kw": str(solar_kw), "battery_kw": str(battery_kw),
            "home_kw": str(home_kw), "grid_kw": "2.7", "cp_session_energy_kwh": str(meter_kwh),
            "session_active_minutes": "15"}


# One session on Wednesday 2026-11-11 at noon: 1.9 kWh (0.75 solar, 1.15 grid), then 1.9 kWh from the Powerwall.
ROWS = [
    _row("2026-11-11T12:00:00", "CHARGING", 0.0, action="start"),
    _row("2026-11-11T12:15:00", "CHARGING", 1.9, solar_kw=3.0),
    _row("2026-11-11T12:30:00", "CHARGING", 3.8, battery_kw=8.0),
    _row("2026-11-11T12:45:00", "IDLE", 3.8, home_kw=0.5, action="idle"),
]


def _day(home, solar, export):
    return {"home_kwh": home, "solar_kwh": solar, "solar_export_kwh": export}


class TestWeeklyReport(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(csv_logger, "get_all_log_rows", return_value=ROWS),
            mock.patch.object(energy_history, "daily_totals",
                              side_effect=lambda start, end: {start: _day(12.0, 20.0, 5.0)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_car_totals_and_whole_home_estimate(self):
        s = weekly_report.week_summary(date(2026, 11, 9), date(2026, 11, 15))
        car = s["car"]
        self.assertAlmostEqual(car["kwh"], 3.8)
        self.assertAlmostEqual(car["solar_kwh"], 0.75)
        self.assertAlmostEqual(car["battery_kwh"], 1.9)
        self.assertAlmostEqual(car["grid_kwh"], 1.15)
        self.assertAlmostEqual(car["cost"], 1.15 * get_tou_rate(datetime(2026, 11, 11, 12, 15)))
        self.assertEqual(len(s["sessions"]), 1)
        # Tesla's home reading during the 3 charging slots (2.7 kW x 0.25 h each) is swapped for the car's own kWh.
        self.assertAlmostEqual(s["house_kwh"], 12.0 - 3 * 2.7 * 0.25)
        self.assertAlmostEqual(s["home_kwh"], s["house_kwh"] + 3.8)

    def test_report_text(self):
        with mock.patch.object(csv_logger, "datetime", wraps=datetime) as dt:
            dt.now.return_value = datetime(2026, 11, 16, 7, 5)
            text = weekly_report.weekly_report_text("last_week")
        self.assertIn("Nov 9 – Nov 15", text)
        self.assertIn("1 session · 3.8 kWh", text)
        self.assertIn("Whole home ≈ <b>14 kWh</b>", text)
        self.assertIn("vs previous week: +3.8 kWh", text)

    def test_week_without_charging(self):
        s = weekly_report.week_summary(date(2026, 11, 16), date(2026, 11, 22))
        self.assertEqual(s["car"]["kwh"], 0.0)
        self.assertEqual(s["home_kwh"], 12.0)


if __name__ == "__main__":
    unittest.main()
