"""Morning AI planner: sets today's Powerwall thresholds for the live charging rules."""
from datetime import datetime, timedelta

from agent import llm_client
from core import config
from core import goal as charge_goal
from core.state import charger_power_kw
from core.tou import (
    ON_PEAK_HOURS,
    get_tou_period,
    get_tou_rate,
    is_utility_holiday,
    is_weekend,
    provider_label,
    season,
)
from reporting.csv_logger import _export_credit_rate, get_energy_saving_advice, get_home_energy_summary
from reporting.logger import log
from reporting.notifications import notify
from services.sheets_db import clear_user_instruction, get_settings

# Applied setting -> (LLM response key, cast, low, high). The prompt quotes
# these same bounds, so the model is never asked for a value we then clamp.
PLAN_FIELDS = {
    "BATTERY_START_PCT": ("battery_start_pct", float, 45.0, 80.0),
    "BATTERY_STOP_PCT":  ("battery_stop_pct", float, 20.0, 50.0),
}
MIN_HYSTERESIS_PCT = 10.0  # start must exceed stop by this much to avoid short-cycling
# Daytime charge window bounds: never into on-peak, where the car would drain
# the Powerwall the house needs for the evening.
WINDOW_EARLIEST_HOUR = 6
WINDOW_LATEST_END = ON_PEAK_HOURS[0]
DEFAULT_WINDOW = (10, 15)

PROMPT = """You are the AI Planner for a Smart EV Charger.
Goal: minimise electricity cost. Charge the EV from solar, and keep enough Powerwall energy for the \
house's unavoidable evening use so it never buys on-peak grid power.

Today: {today} ({day_type}, {season} rates)

How charging is controlled (code checks the LIVE Powerwall % every 15 minutes; you set today's charge \
window and the two Powerwall thresholds):
• Inside the charge window, charging starts when the Powerwall is at or above battery_start_pct and runs \
at {charger_kw} kW until the Powerwall falls below battery_stop_pct or the window ends. Solar readings do \
not start or stop it: whatever the charger draws beyond the current solar surplus comes from the Powerwall.
• Once stopped at battery_stop_pct, it restarts only after the Powerwall refills to battery_start_pct.
• The window must be daytime, {earliest}:00 at the earliest, ending by {latest_end}:00 (on-peak starts then).
• Night charging happens only for a user's miles goal, only at the day's cheapest rate, and it also stops \
below battery_stop_pct.
• Charging always stops below the {low_reserve}% critical Powerwall reserve.

Miles goal: {goal}

Utility rates today ({rate_plan}, delivered price incl. surcharges):
{rate_table}
• Solar export credit: ${export_rate:.3f}/kWh. Solar sent to the EV instead of the grid only gives up this \
credit, so it is the cheapest energy.

How the Powerwall's energy is valued:
• Powerwall energy given to the EV costs nothing extra as long as the Powerwall still holds the evening \
reserve by 16:00 (later solar refills it). It costs on-peak grid power only for the part that leaves the \
Powerwall short in the evening.
• Powerwall energy above the evening reserve has little other use, so it is better spent on the EV than left \
idle. Do not invent other per-kWh costs.

Energy budget today:
{energy_budget}

Hourly profile (7-day averages; house load excludes the EV; surplus = solar - house load):
hour | rate period | avg solar kW | avg house kW | avg surplus kW
{hourly_table}

Evening needs & history:
{history}

{instruction}

Strategy Guidelines:
1. 'battery_stop_pct' is the LOWER cutoff where EV charging must STOP to safeguard the evening home battery \
reserve ({reserve_note}).
2. 'battery_start_pct' is the Powerwall level at which charging starts inside the window. Use the \
energy budget: pick a start the Powerwall will actually reach during the window, or the car will not \
charge today, while leaving the Powerwall able to refill to the evening reserve.
3. CRITICAL CONSTRAINT: 'battery_start_pct' MUST be at least {gap:g} points higher than 'battery_stop_pct' \
(e.g. stop 40.0 -> start 55.0).
4. 'daily_suggestion': name specific shiftable loads (dishwasher, laundry, dryer, pre-cooling with AC, \
water heating) to run during today's strongest solar-surplus hours from the profile, where they use solar \
that would otherwise be exported for the credit. Advise keeping those loads out of the on-peak hours so the \
Powerwall covers unavoidable evening use (cooking, lighting). Never recommend running heavy loads in the \
evening or on the Powerwall.

5. Charge window: cover the hours with the strongest solar surplus in the profile, so the Powerwall \
refills while the car charges and the car draws as little stored energy as possible.

Recommend today's charge window and thresholds. Respond ONLY with a valid JSON object matching this schema \
(write the reasoning first, then the values):
{{
  "reasoning": "<brief explanation of the chosen thresholds, citing the history and rates>",
  "charge_window_start_hour": <int {earliest}-{latest_start}>,
  "charge_window_end_hour": <int, after the start and at most {latest_end}>,
  "battery_start_pct": <float {start_lo:g}-{start_hi:g}, at least {gap:g} above battery_stop_pct>,
  "battery_stop_pct": <float {stop_lo:g}-{stop_hi:g}>,
  "daily_suggestion": "<concise 1-2 sentences: which loads to run in which solar hours, and what to avoid on-peak>"
}}"""


