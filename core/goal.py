"""Miles goals: "add 100 miles by tomorrow", met by solar first and cheap night hours only if needed.

The goal lives in the Google Sheets Settings tab (key CHARGE_GOAL) so it
survives restarts. Progress is measured from logged charging, and night
charging is requested only for the part that solar is not expected to cover
before the deadline.
"""
import json
from datetime import datetime, timedelta

from core import config
from reporting.logger import log

SETTINGS_KEY = "CHARGE_GOAL"
MAX_GOAL_DAYS = 7


def get_goal() -> dict | None:
    """The active goal with parsed datetimes, or None."""
    from services.sheets_db import get_settings
    raw = get_settings().get(SETTINGS_KEY)
    if not raw:
        return None
    try:
        goal = json.loads(raw)
        return {
            "miles": float(goal["miles"]),
            "deadline": datetime.fromisoformat(goal["deadline"]),
            "set_at": datetime.fromisoformat(goal["set_at"]),
        }
    except (ValueError, KeyError, TypeError) as e:
        log.warning(f"Ignoring unreadable {SETTINGS_KEY} setting {raw!r}: {e}")
        return None


def set_goal(miles: float, deadline: datetime, now: datetime) -> dict:
    """Validates and stores a goal; raises ValueError with a user-facing reason."""
    if miles <= 0:
        raise ValueError("miles must be greater than zero")
    if deadline <= now:
        raise ValueError(f"the deadline {deadline:%a %b %d %H:%M} is already in the past")
    if deadline - now > timedelta(days=MAX_GOAL_DAYS):
        raise ValueError(f"the deadline must be within {MAX_GOAL_DAYS} days")
    from services.sheets_db import save_charge_goal
    payload = {"miles": miles, "deadline": deadline.isoformat(), "set_at": now.isoformat()}
    if not save_charge_goal(json.dumps(payload)):
        raise ValueError("could not save the goal to Google Sheets")
    return {"miles": miles, "deadline": deadline, "set_at": now}


def clear_goal() -> bool:
    from services.sheets_db import clear_charge_goal
    return clear_charge_goal()


def progress(goal: dict) -> dict:
    """kWh and miles target, added so far, and remaining."""
    from reporting.csv_logger import get_ev_kwh_since
    target_kwh = goal["miles"] / config.EV_MILES_PER_KWH
    added_kwh = get_ev_kwh_since(goal["set_at"])
    remaining_kwh = max(0.0, target_kwh - added_kwh)
    return {
        "target_kwh": round(target_kwh, 2),
        "added_kwh": added_kwh,
        "added_miles": round(added_kwh * config.EV_MILES_PER_KWH, 1),
        "remaining_kwh": round(remaining_kwh, 2),
        "remaining_miles": round(remaining_kwh * config.EV_MILES_PER_KWH, 1),
    }


def expected_solar_kwh(now: datetime, deadline: datetime, profile: list[dict], charger_kw: float) -> float:
    """Solar the car can expect before the deadline, from the 7-day hourly averages.

    Each remaining hour contributes its average surplus (solar minus house
    load), capped at what the charger can draw.
    """
    surplus = {p["hour"]: max(0.0, p["solar_kw"] - p["house_kw"]) for p in profile}
    total = 0.0
    hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while hour < deadline:
        total += min(charger_kw, surplus.get(hour.hour, 0.0))
        hour += timedelta(hours=1)
    return round(total, 2)


def night_kwh_needed(now: datetime, goal: dict, remaining_kwh: float) -> float:
    """kWh the goal still needs beyond the solar expected before its deadline."""
    from core.state import charger_power_kw
    from reporting.csv_logger import get_energy_saving_advice
    profile = get_energy_saving_advice().get("hourly_profile_7_days", [])
    solar = expected_solar_kwh(now, goal["deadline"], profile,
                               charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE))
    return round(max(0.0, remaining_kwh - solar), 2)


def describe(goal: dict, prog: dict) -> str:
    return (f"{prog['added_miles']:.0f} of {goal['miles']:.0f} miles added; "
            f"{prog['remaining_miles']:.0f} miles to go by {goal['deadline']:%a %b %d %H:%M}")
