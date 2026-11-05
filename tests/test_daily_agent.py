"""Morning planner and miles goals: prompt context, threshold validation, and goal maths."""
import tests  # noqa: F401  -- must stay first: isolates Sheets and log files (see tests/__init__.py)

# isort: split
import json
import unittest
from datetime import datetime, timedelta

import main
from agent import daily_agent
from core import config
from core import goal as charge_goal

WEEKDAY = datetime(2026, 10, 19, 7, 0, tzinfo=config.TZ)   # Monday

PROFILE = [
    {"hour": 9, "solar_kw": 2.0, "house_kw": 0.8},
    {"hour": 12, "solar_kw": 6.0, "house_kw": 1.0},
    {"hour": 18, "solar_kw": 0.0, "house_kw": 2.5},
]
NO_HISTORY = {"hourly_profile_7_days": PROFILE, "error": "No log data found yet."}


class PlannerTestCase(unittest.TestCase):
    KEYS = ("NIGHT_BLACKOUT_START_HOUR", "NIGHT_BLACKOUT_END_HOUR", "BATTERY_START_PCT",
            "BATTERY_STOP_PCT", "BATTERY_LOW_RESERVE_PCT",
            "ALLOWED_CHARGE_START_HOUR", "ALLOWED_CHARGE_END_HOUR")

    def setUp(self):
        self._saved = {key: getattr(config, key) for key in self.KEYS}
        self._update = config.update
        config.update = lambda **changes: config._apply(changes)
        config._apply({"NIGHT_BLACKOUT_START_HOUR": 0, "NIGHT_BLACKOUT_END_HOUR": 0,
                       "ALLOWED_CHARGE_START_HOUR": 0, "ALLOWED_CHARGE_END_HOUR": 24,
                       "BATTERY_LOW_RESERVE_PCT": 15.0})

    def tearDown(self):
        config.update = self._update
        config._apply(self._saved)


class TestPrompt(PlannerTestCase):
    def test_prompt_lists_every_hour_and_marks_missing_history(self):
        prompt = daily_agent.build_prompt(WEEKDAY, NO_HISTORY, {}, "")
        self.assertIn("12:00 | off-peak", prompt)
        self.assertIn("+5.0", prompt)  # 12:00 surplus
        self.assertIn("evening history unavailable", prompt)
        self.assertIn("Yesterday's totals unavailable", prompt)
        self.assertIn("Monday 2026-10-19", prompt)
        self.assertNotIn("forecast", prompt.lower())
        self.assertIn("ending by 17:00", prompt)

    def test_prompt_includes_the_miles_goal(self):
        prompt = daily_agent.build_prompt(WEEKDAY, NO_HISTORY, {}, "", goal="40 of 100 miles added")
        self.assertIn("Miles goal: 40 of 100 miles added", prompt)

    def test_ranges_merge_contiguous_hours(self):
        self.assertEqual(daily_agent._format_ranges([9, 10, 11, 14]), "09:00-12:00, 14:00-15:00")


class TestApplyPlanValidation(PlannerTestCase):
    def test_window_is_applied_and_manual_blackout_cleared(self):
        config._apply({"NIGHT_BLACKOUT_START_HOUR": 0, "NIGHT_BLACKOUT_END_HOUR": 16})
        applied = daily_agent._apply_plan({"battery_start_pct": 55.0, "battery_stop_pct": 40.0,
                                           "charge_window_start_hour": 10, "charge_window_end_hour": 15})
        self.assertEqual((config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR), (0, 0))
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR), (10, 15))
        self.assertIn("NIGHT_BLACKOUT cleared (was 0:00-16:00)", applied)
        self.assertIn("ALLOWED_CHARGE_WINDOW=10-15", applied)

    def test_window_never_reaches_into_on_peak_or_night(self):
        daily_agent._apply_plan({"battery_start_pct": 55.0, "battery_stop_pct": 40.0,
                                 "charge_window_start_hour": 2, "charge_window_end_hour": 22})
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR), (6, 17))

    def test_missing_window_falls_back_to_midday(self):
        daily_agent._apply_plan({"battery_start_pct": 55.0, "battery_stop_pct": 40.0})
        self.assertEqual((config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR),
                         daily_agent.DEFAULT_WINDOW)

    def test_thresholds_clamped_to_prompt_bounds_and_gap(self):
        daily_agent._apply_plan({"battery_start_pct": 95.0, "battery_stop_pct": 5.0})
        self.assertEqual(config.BATTERY_STOP_PCT, 20.0)
        self.assertEqual(config.BATTERY_START_PCT, 80.0)

    def test_stop_never_below_critical_reserve(self):
        config._apply({"BATTERY_LOW_RESERVE_PCT": 30.0})
        daily_agent._apply_plan({"battery_start_pct": 60.0, "battery_stop_pct": 22.0})
        self.assertEqual(config.BATTERY_STOP_PCT, 30.0)

    def test_start_too_close_to_stop_is_widened(self):
        daily_agent._apply_plan({"battery_start_pct": 48.0, "battery_stop_pct": 45.0})
        self.assertEqual(config.BATTERY_START_PCT, 55.0)