# ── Prompt context ──────────────────────────────────────────────────────────

def _at_hour(now: datetime, hour: int) -> datetime:
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def _format_ranges(hours: list[int]) -> str:
    """[9, 10, 11, 14] -> '09:00-12:00, 14:00-15:00'."""
    ranges: list[list[int]] = []
    for h in sorted(hours):
        if ranges and ranges[-1][1] == h:
            ranges[-1][1] = h + 1
        else:
            ranges.append([h, h + 1])
    return ", ".join(f"{a:02d}:00-{b:02d}:00" for a, b in ranges) or "none"


def _day_type(now: datetime) -> str:
    if is_weekend(now):
        return "weekend, off-peak all day"
    if is_utility_holiday(now.date()):
        return "utility holiday, off-peak all day"
    return "weekday"


def _rate_table(now: datetime) -> str:
    """Today's TOU periods as contiguous hour ranges with their delivered rate."""
    periods: dict[tuple[str, float], list[int]] = {}
    for h in range(24):
        at = _at_hour(now, h)
        periods.setdefault((get_tou_period(at), get_tou_rate(at)), []).append(h)
    return "\n".join(
        f"• {_format_ranges(hours)}: {period.replace('_', '-')} ${rate:.3f}/kWh"
        for (period, rate), hours in sorted(periods.items(), key=lambda kv: kv[1][0])
    )


def _hourly_table(now: datetime, profile: list[dict]) -> str:
    rows = []
    for p in profile:
        at = _at_hour(now, p["hour"])
        rows.append(" | ".join((
            f"{p['hour']:02d}:00",
            f"{get_tou_period(at).replace('_', '-')} ${get_tou_rate(at):.3f}",
            f"{p['solar_kw']:.1f}",
            f"{p['house_kw']:.1f}",
            f"{p['solar_kw'] - p['house_kw']:+.1f}",
        )))
    return "\n".join(rows) or "No logged history yet."


def _history_section(advice: dict, yesterday: dict) -> str:
    """Evening needs and yesterday's totals, saying 'unavailable' rather than inventing numbers."""
    lines = []
    if "error" in advice:
        lines.append(f"• 7-day evening history unavailable ({advice['error']}).")
    else:
        lines += [
            f"• Avg Evening Household Load (16:00-22:00): {advice['avg_evening_appliance_load_kwh']} kWh",
            f"• Avg Late-Afternoon/Evening Solar: {advice['avg_evening_solar_generation_kwh']} kWh",
            f"• Net Evening Battery Deficit to Cover: {advice['avg_evening_net_battery_deficit_kwh']} kWh",
            f"• Required Minimum Battery Reserve at 16:00: {advice['recommended_evening_battery_reserve_pct']}%",
            f"• Last 7 days: on-peak grid cost ${advice['on_peak_grid_cost_last_7_days']:.2f}, "
            f"EV grid charging cost ${advice['ev_grid_charging_cost_last_7_days']:.2f}",
        ]
    if not yesterday or "error" in yesterday:
        lines.append("• Yesterday's totals unavailable.")
    else:
        lines += [
            f"• Yesterday Solar Generated: {yesterday['total_solar_generated_kwh']} kWh | "
            f"Grid Imported: {yesterday['total_grid_imported_kwh']} kWh "
            f"(Self-Powered: {yesterday['home_self_powered_percentage']}%)",
            f"• Yesterday EV Charged: {yesterday['ev_charging_total_kwh']} kWh "
            f"({yesterday['ev_estimated_miles_added']} miles added)",
        ]
    return "\n".join(lines)


def _latest_battery_pct(now: datetime) -> float | None:
    """The Powerwall % from the most recent telemetry row in the last two hours."""
    from reporting.csv_logger import get_all_log_rows
    try:
        for row in reversed(get_all_log_rows(days=1)):
            ts = datetime.fromisoformat(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=config.TZ)
            if now - ts > timedelta(hours=2):
                return None
            return float(row["battery_pct"])
    except Exception as e:
        log.warning(f"Daily agent could not read the current Powerwall level: {e}")
    return None


