import json
from datetime import datetime

from core import config
from agent import llm_client
from services.sheets_db import get_recent_logs, get_settings, clear_user_instruction
from reporting.notifications import notify
from reporting.logger import log
from reporting.csv_logger import get_energy_saving_advice, get_home_energy_summary

def run_daily_agent():
    log.info("Starting Daily Agent AI planner...")
    llm_cfg = llm_client.resolve_llm_config()
    if not llm_cfg.get("api_key"):
        log.warning(f"No API key configured for LLM provider '{llm_cfg.get('provider')}'. Daily Agent cannot run.")
        return

    # 1. Fetch concise 7-day energy advice and yesterday's summary
    advice = get_energy_saving_advice()
    yest = get_home_energy_summary("yesterday")

    # 2. Check for User Instructions
    settings = get_settings()
    user_instruction = settings.get("USER_INSTRUCTION", "")

    yest_solar = yest.get("total_solar_generated_kwh", 0.0) if isinstance(yest, dict) else 0.0
    yest_grid = yest.get("total_grid_imported_kwh", 0.0) if isinstance(yest, dict) else 0.0
    yest_self_powered = yest.get("home_self_powered_percentage", 100.0) if isinstance(yest, dict) else 100.0
    yest_ev_kwh = yest.get("ev_charging_total_kwh", 0.0) if isinstance(yest, dict) else 0.0
    yest_ev_miles = yest.get("ev_estimated_miles_added", 0.0) if isinstance(yest, dict) else 0.0

    evening_load_kwh = advice.get("avg_evening_appliance_load_kwh", 5.0)
    evening_solar_kwh = advice.get("avg_evening_solar_generation_kwh", 2.0)
    evening_net_deficit_kwh = advice.get("avg_evening_net_battery_deficit_kwh", 3.5)
    target_evening_reserve_pct = advice.get("recommended_evening_battery_reserve_pct", 38.5)

    prompt = f"""You are the AI Planner for a Smart EV Charger.
Goal: Minimize electricity costs. Maximize solar self-consumption, protect the home battery reserve so heavy evening household loads (AC, induction stove, dishwasher from 16:00 to 22:00) run 100% on battery without pulling on-peak grid power ($0.35/kWh), and utilize cheap off-peak hours ($0.17/kWh) when necessary.

Home Energy & Load Profile:
• Optimal Daytime Solar Window: {advice.get('optimal_solar_appliance_window', '10:00 - 15:00')}
• Recommended EV Charge Window: {advice.get('cheapest_ev_charging_window', '10:00 - 15:00')}
• On-Peak Avoid Hours: {advice.get('hours_to_avoid_heavy_loads', '17:00 - 20:00')}
• Avg Evening Household Load (16:00-22:00 AC/Cooking/Dishwasher): {evening_load_kwh} kWh
• Expected Late-Afternoon/Evening Solar: {evening_solar_kwh} kWh
• Net Evening Battery Deficit to Cover: {evening_net_deficit_kwh} kWh
• Required Minimum Battery Reserve at 16:00: {target_evening_reserve_pct}%
• Yesterday Solar Generated: {yest_solar} kWh | Grid Imported: {yest_grid} kWh (Self-Powered: {yest_self_powered}%)
• Yesterday EV Charged: {yest_ev_kwh} kWh ({yest_ev_miles} miles added)

{f"USER INSTRUCTION OVERRIDE: {user_instruction}" if user_instruction else "No special user instructions today."}

Strategy Guidelines:
1. Ensure 'battery_stop_pct' is set high enough (e.g. {target_evening_reserve_pct}%) so EV charging stops with sufficient battery remaining for evening AC, cooking, and appliances.
2. If EV needs charging beyond daytime solar, it is safe to charge during cheap Off-Peak hours (night/morning at $0.17/kWh) rather than depleting the battery before evening peak.

Recommend today's optimal configuration.
Respond ONLY with a valid JSON object matching this schema:
{{
  "battery_start_pct": <float 40.0-65.0>,
  "battery_stop_pct": <float 20.0-55.0>,
  "charge_window_start_hour": <int 0-24>,
  "charge_window_end_hour": <int 0-24>,
  "daily_suggestion": "<concise 1-2 sentence advice for heavy appliance scheduling and peak cost avoidance>",
  "reasoning": "<brief explanation of chosen settings>"
}}"""

    try:
        result = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily Agent Decision: {result}")
        
        # 3. Apply the settings
        if result.get("battery_start_pct") is not None:
            config.BATTERY_START_PCT = float(result["battery_start_pct"])
        if result.get("battery_stop_pct") is not None:
            config.BATTERY_STOP_PCT = float(result["battery_stop_pct"])
        if result.get("charge_window_start_hour") is not None:
            config.ALLOWED_CHARGE_START_HOUR = int(result["charge_window_start_hour"])
        if result.get("charge_window_end_hour") is not None:
            config.ALLOWED_CHARGE_END_HOUR = int(result["charge_window_end_hour"])
        config.save_dynamic_config()
        
        if user_instruction:
            clear_user_instruction()
            
        # 4. Construct Yesterday's Summary Message
        yest_msg = ""
        if yest and "error" not in yest:
            yest_msg = (
                f"📊 <b>Yesterday's Energy & Bill Summary</b>:\n"
                f"• <b>Estimated Total Bill</b>: ${yest.get('estimated_total_mid_utility_bill_dollars', 0.0):.2f}\n"
                f"  - 🚗 <b>EV Charging Share</b>: ${yest.get('ev_charging_share_of_bill_dollars', 0.0):.2f}\n"
                f"  - 🏠 <b>Home Appliances Share</b>: ${yest.get('home_appliances_share_of_bill_dollars', 0.0):.2f}\n"
                f"• <b>Home Self-Powered</b>: {yest_self_powered}%\n"
                f"• <b>Solar Generated</b>: {yest_solar} kWh\n"
                f"• <b>Grid Imported</b>: {yest_grid} kWh\n\n"
            )

        # 5. Notify User via Telegram & Pushover
        msg = (
            f"🤖 <b>Daily AI Agent Update (7:00 AM)</b>\n\n"
            f"{yest_msg}"
            f"⚙️ <b>Today's Charging Strategy</b>:\n"
            f"• <b>Charge Window</b>: {config.ALLOWED_CHARGE_START_HOUR}:00 - {config.ALLOWED_CHARGE_END_HOUR}:00\n"
            f"• <b>Battery Start/Stop</b>: {config.BATTERY_START_PCT}% / {config.BATTERY_STOP_PCT}%\n\n"
            f"💡 <b>Energy Suggestion for Today</b>:\n<i>{result.get('daily_suggestion', '')}</i>\n\n"
            f"<i>{result.get('reasoning', '')}</i>"
        )
        notify(msg)
        
    except Exception as e:
        log.error(f"Daily Agent failed: {e}")

if __name__ == "__main__":
    run_daily_agent()
