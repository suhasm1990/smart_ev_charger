"""Mutable runtime state shared across the daemon, bot, and reporting layers.

The state lives in a single `StateStore` instance guarded by one RLock. Plain
attribute reads/writes (`state.charger_state = ...`) are individually locked,
and compound transitions (adopting a hardware session, starting/ending a
session) are methods that hold the lock across the whole transition.
`snapshot()` gives callers one internally-consistent view for multi-field reads.
"""
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime

from core import config


class State:
    IDLE     = "IDLE"
    CHARGING = "CHARGING"


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Every state field with its default; also the shape `snapshot()` returns."""
    charger_state: str = State.IDLE
    charge_session_start: datetime | None = None
    session_stop_reason: str | None = None
    manual_mode: bool = False
    manual_mode_set_at: datetime | None = None
    prev_manual_mode: bool = False
    session_count_today: int = 0
    grid_draw_count: int = 0
    consecutive_api_failures: int = 0
    last_grid_export_alert_date: str | None = None
    last_manual_grid_alert: str | None = None
    last_goal_plug_alert_date: str | None = None
    # Manual-mode guardrails, both optional and cleared on return to auto.
    manual_guard_stop_battery_pct: float | None = None  # e.g. 30.0
    manual_guard_stop_time: datetime | None = None      # charging must stop at this time
    active_amperage: int = field(default_factory=lambda: config.DEFAULT_CHARGER_AMPERAGE)


class StateStore:
    State = State  # allows `state.State.IDLE` at call sites

    def __init__(self):
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_data", asdict(StateSnapshot()))
        self.restore_guards_from_config()

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
                raise AttributeError(f"Unknown state field: {name!r}")  # catches typo'd names
            self._data[name] = value

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

    def set_manual_guards(self, stop_battery_pct: float | None, stop_time: datetime | None):
        """Sets the manual-session guardrails and persists them across restarts."""
        with self._lock:
            self._data.update(manual_guard_stop_battery_pct=stop_battery_pct, manual_guard_stop_time=stop_time)
        try:
            config.update(
                MANUAL_GUARD_STOP_BATTERY_PCT=stop_battery_pct,
                MANUAL_GUARD_STOP_TIME=stop_time.isoformat() if stop_time else None,
            )
        except Exception:
            pass

    def clear_manual_guards(self):
        self.active_amperage = config.DEFAULT_CHARGER_AMPERAGE
        self.set_manual_guards(None, None)

    def restore_guards_from_config(self):
        """Restores persisted guardrails from config into active state."""
        try:
            stop_time = datetime.fromisoformat(config.MANUAL_GUARD_STOP_TIME)
        except (TypeError, ValueError):
            stop_time = None
        with self._lock:
            self._data.update(
                manual_guard_stop_battery_pct=config.MANUAL_GUARD_STOP_BATTERY_PCT,
                manual_guard_stop_time=stop_time,
            )

    def reset_daily(self) -> tuple[int, int]:
        """Zeroes the daily counters; returns the old values for the log line."""
        with self._lock:
            old = (self._data["session_count_today"], self._data["grid_draw_count"])
            self._data.update(session_count_today=0, grid_draw_count=0)
            return old

    def reset_for_tests(self):
        """Restores every field to its default. Test harness only."""
        with self._lock:
            self._data.update(asdict(StateSnapshot()))

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
get_session_minutes = state.get_session_minutes
charger_power_kw = state.charger_power_kw
