"""Morning AI planner: sets today's charge window and battery thresholds."""
from datetime import datetime

from agent import llm_client
from core import config
from core.decision import is_in_charge_window
from core.state import charger_power_kw
from core.tou import (
    get_tou_period,
    get_tou_rate,
    is_in_night_blackout,
    is_utility_holiday,
    is_weekend,
    provider_label,
    season,
)
from reporting.csv_logger import _export_credit_rate, get_energy_saving_advice, get_home_energy_summary
from reporting.logger import log
from reporting.notifications import notify
from services.sheets_db import clear_user_instruction, get_settings
from services.weather import get_solar_forecast

# Applied setting -> (LLM response key, cast, low, high). The prompt quotes
# these same bounds, so the model is never asked for a value we then clamp.
PLAN_FIELDS = {
    "BATTERY_START_PCT":         ("battery_start_pct", float, 45.0, 80.0),
    "BATTERY_STOP_PCT":          ("battery_stop_pct", float, 20.0, 50.0),
    "ALLOWED_CHARGE_START_HOUR": ("charge_window_start_hour", int, 0, 23),
    "ALLOWED_CHARGE_END_HOUR":   ("charge_window_end_hour", int, 1, 24),
}
MIN_HYSTERESIS_PCT = 10.0  # start must exceed stop by this much to avoid short-cycling
MIN_FORECAST_RADIATION_WM2 = 50.0  # below this an hour's irradiance ratio is noise

PROMPT = """You are the AI Planner for a Smart EV Charger.
Goal: minimise electricity cost. Maximise solar self-consumption, protect the home battery so heavy \
evening loads (AC, induction stove, dishwasher between 16:00 and 22:00) run on battery instead of \
on-peak grid power, and use cheap hours when extra charging is needed.

Today: {today} ({day_type}, {season} rates)

How the charger is controlled (code enforces these rules; plan within them):
• Charging can only happen inside your charge window AND outside the night blackout. Night blackout today: {blackout}.
• Hours when charging is possible today: {chargeable}. A window outside these hours does nothing.
• Inside those hours, charging starts once the Powerwall is at or above battery_start_pct and stops when it \
falls below battery_stop_pct. It does not follow solar surplus: whenever surplus is below the charger's draw, \
the rest comes from the Powerwall (or the grid once the Powerwall is low).
• The charger draws {charger_kw} kW ({amps} A at {volts:g} V).
• Charging always stops below the {low_reserve}% critical Powerwall reserve.

Utility rates today ({rate_plan}, delivered price incl. surcharges):
{rate_table}
• Solar export credit: ${export_rate:.3f}/kWh. Solar sent to the EV instead of the grid only gives up this \
credit, so it is the cheapest energy. Powerwall energy used on the EV costs whatever grid power later replaces it.

Solar forecast: {forecast_summary}

Hourly profile (7-day averages; house load excludes the EV; forecast solar = average solar scaled by \
today's forecast irradiance; surplus = forecast solar - house load):
hour | rate period | chargeable | avg solar kW | avg house kW | forecast solar kW | forecast surplus kW | cloud %
{hourly_table}

Evening needs & history:
{history}

{instruction}

Strategy Guidelines:
1. 'battery_stop_pct' is the LOWER cutoff where EV charging must STOP to safeguard the evening home battery \
reserve ({reserve_note}). On a cloudy forecast, keep it higher, because the Powerwall will refill less before 16:00.
2. 'battery_start_pct' is the UPPER threshold where EV charging is allowed to begin.
3. CRITICAL CONSTRAINT: 'battery_start_pct' MUST be at least {gap:g} points higher than 'battery_stop_pct' \
(e.g. stop 40.0 -> start 55.0).
4. Put the charge window over the chargeable hours where forecast surplus is largest. If the EV needs more \
than solar can give, prefer the cheapest chargeable hours over draining the battery before the evening peak.

Recommend today's optimal configuration. Respond ONLY with a valid JSON object matching this schema \
(write the reasoning first, then the values):
{{
  "reasoning": "<brief explanation of the chosen settings, citing the forecast and rates>",
  "battery_start_pct": <float {start_lo:g}-{start_hi:g}, at least {gap:g} above battery_stop_pct>,
  "battery_stop_pct": <float {stop_lo:g}-{stop_hi:g}>,
  "charge_window_start_hour": <int 0-23, must overlap the chargeable hours>,
  "charge_window_end_hour": <int 1-24, exclusive; may be lower than the start hour to wrap midnight>,
  "daily_suggestion": "<concise 1-2 sentence advice on appliance scheduling and peak cost avoidance>"
}}"""