def _energy_budget(now: datetime, profile: list[dict], battery_pct: float | None) -> str:
    """Powerwall size, current level and expected solar surplus to 16:00, as kWh and Powerwall %."""
    usable = config.POWERWALL_USABLE_KWH
    surplus = sum(max(0.0, p["solar_kw"] - p["house_kw"]) for p in profile if now.hour <= p["hour"] < 16)
    surplus_pct = surplus / usable * 100
    lines = [f"• Powerwall usable capacity: {usable:g} kWh (1% = {usable / 100:.3f} kWh)",
             f"• Expected solar surplus from now to 16:00: {surplus:.1f} kWh (about {surplus_pct:.0f}% of the Powerwall)"]
    if battery_pct is None:
        lines.append("• Current Powerwall level: unavailable")
    else:
        lines += [f"• Current Powerwall level: {battery_pct:.0f}%",
                  f"• If the EV takes none of that surplus, the Powerwall reaches about "
                  f"{min(100.0, battery_pct + surplus_pct):.0f}% by 16:00"]
    return "\n".join(lines)


def _goal_text() -> str:
    try:
        goal = charge_goal.get_goal()
        return charge_goal.describe(goal, charge_goal.progress(goal)) if goal else "none"
    except Exception as e:
        log.warning(f"Daily agent could not read the miles goal: {e}")
        return "unavailable"


def build_prompt(now: datetime, advice: dict, yesterday: dict, instruction: str, goal: str = "none",
                 battery_pct: float | None = None) -> str:
    profile = advice.get("hourly_profile_7_days", [])
    reserve = advice.get("recommended_evening_battery_reserve_pct")
    start_lo, start_hi = PLAN_FIELDS["BATTERY_START_PCT"][2:]
    stop_lo, stop_hi = PLAN_FIELDS["BATTERY_STOP_PCT"][2:]
    return PROMPT.format(
        today=now.strftime("%A %Y-%m-%d"),
        day_type=_day_type(now),
        season=season(now.month),
        charger_kw=charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE),
        earliest=WINDOW_EARLIEST_HOUR, latest_start=WINDOW_LATEST_END - 1, latest_end=WINDOW_LATEST_END,
        low_reserve=config.BATTERY_LOW_RESERVE_PCT,
        goal=goal,
        rate_plan=provider_label(),
        rate_table=_rate_table(now),
        export_rate=_export_credit_rate(),
        hourly_table=_hourly_table(now, profile),
        energy_budget=_energy_budget(now, profile, battery_pct),
        history=_history_section(advice, yesterday),
        instruction=f"USER INSTRUCTION OVERRIDE: {instruction}" if instruction else "No special user instructions today.",
        reserve_note=f"around {reserve}% needed at 16:00" if reserve is not None
        else f"history unavailable; current stop is {config.BATTERY_STOP_PCT}%",
        gap=MIN_HYSTERESIS_PCT,
        start_lo=start_lo, start_hi=start_hi, stop_lo=stop_lo, stop_hi=stop_hi,
    )


# ── Plan validation ─────────────────────────────────────────────────────────

def _valid_window(plan: dict) -> tuple[int, int]:
    """The planned daytime window, clamped to the bounds; the default if unusable."""
    try:
        start = int(plan["charge_window_start_hour"])
        end = int(plan["charge_window_end_hour"])
    except (KeyError, TypeError, ValueError):
        log.warning(f"Daily agent gave no usable charge window; using {DEFAULT_WINDOW[0]}-{DEFAULT_WINDOW[1]}")
        return DEFAULT_WINDOW
    start = min(WINDOW_LATEST_END - 1, max(WINDOW_EARLIEST_HOUR, start))
    end = min(WINDOW_LATEST_END, max(start + 1, end))
    return start, end


def _apply_plan(plan: dict) -> list[str]:
    """Applies the returned thresholds, clamping each value into a safe range.

    Also enforces the start/stop hysteresis gap and keeps the stop above the
    critical reserve. Sets today's daytime charge window and clears any
    manual night blackout so it cannot block that window.
    """
    applied = []
    changes = {}
    for setting, (key, cast, low, high) in PLAN_FIELDS.items():
        raw = plan.get(key)
        if raw is None:
            continue
        try:
            value = min(high, max(low, cast(raw)))
        except (TypeError, ValueError):
            log.warning(f"Daily agent returned an unusable value for {key}: {raw!r}")
            continue
        changes[setting] = value
        applied.append(f"{setting}={value}")

    stop = changes.get("BATTERY_STOP_PCT", config.BATTERY_STOP_PCT)
    if stop < config.BATTERY_LOW_RESERVE_PCT:
        stop = changes["BATTERY_STOP_PCT"] = config.BATTERY_LOW_RESERVE_PCT
        applied.append(f"BATTERY_STOP_PCT={stop} (raised to critical reserve)")

    start = changes.get("BATTERY_START_PCT", config.BATTERY_START_PCT)
    if start < stop + MIN_HYSTERESIS_PCT:
        corrected_start = min(100.0, stop + MIN_HYSTERESIS_PCT)
        log.warning(
            f"Daily agent plan had thresholds too close (start {start}% vs stop {stop}%). "
            f"Correcting BATTERY_START_PCT to {corrected_start}%."
        )
        changes["BATTERY_START_PCT"] = corrected_start
        applied.append(f"BATTERY_START_PCT={corrected_start} (corrected)")

    start_hr, end_hr = _valid_window(plan)
    changes["ALLOWED_CHARGE_START_HOUR"], changes["ALLOWED_CHARGE_END_HOUR"] = start_hr, end_hr
    applied.append(f"ALLOWED_CHARGE_WINDOW={start_hr}-{end_hr}")

    blackout_start, blackout_end = config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR
    if blackout_start != blackout_end:
        changes["NIGHT_BLACKOUT_START_HOUR"] = changes["NIGHT_BLACKOUT_END_HOUR"] = 0
        applied.append(f"NIGHT_BLACKOUT cleared (was {blackout_start}:00-{blackout_end}:00)")

    if changes:
        config.update(**changes)  # one atomic, validated, persisted transition
    return applied


