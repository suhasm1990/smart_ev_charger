"""Morning AI planner: sets today's Powerwall thresholds for the live charging rules."""
from datetime import datetime

from agent import llm_client
from core import config
from core import goal as charge_goal
from core.state import charger_power_kw
from core.tou import (
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

PROMPT = """You are the AI Planner for a Smart EV Charger.
Goal: minimise electricity cost. Charge the EV from solar, and protect the home battery so heavy \
evening loads (AC, induction stove, dishwasher between 16:00 and 22:00) run on battery instead of \
on-peak grid power.

Today: {today} ({day_type}, {season} rates)

How charging is controlled (code applies these rules every 15 minutes to LIVE readings; you only set \
today's two Powerwall thresholds):
• Charging starts when the live solar surplus (solar minus house load) is at least {surplus_start:g} kW and \
the Powerwall is at or above battery_start_pct, or as soon as the surplus alone covers the charger's \
{charger_kw} kW draw.
• It continues while solar produces at least {solar_min:g} kW, and stops when solar fades or the Powerwall \
falls below battery_stop_pct (the car is then draining it).
• Night charging happens only for a user's miles goal, only at the day's cheapest rate, and it also stops \
below battery_stop_pct.
• Charging always stops below the {low_reserve}% critical Powerwall reserve.

Miles goal: {goal}

Utility rates today ({rate_plan}, delivered price incl. surcharges):
{rate_table}
• Solar export credit: ${export_rate:.3f}/kWh. Solar sent to the EV instead of the grid only gives up this \
credit, so it is the cheapest energy. Powerwall energy used on the EV costs whatever grid power later replaces it.

Hourly profile (7-day averages; house load excludes the EV; surplus = solar - house load):
hour | rate period | avg solar kW | avg house kW | avg surplus kW
{hourly_table}

Evening needs & history:
{history}

{instruction}

Strategy Guidelines:
1. 'battery_stop_pct' is the LOWER cutoff where EV charging must STOP to safeguard the evening home battery \
reserve ({reserve_note}).
2. 'battery_start_pct' is the Powerwall level from which a partial solar surplus may start charging, with \
the Powerwall supplying the rest. Lower starts charging earlier; higher waits for a fuller Powerwall.
3. CRITICAL CONSTRAINT: 'battery_start_pct' MUST be at least {gap:g} points higher than 'battery_stop_pct' \
(e.g. stop 40.0 -> start 55.0).

Recommend today's thresholds. Respond ONLY with a valid JSON object matching this schema \
(write the reasoning first, then the values):
{{
  "reasoning": "<brief explanation of the chosen thresholds, citing the history and rates>",
  "battery_start_pct": <float {start_lo:g}-{start_hi:g}, at least {gap:g} above battery_stop_pct>,
  "battery_stop_pct": <float {stop_lo:g}-{stop_hi:g}>,
  "daily_suggestion": "<concise 1-2 sentence advice on appliance scheduling and peak cost avoidance>"
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


def _goal_text() -> str:
    try:
        goal = charge_goal.get_goal()
        return charge_goal.describe(goal, charge_goal.progress(goal)) if goal else "none"
    except Exception as e:
        log.warning(f"Daily agent could not read the miles goal: {e}")
        return "unavailable"


def build_prompt(now: datetime, advice: dict, yesterday: dict, instruction: str, goal: str = "none") -> str:
    profile = advice.get("hourly_profile_7_days", [])
    reserve = advice.get("recommended_evening_battery_reserve_pct")
    start_lo, start_hi = PLAN_FIELDS["BATTERY_START_PCT"][2:]
    stop_lo, stop_hi = PLAN_FIELDS["BATTERY_STOP_PCT"][2:]
    return PROMPT.format(
        today=now.strftime("%A %Y-%m-%d"),
        day_type=_day_type(now),
        season=season(now.month),
        surplus_start=config.SOLAR_START_SURPLUS_KW,
        charger_kw=charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE),
        solar_min=config.SOLAR_MIN_KW,
        low_reserve=config.BATTERY_LOW_RESERVE_PCT,
        goal=goal,
        rate_plan=provider_label(),
        rate_table=_rate_table(now),
        export_rate=_export_credit_rate(),
        hourly_table=_hourly_table(now, profile),
        history=_history_section(advice, yesterday),
        instruction=f"USER INSTRUCTION OVERRIDE: {instruction}" if instruction else "No special user instructions today.",
        reserve_note=f"around {reserve}% needed at 16:00" if reserve is not None
        else f"history unavailable; current stop is {config.BATTERY_STOP_PCT}%",
        gap=MIN_HYSTERESIS_PCT,
        start_lo=start_lo, start_hi=start_hi, stop_lo=stop_lo, stop_hi=stop_hi,
    )


# ── Plan validation ─────────────────────────────────────────────────────────

def _apply_plan(plan: dict) -> list[str]:
    """Applies the returned thresholds, clamping each value into a safe range.

    Also enforces the start/stop hysteresis gap and keeps the stop above the
    critical reserve. Charging hours are decided live from solar and the
    Powerwall, so a leftover charge window or manual night blackout is
    cleared rather than left to block today's solar.
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

    window = (config.ALLOWED_CHARGE_START_HOUR, config.ALLOWED_CHARGE_END_HOUR)
    if window != (0, 24):
        changes["ALLOWED_CHARGE_START_HOUR"], changes["ALLOWED_CHARGE_END_HOUR"] = 0, 24
        applied.append(f"ALLOWED_CHARGE_WINDOW reset to 0-24 (was {window[0]}-{window[1]})")

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
    prompt = build_prompt(now, advice, yesterday, instruction, goal)

    try:
        plan = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily agent plan: {plan}")
        blackout = (config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR)
        log.info(f"Daily agent applied: {', '.join(_apply_plan(plan)) or 'no changes'}")
        blackout_note = (
            f"• <b>Night Blackout</b>: cleared (was {blackout[0]}:00 - {blackout[1]}:00) so live solar decides\n"
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
            f"🤖 <b>Daily AI Agent Update ({config.DAILY_AGENT_TIME})</b>\n\n{summary}"
            f"⚙️ <b>Today's Charging Strategy</b>:\n"
            f"• <b>Battery Start/Stop</b>: {config.BATTERY_START_PCT}% / {config.BATTERY_STOP_PCT}%\n"
            f"• <b>Charging</b>: live — starts on ≥{config.SOLAR_START_SURPLUS_KW:g} kW solar surplus with the "
            f"Powerwall ≥{config.BATTERY_START_PCT}%; night charging only for a miles goal\n"
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
