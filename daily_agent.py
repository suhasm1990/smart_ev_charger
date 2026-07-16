import json
from datetime import datetime
from google import genai
from google.genai import types

import config
from sheets_db import get_recent_logs, get_settings, clear_user_instruction
from notifications import notify
from logger import log

def run_daily_agent():
    log.info("Starting Daily Agent AI planner...")
    if not config.GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set. Daily Agent cannot run.")
        return

    # 1. Fetch 7 days of logs
    recent_logs = get_recent_logs(days=7)
    if not recent_logs:
        log.warning("No recent logs found in Google Sheets. Skipping Daily Agent.")
        return

    # Convert recent logs to json string
    logs_str = json.dumps(recent_logs)

    # 2. Check for User Instructions
    settings = get_settings()
    user_instruction = settings.get("USER_INSTRUCTION", "")

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    
    prompt = f"""
You are the AI Planner for a Smart EV Charger.
Goal: Maximize solar self-consumption, ensure the home battery does not drop below 20% by the end of the day, minimizing grid draw.

Here is the CSV data (in JSON format) for the home's power usage over the last 7 days (readings every 15 mins):
{logs_str}

{"USER INSTRUCTION OVERRIDE: " + user_instruction if user_instruction else "No special user instructions today."}

If there is a USER INSTRUCTION OVERRIDE, you MUST prioritize fulfilling it (e.g., by widening the charge window to 0-24 and dropping battery thresholds) over solar savings.

Recommend the optimal configuration for tomorrow.
Respond ONLY with a valid JSON object matching this schema exactly:
{{
  "battery_start_pct": <float>,
  "battery_stop_pct": <float>,
  "charge_window_start_hour": <int 0-24>,
  "charge_window_end_hour": <int 0-24>,
  "daily_suggestion": "<string, e.g. advice on when to run heavy appliances based on solar peaks>",
  "reasoning": "<string, why you chose these settings>"
}}
"""

    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2
            )
        )
        
        result = json.loads(response.text)
        log.info(f"Daily Agent Decision: {result}")
        
        # 3. Apply the settings
        config.BATTERY_START_PCT = float(result.get("battery_start_pct", config.BATTERY_START_PCT))
        config.BATTERY_STOP_PCT = float(result.get("battery_stop_pct", config.BATTERY_STOP_PCT))
        config.ALLOWED_CHARGE_START_HOUR = int(result.get("charge_window_start_hour", config.ALLOWED_CHARGE_START_HOUR))
        config.ALLOWED_CHARGE_END_HOUR = int(result.get("charge_window_end_hour", config.ALLOWED_CHARGE_END_HOUR))
        config.save_dynamic_config()
        
        if user_instruction:
            clear_user_instruction()
            
        # 4. Notify User
        msg = (
            f"🤖 **Daily AI Agent Update**\n"
            f"• **Charge Window**: {config.ALLOWED_CHARGE_START_HOUR}:00 - {config.ALLOWED_CHARGE_END_HOUR}:00\n"
            f"• **Battery Start/Stop**: {config.BATTERY_START_PCT}% / {config.BATTERY_STOP_PCT}%\n\n"
            f"💡 **Energy Suggestion for Tomorrow**: \n_{result.get('daily_suggestion', '')}_\n\n"
            f"*{result.get('reasoning', '')}*"
        )
        notify(msg)
        
    except Exception as e:
        log.error(f"Daily Agent failed: {e}")

if __name__ == "__main__":
    run_daily_agent()
