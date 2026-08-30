"""Mutable runtime state shared across the daemon, bot, and reporting layers.

The state lives in a single `StateStore` instance guarded by one RLock. Plain
attribute reads/writes (`state.charger_state = ...`) keep working exactly as
they did when these were module globals, but are now individually locked, and
compound transitions (adopting a hardware session, starting/ending a session)
are methods that hold the lock across the whole transition. `snapshot()` gives
callers one internally-consistent view for multi-field reads.
"""
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from core import config


class State:
    IDLE     = "IDLE"
    CHARGING = "CHARGING"
    WAITING  = "WAITING"


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    charger_state: str
    charge_session_start: datetime | None
    session_stop_reason: str | None
    manual_mode: bool
    manual_mode_set_at: datetime | None
    prev_manual_mode: bool
    session_count_today: int
    grid_draw_count: int
    consecutive_api_failures: int
    last_grid_export_alert_date: str | None
    last_manual_grid_alert: str | None
    manual_guard_stop_battery_pct: float | None
    manual_guard_stop_at_hour: int | None
    manual_guard_stop_time: datetime | None
    active_amperage: int


def _defaults() -> dict:
    return {
        "charger_state": State.IDLE,
        "charge_session_start": None,
        "session_stop_reason": None,
        "manual_mode": False,
        "manual_mode_set_at": None,
        "prev_manual_mode": False,
        "session_count_today": 0,
        "grid_draw_count": 0,
        "consecutive_api_failures": 0,
        "last_grid_export_alert_date": None,
        "last_manual_grid_alert": None,
        # Manual-mode guardrails, all optional and cleared on return to auto.
        "manual_guard_stop_battery_pct": None,  # e.g. 30.0
        "manual_guard_stop_at_hour": None,      # e.g. 16
        "manual_guard_stop_time": None,         # datetime after which charging must stop
        "active_amperage": config.DEFAULT_CHARGER_AMPERAGE,
    }


class StateStore:
    State = State  # keeps `state.State.IDLE` working at existing call sites

    def __init__(self):
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_data", _defaults())

    # ── Locked attribute access ─────────────────────────────────────────────

    def __getattr__(self, name):
        # Only called when normal lookup fails, i.e. for data fields.
        try:
            with object.__getattribute__(self, "_lock"):
                return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(f"Unknown state field: {name!r}") from None

    def __setattr__(self, name, value):
        with self._lock:
            if name not in self._data:
                # Module globals silently accepted typo'd names; the store does not.
                raise AttributeError(f"Unknown state field: {name!r}")
            self._data[name] = value

    @contextmanager
    def locked(self):
        """Holds the state lock across a caller's compound read-modify-write."""
        with self._lock:
            yield self

    def snapshot(self) -> StateSnapshot:
        """One internally-consistent view of every field."""
        with self._lock:
            return StateSnapshot(**self._data)

    def bump(self, name: str) -> int:
        """Atomic increment for counters written from more than one thread."""
        with self._lock:
            self._data[name] += 1
            return self._data[name]

    # ── Transactions (hold the lock across the whole transition) ────────────

    def begin_session(self, now: datetime, amperage: int):
        """Records a confirmed charging start."""
        with self._lock:
            self._data.update(
                charger_state=State.CHARGING,
                charge_session_start=now,
                session_stop_reason=None,
                active_amperage=amperage,
            )
            self._data["session_count_today"] += 1

    def end_session(self, reason: str):
        """Records a confirmed charging stop."""
        with self._lock:
            self._data.update(
                charger_state=State.IDLE,
                charge_session_start=None,
                session_stop_reason=reason,
            )

    def sync_with_hardware(self, cp_status: dict, now: datetime,
                           drift_seconds: float = 3600) -> str | None:
        """Reconciles in-memory session state with what the charger reports.

        The single implementation used by the control cycle, startup adoption,
        and the bot's status tool. Returns what changed — "adopted", "drift",
        "cleared" — or None when state already matched, so callers can log.
        """
        reported = cp_status.get("session_start_time")
        with self._lock:
            d = self._data
            if cp_status.get("charging_status") == "CHARGING":
                if d["charger_state"] != State.CHARGING or d["charge_session_start"] is None:
                    d["charger_state"] = State.CHARGING
                    d["charge_session_start"] = reported or now
                    d["session_stop_reason"] = None
                    if cp_status.get("amperage_limit"):
                        d["active_amperage"] = cp_status["amperage_limit"]
                    return "adopted"
                if reported and abs((d["charge_session_start"] - reported).total_seconds()) > drift_seconds:
                    d["charge_session_start"] = reported
                    return "drift"
                return None
            if d["charger_state"] == State.CHARGING or d["charge_session_start"] is not None:
                d["charger_state"] = State.IDLE
                d["charge_session_start"] = None
                return "cleared"
            return None

    def set_manual_guards(self, stop_battery_pct: float | None,
                          stop_at_hour: int | None, stop_time: datetime | None):
        with self._lock:
            self._data.update(
                manual_guard_stop_battery_pct=stop_battery_pct,
                manual_guard_stop_at_hour=stop_at_hour,
                manual_guard_stop_time=stop_time,
            )

    def clear_manual_guards(self):
        with self._lock:
            self._data.update(
                manual_guard_stop_battery_pct=None,
                manual_guard_stop_at_hour=None,
                manual_guard_stop_time=None,
                active_amperage=config.DEFAULT_CHARGER_AMPERAGE,
            )

    def reset_daily(self) -> tuple[int, int]:
        """Zeroes the daily counters; returns the old values for the log line."""
        with self._lock:
            old = (self._data["session_count_today"], self._data["grid_draw_count"])
            self._data["session_count_today"] = 0
            self._data["grid_draw_count"] = 0
            return old

    def reset_for_tests(self):
        """Restores every field to its default. Test harness only."""
        with self._lock:
            self._data.clear()
            self._data.update(_defaults())

    # ── Derived values ──────────────────────────────────────────────────────

    def get_session_minutes(self) -> float:
        """Elapsed minutes of the active charging session, or 0.0 when idle."""
        start = self.charge_session_start
        if start is None:
            return 0.0
        return max(0.0, round((datetime.now(config.TZ) - start).total_seconds() / 60, 1))

    def charger_power_kw(self, amperage: int = None) -> float:
        """Charger draw in kW at the given amperage (defaults to the active limit)."""
        amps = amperage if amperage is not None else self.active_amperage
        return round(amps * config.CHARGER_VOLTAGE / 1000.0, 3)


state = StateStore()


# Back-compat module-level functions for `from core.state import ...` callers.

def clear_manual_guards():
    state.clear_manual_guards()


def get_session_minutes() -> float:
    """Elapsed minutes of the active charging session, or 0.0 when idle."""
    return state.get_session_minutes()


def charger_power_kw(amperage: int = None) -> float:
    """Charger draw in kW at the given amperage (defaults to the active limit)."""
    return state.charger_power_kw(amperage)