# ── Prompt context ──────────────────────────────────────────────────────────

def _at_hour(now: datetime, hour: int) -> datetime:
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def _chargeable_hours(now: datetime) -> list[int]:
    """Hours of `now`'s day the night blackout leaves open (mirrors decision.evaluate)."""
    return [h for h in range(24)
            if not (is_in_night_blackout(_at_hour(now, h)) and not is_weekend(now))]


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


def _blackout_description(now: datetime) -> str:
    start, end = config.NIGHT_BLACKOUT_START_HOUR, config.NIGHT_BLACKOUT_END_HOUR
    if start == end:
        return "disabled"
    if is_weekend(now):
        return f"not applied on weekends (weekdays {start}:00-{end}:00)"
    return f"{start}:00-{end}:00"


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


def _forecast_solar(profile: list[dict], forecast: dict) -> dict[int, float]:
    """Scales each hour's 7-day average solar output by today's forecast irradiance.

    Hours too dim for a stable ratio fall back to the whole-day ratio; ratios
    are capped so one unusually dark week cannot inflate the forecast.
    """
    today, past = forecast["radiation_wm2"], forecast["past_avg_radiation_wm2"]
    past_total = sum(past.get(h, 0.0) for h in today)
    day_ratio = sum(today.values()) / past_total if past_total else 1.0
    result = {}
    for row in profile:
        hour = row["hour"]
        if hour not in today:
            continue
        base = past.get(hour, 0.0)
        ratio = today[hour] / base if base >= MIN_FORECAST_RADIATION_WM2 else day_ratio
        result[hour] = round(row["solar_kw"] * min(1.5, max(0.0, ratio)), 2)
    return result


def _forecast_summary(profile: list[dict], forecast: dict | None, solar: dict[int, float]) -> str:
    if forecast is None:
        return "unavailable today; plan from the 7-day averages below."
    today, past = forecast["radiation_wm2"], forecast["past_avg_radiation_wm2"]
    past_total = sum(past.get(h, 0.0) for h in today)
    pct = round(sum(today.values()) / past_total * 100) if past_total else 100
    daylight = [forecast["cloud_cover_pct"][h] for h, rad in today.items() if rad > 0]
    cloud = round(sum(daylight) / len(daylight)) if daylight else 0
    expected = sum(solar.values())
    usual = sum(p["solar_kw"] for p in profile)
    return (f"today's irradiance is {pct}% of the past week's average, with {cloud}% average daytime cloud "
            f"cover; expected solar ≈ {expected:.1f} kWh vs {usual:.1f} kWh on an average recent day.")


