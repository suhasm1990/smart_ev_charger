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

    # 1. Fetch concise 7-day energy advice and recent summary
    advice = get_energy_saving_advice()
    recent_summary = get_home_energy_summary("yesterday")

    # 2. Check for User Instructions
    settings = get_settings()
    user_instruction = settings.get("USER_INSTRUCTION", "")

    prompt = f"""
You are the AI Planner for a Smart EV Charger.
Goal: Maximize solar self-consumption, ensure the home battery does not drop below 20% by the end of the day, and minimize utility grid draw.

Home Energy Profile (Last 7 Days):
• Optimal Solar Surplus Window: {advice.get('optimal_solar_appliance_window', '10:00 - 15:00')}
• Recommended EV Charging Window: {advice.get('cheapest_ev_charging_window', '10:00 - 15:00')}
• On-Peak Avoid Hours: {advice.get('hours_to_avoid_heavy_loads', '17:00 - 20:00')}
• Yesterday Solar Generated: {recent_summary.get('total_solar_generated_kwh', 'N/A')} kWh
• Yesterday Grid Imported: {recent_summary.get('total_grid_imported_kwh', 'N/A')} kWh
• Yesterday Self-Powered: {recent_summary.get('home_self_powered_percentage', 'N/A')}%

{"USER INSTRUCTION OVERRIDE: " + user_instruction if user_instruction else "No special user instructions today."}

If there is a USER INSTRUCTION OVERRIDE, prioritize fulfilling it (e.g. widening charge window or dropping battery thresholds) over solar savings.

Recommend the optimal configuration for today.
Respond ONLY with a valid JSON object matching this schema exactly:
{{
  "battery_start_pct": <float between 40 and 60>,
  "battery_stop_pct": <float between 20 and 35>,
  "charge_window_start_hour": <int 0-24>,
  "charge_window_end_hour": <int 0-24>,
  "daily_suggestion": "<concise advice on when to run heavy appliances based on solar peaks>",
  "reasoning": "<brief explanation of chosen settings>"
}}
"""

    try:
        result = llm_client.generate_json(prompt=prompt)
        log.info(f"Daily Agent Decision: {result}")
        
        # 3. Apply the settings
        config.BATTERY_START_PCT = float(result.get("battery_start_pct", config.BATTERY_START_PCT))
        config.BATTERY_STOP_PCT = float(result.get("battery_stop_pct", config.BATTERY_STOP_PCT))
        config.ALLOWED_CHARGE_START_HOUR = int(result.get("charge_window_start_hour", config.ALLOWED_CHARGE_START_HOUR))
        config.ALLOWED_CHARGE_END_HOUR = int(result.get("charge_window_end_hour", config.ALLOWED_CHARGE_END_HOUR))
        config.save_dynamic_config()
        
        if user_instruction:
            clear_user_instruction()
            
        # 4. Fetch Yesterday's Energy & Cost Summary
        try:
            yest = get_home_energy_summary("yesterday")
        except Exception as yest_err:
            log.warning(f"Daily Agent could not fetch yesterday summary: {yest_err}")
            yest = {}

        yest_msg = ""
        if yest and "error" not in yest:
            yest_msg = (
                f"📊 <b>Yesterday's Energy & Bill Summary</b>:\n"
                f"• <b>Estimated Total Bill</b>: ${yest.get('estimated_total_mid_utility_bill_dollars', 0.0):.2f}\n"
                f"  - 🚗 <b>EV Charging Share</b>: ${yest.get('ev_charging_share_of_bill_dollars', 0.0):.2f}\n"
                f"  - 🏠 <b>Home Appliances Share</b>: ${yest.get('home_appliances_share_of_bill_dollars', 0.0):.2f}\n"
                f"• <b>Home Self-Powered</b>: {yest.get('home_self_powered_percentage', 100.0)}%\n"
                f"• <b>Solar Generated</b>: {yest.get('total_solar_generated_kwh', 0.0)} kWh\n"
                f"• <b>Grid Imported</b>: {yest.get('total_grid_imported_kwh', 0.0)} kWh\n\n"
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