class TestMilesGoal(unittest.TestCase):
    NOW = datetime(2026, 11, 5, 20, 0, tzinfo=config.TZ)

    def setUp(self):
        import reporting.csv_logger as csv_logger
        import services.sheets_db as sheets_db
        self.store = {}
        self.notes = []
        self.added_kwh = 0.0
        patches = [
            (sheets_db, "get_settings", lambda force_refresh=False: dict(self.store)),
            (sheets_db, "save_charge_goal", lambda raw: self.store.update(CHARGE_GOAL=raw) or True),
            (sheets_db, "clear_charge_goal", lambda: self.store.pop("CHARGE_GOAL", None) is not None),
            (csv_logger, "get_ev_kwh_since", lambda since: self.added_kwh),
            (csv_logger, "get_energy_saving_advice", lambda: {"hourly_profile_7_days": PROFILE}),
            (main, "notify", self.notes.append),
        ]
        self._originals = [(module, name, getattr(module, name)) for module, name, _ in patches]
        for module, name, fake in patches:
            setattr(module, name, fake)

    def tearDown(self):
        for module, name, original in self._originals:
            setattr(module, name, original)

    def test_rejects_past_and_far_deadlines(self):
        with self.assertRaises(ValueError):
            charge_goal.set_goal(100, self.NOW - timedelta(hours=1), self.NOW)
        with self.assertRaises(ValueError):
            charge_goal.set_goal(100, self.NOW + timedelta(days=8), self.NOW)

    def test_round_trips_through_settings(self):
        charge_goal.set_goal(100, self.NOW + timedelta(days=1), self.NOW)
        self.assertEqual(json.loads(self.store["CHARGE_GOAL"])["miles"], 100)
        self.assertEqual(charge_goal.get_goal()["deadline"], self.NOW + timedelta(days=1))

    def test_expected_solar_counts_only_hours_before_the_deadline(self):
        # 20:00 tonight to 23:59 tomorrow covers one 09:00 (+1.2) and one 12:00 (+5.0, capped at 4.8).
        deadline = self.NOW.replace(hour=23, minute=59) + timedelta(days=1)
        self.assertEqual(charge_goal.expected_solar_kwh(self.NOW, deadline, PROFILE, charger_kw=4.8), 6.0)

    def test_night_need_is_what_solar_leaves_uncovered(self):
        goal = charge_goal.set_goal(100, self.NOW.replace(hour=23, minute=59) + timedelta(days=1), self.NOW)
        remaining = charge_goal.progress(goal)["remaining_kwh"]  # 100 mi / 3.53 mi/kWh = 28.33 kWh
        self.assertAlmostEqual(charge_goal.night_kwh_needed(self.NOW, goal, remaining), 22.33, places=1)

    def test_cycle_clears_a_met_goal_and_notifies(self):
        charge_goal.set_goal(10, self.NOW + timedelta(days=1), self.NOW)
        self.added_kwh = 5.0  # 17.6 miles >= 10
        self.assertEqual(main._goal_night_kwh(self.NOW), 0.0)
        self.assertNotIn("CHARGE_GOAL", self.store)
        self.assertIn("Miles goal reached", self.notes[0])

    def test_cycle_clears_an_expired_goal(self):
        charge_goal.set_goal(100, self.NOW + timedelta(hours=1), self.NOW)
        self.assertEqual(main._goal_night_kwh(self.NOW + timedelta(hours=2)), 0.0)
        self.assertNotIn("CHARGE_GOAL", self.store)
        self.assertIn("deadline passed", self.notes[0])


if __name__ == "__main__":
    unittest.main()
