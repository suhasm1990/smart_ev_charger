"""Morning AI planner: sets today's charge window and battery thresholds."""
from agent import llm_client
from core import config
from reporting.csv_logger import get_energy_saving_advice, get_home_energy_summary
from reporting.logger import log
from reporting.notifications import notify
from services.sheets_db import clear_user_instruction, get_settings

# Applied setting -> (LLM response key, cast). Bounds keep a bad completion safe.
PLAN_FIELDS = {
    "BATTERY_START_PCT":         ("battery_start_pct", float, 20.0, 100.0),
    "BATTERY_STOP_PCT":          ("battery_stop_pct", float, 10.0, 90.0),
    "ALLOWED_CHARGE_START_HOUR": ("charge_window_start_hour", int, 0, 24),
    "ALLOWED_CHARGE_END_HOUR":   ("charge_window_end_hour", int, 0, 24),
}

PROMPT = """You are the AI Planner for a Smart EV Charger.
Goal: minimise electricity cost. Maximise solar self-consumption, protect the home battery so heavy \
evening loads (AC, induction stove, dishwasher between 16:00 and 22:00) run on battery instead of \
on-peak grid power, and use cheap off-peak hours when extra charging is needed.

Home Energy & Load Profile:
• Optimal Daytime Solar Window: {solar_window}
• Recommended EV Charge Window: {charge_window}
• On-Peak Hours to Avoid: {avoid_window}
• Avg Evening Household Load (16:00-22:00): {evening_load} kWh
• Expected Late-Afternoon/Evening Solar: {evening_solar} kWh
• Net Evening Battery Deficit to Cover: {evening_deficit} kWh
• Required Minimum Battery Reserve at 16:00: {reserve_pct}%
• Yesterday Solar Generated: {yest_solar} kWh | Grid Imported: {yest_grid} kWh (Self-Powered: {yest_self}%)
• Yesterday EV Charged: {yest_ev_kwh} kWh ({yest_ev_miles} miles added)

{instruction}

Strategy Guidelines:
1. 'battery_stop_pct' is the LOWER cutoff limit (around {reserve_pct}%) where EV charging must STOP to safeguard evening home battery reserve.
2. 'battery_start_pct' is the UPPER start/resume threshold where EV charging is allowed to begin.
3. CRITICAL CONSTRAINT: 'battery_start_pct' MUST ALWAYS be strictly higher than 'battery_stop_pct' by at least 10-15% (e.g. if battery_stop_pct is 40.0%, battery_start_pct should be 55.0%).
4. If the EV needs more than daytime solar can give, prefer cheap off-peak hours over draining the battery before the evening peak.

Recommend today's optimal configuration. Respond ONLY with a valid JSON object matching this schema:
{{
  "battery_start_pct": <float 45.0-80.0, MUST be strictly greater than battery_stop_pct>,
  "battery_stop_pct": <float 20.0-50.0, MUST be strictly lower than battery_start_pct>,
  "charge_window_start_hour": <int 0-24>,
  "charge_window_end_hour": <int 0-24>,
  "daily_suggestion": "<concise 1-2 sentence advice on appliance scheduling and peak cost avoidance>",
  "reasoning": "<brief explanation of the chosen settings>"
}}"""


def _apply_plan(plan: dict) -> list[str]:
    """Applies the returned plan, clamping each value into a safe range and enforcing start_pct > stop_pct."""
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

    # Enforce hysteresis constraint: start threshold must always be strictly higher than stop threshold
    start = changes.get("BATTERY_START_PCT", config.BATTERY_START_PCT)
    stop = changes.get("BATTERY_STOP_PCT", config.BATTERY_STOP_PCT)
    if start <= stop:
        corrected_start = min(100.0, max(start, stop + 10.0))
        log.warning(
            f"Daily agent plan had invalid thresholds (start {start}% <= stop {stop}%). "
            f"Correcting BATTERY_START_PCT to {corrected_start}%."
        )
        changes["BATTERY_START_PCT"] = corrected_start
        applied.append(f"BATTERY_START_PCT={corrected_start} (corrected)")

    if changes:
        config.update(**changes)  # one atomic, validated, persisted transition
    return applied


def run_daily_agent():
    """Fetches yesterday's performance, asks the model for a plan, and applies it."""
    log.info("Starting the daily AI planner...")
    cfg = llm_client.resolve_llm_config()
    if not cfg.get("api_key"):
        log.warning(f"No API key for LLM provider '{cfg.get('provider')}'. Daily agent cannot run.")
        return

    advice = get_energy_saving_advice()
    yesterday = get_home_energy_summary("yesterday")
    if not isinstance(yesterday, dict):
        yesterday = {}
    instruction = get_settings().get("USER_INSTRUCTION", "")

    prompt = PROMPT.format(
        solar_window=advice.get("optimal_solar_appliance_window", "10:00 - 15:00"),
        charge_window=advice.get("cheapest_ev_charging_window", "10:00 - 15:00"),
        avoid_window=advice.get("hours_to_avoid_heavy_loads", "17:00 - 20:00"),
        evening_load=advice.get("avg_evening_appliance_load_kwh", 5.0),
        evening_solar=advice.get("avg_evening_solar_generation_kwh", 2.0),
        evening_deficit=advice.get("avg_evening_net_battery_deficit_kwh", 3.5),
        reserve_pct=advice.get("recommended_evening_battery_reserve_pct", 38.5),
        yest_solar=yesterday.get("total_solar_generated_kwh", 0.0),
        yest_grid=yesterday.get("total_grid_imported_kwh", 0.0),
        yest_self=yesterday.get("home_self_powered_percentage", 100.0),
        yest_ev_kwh=yesterday.get("ev_charging_total_kwh", 0.0),
        yest_ev_miles=yesterday.get("ev_estimated_miles_added", 0.0),
        instruction=f"USER INSTRUCTION OVERRIDE: {instruction}" if instruction else "No special user instructions today.",
    )

    try:
        plan = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily agent plan: {plan}")
        log.info(f"Daily agent applied: {', '.join(_apply_plan(plan)) or 'no changes'}")

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
            f"<i>{plan.get('reasoning', '')}</i>"
        )
    except Exception as e:
        log.error(f"Daily agent failed: {e}", exc_info=True)
        notify(f"⚠️ <b>Daily AI Agent Notice</b>\nThe planner failed to run: {e}")


if __name__ == "__main__":
    run_daily_agent()