def _hourly_table(now: datetime, profile: list[dict], forecast: dict | None,
                  solar: dict[int, float], chargeable: list[int]) -> str:
    rows = []
    for p in profile:
        hour = p["hour"]
        if p["solar_kw"] <= 0.05 and hour not in chargeable:
            continue  # dark, unchargeable hours tell the planner nothing
        at = _at_hour(now, hour)
        forecast_kw = solar.get(hour)
        cloud = forecast["cloud_cover_pct"].get(hour) if forecast else None
        rows.append(" | ".join((
            f"{hour:02d}:00",
            f"{get_tou_period(at).replace('_', '-')} ${get_tou_rate(at):.3f}",
            "yes" if hour in chargeable else "no",
            f"{p['solar_kw']:.1f}",
            f"{p['house_kw']:.1f}",
            f"{forecast_kw:.1f}" if forecast_kw is not None else "n/a",
            f"{forecast_kw - p['house_kw']:+.1f}" if forecast_kw is not None else "n/a",
            f"{cloud}%" if cloud is not None else "n/a",
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


def build_prompt(now: datetime, advice: dict, yesterday: dict, forecast: dict | None, instruction: str) -> str:
    profile = advice.get("hourly_profile_7_days", [])
    solar = _forecast_solar(profile, forecast) if forecast else {}
    chargeable = _chargeable_hours(now)
    reserve = advice.get("recommended_evening_battery_reserve_pct")
    start_lo, start_hi = PLAN_FIELDS["BATTERY_START_PCT"][2:]
    stop_lo, stop_hi = PLAN_FIELDS["BATTERY_STOP_PCT"][2:]
    return PROMPT.format(
        today=now.strftime("%A %Y-%m-%d"),
        day_type=_day_type(now),
        season=season(now.month),
        blackout=_blackout_description(now),
        chargeable=_format_ranges(chargeable),
        charger_kw=charger_power_kw(config.DEFAULT_CHARGER_AMPERAGE),
        amps=config.DEFAULT_CHARGER_AMPERAGE,
        volts=config.CHARGER_VOLTAGE,
        low_reserve=config.BATTERY_LOW_RESERVE_PCT,
        rate_plan=provider_label(),
        rate_table=_rate_table(now),
        export_rate=_export_credit_rate(),
        forecast_summary=_forecast_summary(profile, forecast, solar),
        hourly_table=_hourly_table(now, profile, forecast, solar, chargeable),
        history=_history_section(advice, yesterday),
        instruction=f"USER INSTRUCTION OVERRIDE: {instruction}" if instruction else "No special user instructions today.",
        reserve_note=f"around {reserve}% needed at 16:00" if reserve is not None
        else f"history unavailable; current stop is {config.BATTERY_STOP_PCT}%",
        gap=MIN_HYSTERESIS_PCT,
        start_lo=start_lo, start_hi=start_hi, stop_lo=stop_lo, stop_hi=stop_hi,
    )


# ── Plan validation ─────────────────────────────────────────────────────────

def _apply_plan(plan: dict, now: datetime = None) -> list[str]:
    """Applies the returned plan, clamping each value into a safe range.

    Also enforces the start/stop hysteresis gap, keeps the stop above the
    critical reserve, and replaces a charge window that would never open
    (start == end, or no overlap with today's chargeable hours).
    """
    now = now or datetime.now(config.TZ)
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

    start_hr = changes.get("ALLOWED_CHARGE_START_HOUR", config.ALLOWED_CHARGE_START_HOUR)
    end_hr = changes.get("ALLOWED_CHARGE_END_HOUR", config.ALLOWED_CHARGE_END_HOUR)
    if not any(is_in_charge_window(h, start_hr, end_hr) for h in _chargeable_hours(now)):
        log.warning(
            f"Daily agent window {start_hr}:00-{end_hr}:00 has no chargeable hours today; "
            f"falling back to the full day (blackout and battery rules still apply)."
        )
        changes["ALLOWED_CHARGE_START_HOUR"], changes["ALLOWED_CHARGE_END_HOUR"] = 0, 24
        applied.append("ALLOWED_CHARGE_WINDOW=0-24 (corrected: window never opened)")

    if changes:
        config.update(**changes)  # one atomic, validated, persisted transition
    return applied


def run_daily_agent():
    """Fetches history, today's rates and forecast, asks the model for a plan, and applies it."""
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
    prompt = build_prompt(now, advice, yesterday, get_solar_forecast(now), instruction)

    try:
        plan = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily agent plan: {plan}")
        log.info(f"Daily agent applied: {', '.join(_apply_plan(plan, now)) or 'no changes'}")

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
            f"• <b>Charge Window</b>: {config.ALLOWED_CHARGE_START_HOUR}:00 - {config.ALLOWED_CHARGE_END_HOUR}:00\n"
            f"• <b>Battery Start/Stop</b>: {config.BATTERY_START_PCT}% / {config.BATTERY_STOP_PCT}%\n\n"
            f"💡 <b>Energy Suggestion for Today</b>:\n<i>{plan.get('daily_suggestion', '')}</i>\n\n"
            f"<i>{plan.get('reasoning', '')}</i>\n\n"
            f"{llm_client.model_label()}"
        )
    except Exception as e:
        log.error(f"Daily agent failed: {e}", exc_info=True)
        notify(f"⚠️ <b>Daily AI Agent Notice</b>\nThe planner failed to run: {e}")


if __name__ == "__main__":
    run_daily_agent()
