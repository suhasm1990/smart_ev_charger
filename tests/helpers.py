"""Shared mocking harness for tests that drive the control loop."""
from datetime import datetime, timedelta

import main
import services.sheets_db
from core import config, state


def powerwall(battery_pct=50.0, solar_kw=4.0, home_kw=1.0, grid_kw=0.0,
              island_mode="on_grid", storm_mode=False) -> dict:
    return {
        "battery_pct": battery_pct, "solar_kw": solar_kw, "home_kw": home_kw,
        "grid_kw": grid_kw, "battery_kw": -1.0, "solar_surplus_kw": solar_kw - home_kw,
        "grid_export_kw": max(0.0, -grid_kw), "self_powered_pct": 100.0,
        "island_mode": island_mode, "storm_mode": storm_mode, "data_ts": "",
    }


def charger(charging=False, plugged_in=True, amperage=20, session_start=None) -> dict:
    return {
        "charging_status": "CHARGING" if charging else "AVAILABLE",
        "is_plugged_in": plugged_in, "is_connected": True, "amperage_limit": amperage,
        "energy_kwh": 0.0, "power_kw": 0.0, "miles_added": 0.0,
        "charging_time_seconds": 0, "session_start_time": session_start,
    }


class MockedCycle:
    """Neutralises hardware, notifications, and network for control-loop tests."""

    def __init__(self):
        self.calls = []
        self._patched: list[tuple] = []

    def _patch(self, module, name, replacement):
        """Swaps an attribute, remembering the original so restore() is exact."""
        self._patched.append((module, name, getattr(module, name)))
        setattr(module, name, replacement)

    def install(self, pw: dict, cp: dict):
        self._patch(main, "get_powerwall_stats", lambda: pw)
        self._patch(main, "get_charger_status", lambda: cp)
        self._patch(main, "start_charger", lambda amperage=None: self.calls.append(("start", amperage)))
        self._patch(main, "stop_charger", lambda: self.calls.append(("stop", None)))
        self._patch(main, "set_charger_amperage_limit", lambda amperage: self.calls.append(("amperage", amperage)))
        self._patch(main, "notify", lambda message: None)
        self._patch(main, "check_recent_log_errors", lambda interval_minutes=20: False)
        self._patch(main.config, "load_dynamic_config", lambda remote=True: None)
        self._patch(services.sheets_db, "append_log_row", lambda row: True)
        self._patch(services.sheets_db, "update_settings", lambda settings, blocking=False: True)

    def restore(self):
        """Undoes every patch, so one test cannot leak stubs into the next."""
        for module, name, original in reversed(self._patched):
            setattr(module, name, original)
        self._patched.clear()

    def reset_state(self, override="auto"):
        self.calls.clear()
        state.charger_state = state.State.IDLE
        state.charge_session_start = None
        state.session_stop_reason = None
        state.manual_mode = state.prev_manual_mode = False
        state.manual_mode_set_at = None
        state.clear_manual_guards()
        config.MANUAL_MODE_OVERRIDE = override
        config.BATTERY_START_PCT, config.BATTERY_STOP_PCT = 40.0, 25.0
        config.BATTERY_LOW_RESERVE_PCT = 15.0
        config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR = 16, 9
        config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR = 0, 24

    def enter_manual(self):
        """Puts the daemon into an active manual charging session."""
        state.manual_mode = state.prev_manual_mode = True
        state.manual_mode_set_at = datetime.now(config.TZ)
        state.charger_state = state.State.CHARGING
        state.charge_session_start = datetime.now(config.TZ) - timedelta(minutes=5)
        config.MANUAL_MODE_OVERRIDE = "manual"


def frozen_now(moment: datetime):
    """Returns a datetime subclass whose now() is pinned, for time-based guards."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment
    return Frozen


def seed_telemetry(days_back_start: int = 40, days: int = 31, interval_minutes: int = 15) -> list[dict]:
    """Builds a deterministic telemetry history so analytics tests never depend
    on whatever happens to be in the live spreadsheet."""
    from datetime import date

    rows = []
    start = datetime.now(config.TZ).date() - timedelta(days=days_back_start)
    for day_offset in range(days):
        day = start + timedelta(days=day_offset)
        for minute in range(0, 24 * 60, interval_minutes):
            hour, mins = divmod(minute, 60)
            stamp = datetime(day.year, day.month, day.day, hour, mins, tzinfo=config.TZ)
            # A simple solar bell curve, with the EV charging over midday.
            solar = round(max(0.0, 6.0 - abs(hour - 12.5) * 0.9), 2)
            home = 1.2 if hour < 16 else 3.4
            charging = 10 <= hour < 15
            if charging:
                home += 4.8
            grid = round(max(-4.0, home - solar), 2)
            rows.append({
                "timestamp": stamp.isoformat(),
                "date": day.strftime("%Y-%m-%d"),
                "time": stamp.strftime("%H:%M"),
                "solar_kw": str(solar), "home_kw": str(home), "grid_kw": str(grid),
                "battery_kw": "0.0", "battery_pct": "55.0", "self_powered_pct": "90.0",
                "charger_amperage": "20" if charging else "0",
                "charger_state": "CHARGING" if charging else "IDLE",
                "action": "hold", "reason": "",
                "session_active_minutes": "60" if charging else "0",
                "session_stop_reason": "",
                "tou_period": "", "tou_rate_per_kwh": "0.17",
            })
    return rows
