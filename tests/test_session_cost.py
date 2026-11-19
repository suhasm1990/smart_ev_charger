import unittest
from datetime import datetime
from unittest import mock

import main
import reporting.csv_logger as csv_logger
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)
from core import state
from core.tou import get_tou_rate


def _row(ts, charger_state, meter_kwh, solar_kw=0.0, battery_kw=0.0, action="charging"):
    return {"timestamp": ts, "date": ts[:10], "charger_state": charger_state, "action": action,
            "charger_amperage": "32", "solar_kw": str(solar_kw), "battery_kw": str(battery_kw),
            "home_kw": "2.7", "grid_kw": "2.7", "cp_session_energy_kwh": str(meter_kwh),
            "session_active_minutes": "15"}


class TestSessionCost(unittest.TestCase):
    def test_grid_share_is_priced_at_each_slots_rate(self):
        rows = [
            _row("2026-11-18T12:00:00", "CHARGING", 0.0, action="start"),
            _row("2026-11-18T12:15:00", "CHARGING", 1.9, solar_kw=3.0),           # 0.75 solar, 1.15 grid
            _row("2026-11-18T12:30:00", "CHARGING", 3.8, battery_kw=8.0),         # all Powerwall
            _row("2026-11-18T12:45:00", "IDLE", 3.8, action="idle"),
        ]
        with mock.patch.object(csv_logger, "get_all_log_rows", return_value=rows):
            s = csv_logger.get_recent_sessions(limit=1)[0]
        self.assertAlmostEqual(s["energy_kwh"], 3.8)
        self.assertAlmostEqual(s["grid_kwh"], 1.15)
        self.assertAlmostEqual(s["cost"], round(1.15 * get_tou_rate(datetime(2026, 11, 18, 12, 15)), 2))

    def test_cost_is_posted_once_when_charging_ends(self):
        session = {"energy_kwh": 7.6, "grid_kwh": 7.6, "cost": 1.23}
        with mock.patch.object(main, "get_recent_sessions", return_value=[session]), \
             mock.patch.object(main, "get_last_session_kwh", return_value=0.0), \
             mock.patch.object(main.sheets_db, "flush"), mock.patch.object(main, "notify") as notify:
            main._was_charging = False
            for charger_state in (state.State.CHARGING, state.State.CHARGING, state.State.IDLE, state.State.IDLE):
                state.charger_state = charger_state
                main._report_session_cost()
        notify.assert_called_once()
        self.assertIn("$1.23", notify.call_args[0][0])

    def test_tail_after_the_last_reading_uses_chargepoints_final_total(self):
        session = {"energy_kwh": 6.0, "grid_kwh": 3.0, "cost": 0.60}
        with mock.patch.object(main, "get_last_session_kwh", return_value=7.5):
            s = main._with_final_kwh(session)
        self.assertEqual((s["energy_kwh"], s["grid_kwh"]), (7.5, 3.75))
        self.assertAlmostEqual(s["cost"], 0.75)

    def test_final_total_is_ignored_when_chargepoint_fails(self):
        session = {"energy_kwh": 6.0, "grid_kwh": 3.0, "cost": 0.60}
        with mock.patch.object(main, "get_last_session_kwh", side_effect=RuntimeError("down")):
            self.assertIs(main._with_final_kwh(session), session)


if __name__ == "__main__":
    unittest.main()
