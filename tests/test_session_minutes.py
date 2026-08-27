import unittest
from datetime import datetime, timedelta

import main
from core import config, state
from core.state import get_session_minutes
from tests.helpers import MockedCycle, charger, powerwall


class TestSessionMinutes(unittest.TestCase):
    def setUp(self):
        self.mock = MockedCycle()
        self.mock.reset_state()
        # Notifications and save are patched at source for the modules under test.
        config.save_dynamic_config = lambda: None
        self.addCleanup(self.mock.restore)

    def test_zero_when_idle(self):
        state.charge_session_start = None
        self.assertEqual(get_session_minutes(), 0.0)

    def test_elapsed_minutes_while_charging(self):
        state.charge_session_start = datetime.now(config.TZ) - timedelta(minutes=30)
        state.charger_state = state.State.CHARGING
        self.assertAlmostEqual(get_session_minutes(), 30.0, delta=0.2)

    def test_stop_clears_the_session(self):
        """A stop must reset charge_session_start so minutes fall back to 0."""
        now = datetime.now(config.TZ)
        state.charger_state = state.State.CHARGING
        state.charge_session_start = now - timedelta(minutes=45)
        self.mock.install(powerwall(battery_pct=24.0, grid_kw=0.5),
                          charger(charging=True, session_start=now - timedelta(minutes=45)))
        main.run_cycle()

        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)
        self.assertEqual(get_session_minutes(), 0.0)

    def test_idle_cycles_stay_at_zero(self):
        self.mock.install(powerwall(battery_pct=25.0), charger(charging=False))
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertEqual(get_session_minutes(), 0.0)

    def test_adopts_externally_started_session(self):
        """A charge started outside the app must be adopted with its real start time."""
        now = datetime.now(config.TZ)
        self.mock.install(powerwall(battery_pct=60.0),
                          charger(charging=True, session_start=now - timedelta(minutes=15)))
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.CHARGING)
        self.assertIsNotNone(state.charge_session_start)
        self.assertAlmostEqual(get_session_minutes(), 15.0, delta=0.5)

    def test_detects_externally_stopped_session(self):
        # 30% sits between the stop (25%) and start (40%) thresholds, so the
        # decision engine holds and the sync result is what is under test.
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ) - timedelta(minutes=20)
        self.mock.install(powerwall(battery_pct=30.0), charger(charging=False))
        main.run_cycle()
        self.assertEqual(state.charger_state, state.State.IDLE)
        self.assertIsNone(state.charge_session_start)
        self.assertEqual(get_session_minutes(), 0.0)


if __name__ == "__main__":
    unittest.main()