def run_daily_agent():
    """Fetches history and today's rates, asks the model for thresholds, and applies them."""
    log.info("Starting the daily AI planner...")
    cfg = llm_client.resolve_llm_config()
    if not cfg.get("api_key"):
        log.warning(f"No API key for LLM provider '{cfg.get('provider')}'. Daily agent cannot run.")
        return

    now = datetime.now(config.TZ)
    advice = get_energy_saving_advice()
    yesterday = get_home_energy_summary("yesterday")
    if not isinstance(yesterday, dict):
        yesterday = {}
    instruction = get_settings().get("USER_INSTRUCTION", "")
    goal = _goal_text()
    prompt = build_prompt(now, advice, yesterday, instruction, goal, _latest_battery_pct(now))

    try:
        plan = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily agent plan: {plan}")
        blackout = (config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR)
        log.info(f"Daily agent applied: {', '.join(_apply_plan(plan)) or 'no changes'}")
        blackout_note = (
            f"• <b>Night Blackout</b>: cleared (was {blackout[0]}:00 - {blackout[1]}:00) so it can't block the charge window\n"
            if blackout[0] != blackout[1] else ""
        )
        goal_note = f"• <b>Miles Goal</b>: {goal}\n" if goal not in ("none", "unavailable") else ""

        # Only clear the instruction once it has actually been acted on.
        if instruction:
            clear_user_instruction()

        summary = ""
        if yesterday and "error" not in yesterday:
            summary = (
                f"📊 <b>Yesterday's Energy & Bill Summary</b>:\n"
                f"• <b>Estimated Total Bill</b>: ${yesterday.get('estimated_total_mid_utility_bill_dollars', 0.0):.2f}\n"
                f"  - 🚗 <b>EV Charging Share</b>: ${yesterday.get('ev_charging_share_of_bill_dollars', 0.0):.2f}\n"
                f"  - 🏠 <b>Home Appliances Share</b>: ${yesterday.get('home_appliances_share_of_bill_dollars', 0.0):.2f}\n"
                f"• <b>Home Self-Powered</b>: {yesterday.get('home_self_powered_percentage', 100.0)}%\n"
                f"• <b>Solar Generated</b>: {yesterday.get('total_solar_generated_kwh', 0.0)} kWh\n"
                f"• <b>Grid Imported</b>: {yesterday.get('total_grid_imported_kwh', 0.0)} kWh\n\n"
            )

        notify(
            f"🤖 <b>Daily AI Agent Update ({datetime.now(config.TZ):%H:%M})</b>\n\n{summary}"
            f"⚙️ <b>Today's Charging Strategy</b>:\n"
            f"• <b>Battery Start/Stop</b>: {config.BATTERY_START_PCT}% / {config.BATTERY_STOP_PCT}%\n"
            f"• <b>Charge Window</b>: {config.ALLOWED_CHARGE_START_HOUR}:00 - {config.ALLOWED_CHARGE_END_HOUR}:00\n"
            f"• <b>Charging</b>: starts in the window when the Powerwall is ≥{config.BATTERY_START_PCT}%, "
            f"stops below {config.BATTERY_STOP_PCT}% or when the window ends; night charging only for a miles goal\n"
            f"{goal_note}{blackout_note}\n"
            f"💡 <b>Energy Suggestion for Today</b>:\n<i>{plan.get('daily_suggestion', '')}</i>\n\n"
            f"<i>{plan.get('reasoning', '')}</i>\n\n"
            f"{llm_client.model_label()}"
        )
    except Exception as e:
        log.error(f"Daily agent failed: {e}", exc_info=True)
        notify(f"⚠️ <b>Daily AI Agent Notice</b>\nThe planner failed to run: {e}")


if __name__ == "__main__":
    run_daily_agent()
