# Smart EV Charger Automation

An intelligent, fully automated Python daemon that optimally charges your EV based on solar production, Tesla Powerwall battery levels, ChargePoint hardware metrics, and Time-Of-Use (TOU) rates. 

Designed specifically to maximize free solar energy usage, isolate EV charging costs from general home loads (like AC or washing machines), provide personalized bill reduction advice, and dynamically calculate electric bills matching your utility provider's rates.

---

## 🌟 Key Features

- ☀️ **Solar & Battery Synchronized:** Integrates directly with the **NetZero API** to read your Tesla Powerwall state in real time. It acts as a "daytime solar sponge," only charging your EV when your house battery is healthy (e.g. `> 40%`) and stopping when it dips too low (e.g. `< 25%`).
- ⚡ **ChargePoint Hardware Integration:** Directly interfaces with your ChargePoint Home Flex charger to monitor real-time charging power (`kW`), total energy delivered (`kWh`), driving range added (`miles`), and dynamic amperage limits (8A–32A).
- ⚙️ **Dynamic Utility Provider Rate Engine (MID, PG&E, or Custom):** Easily switch utility rate schedules by setting `UTILITY_PROVIDER` (`MID`, `PGE`, or `CUSTOM`) in `.env`. Supports configurable fixed monthly service fees, volumetric surcharges, local tax multipliers, and NEM solar export credits.
- 🧠 **Daily 7:00 AM AI Planner & Morning Briefing:** Every morning at **7:00 AM**, the AI Planner analyzes your past 7 days of solar production and home load logs, processes any special user instructions sent via Telegram, and sends a comprehensive morning update:
  - 📊 **Yesterday's Energy & Bill Summary**: Total estimated electric bill ($), EV charging share ($), home appliances share ($), self-powered percentage (%), solar kWh generated, and grid kWh imported.
  - ⚙️ **Today's Charging Strategy**: Selected charge windows (`ALLOWED_CHARGE_START_HOUR` – `ALLOWED_CHARGE_END_HOUR`) and Powerwall battery start/stop thresholds.
  - 💡 **AI Energy Suggestions & Reasoning**: Actionable appliance scheduling recommendations tailored for the upcoming day's solar generation forecast.
- 💡 **AI Bill Reduction & Appliance Scheduling Advice:** Analyzes 7 days of solar production and home consumption logs to determine the best hours for running heavy appliances (AC, washing machine, dishwasher, dryer) and charging the car for free.
- 💰 **EV vs. Home Cost Isolation & Tracking:** Intelligently isolates the EV charger's grid energy draw from heavy home appliances (AC, washing machine, fridge) to calculate exact grid kWh pulled, grid costs ($), solar energy used, and solar savings ($) for **today**, **yesterday**, **this week**, or **this month**.
- 🕐 **TOU & Night Blackout Optimization:** Implements a customized weekday nighttime blackout window (default 4 PM to 9 AM) to prevent the EV from draining your Powerwall overnight or pulling from the grid during expensive Peak rate hours.
- 🤖 **Gemini AI Telegram Assistant:** Natural-language conversational interface powered by Google Gemini function calling. Ask questions like:
  - *"How can I reduce my electric bill?"*
  - *"When is the best time to run my washing machine or AC?"*
  - *"Why did charging stop last time?"*
  - *"How much did EV charging cost me this week?"*
  - *"What is my total electric bill for this month?"*
  - *"Charge with full power (32A)"*
- 📊 **Google Sheets & CSV Sync:** Every 15-minute cycle logs detailed metrics (Solar kW, Home kW, Grid kW, Battery %, Charger State, Action, Reason, TOU Rate, and Grid Cost) to `logs/charger_log.csv` and Google Sheets.
- 🛡️ **Emergency Safety Overrides:** Automatically halts EV charging if your Powerwall goes Off-Grid (`island_mode`) or if Tesla activates Storm Watch mode.

---

## 🛠 Setup & Installation

### Requirements
- Python 3.10+
- ChargePoint Home Flex (and account credentials)
- Tesla Powerwall (via NetZero API)
- Telegram Bot Token & Google Gemini API Key
- Google Sheets Service Account (optional, for cloud sync)

### 1. Local Setup

Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/suhasm1990/smart_ev_charger.git
cd smart_ev_charger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration

Copy the example environment file and fill in your API credentials:

```bash
cp .env.example .env
```

Open `.env` and configure your credentials and behavioral thresholds:

```env
# NetZero Energy API (Tesla Powerwall)
NETZERO_SITE_ID=your_site_id
NETZERO_API_TOKEN=your_token

# ChargePoint API
CHARGEPOINT_USERNAME=your_email
CHARGEPOINT_COULOMB_TOKEN=your_coulomb_token
CHARGEPOINT_DEVICE_ID=your_device_id

# Telegram AI Bot & Gemini
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ALLOWED_USER_ID=your_user_id
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-flash-latest

# Schedule Config
DAILY_RESET_TIME=00:00
DAILY_AGENT_TIME=07:00

# Utility Rate Provider (MID, PGE, or CUSTOM)
UTILITY_PROVIDER=MID
UTILITY_FIXED_MONTHLY_FEE=32.00
UTILITY_VOLUMETRIC_ADDER=0.0151
UTILITY_TAX_MULTIPLIER=1.065
UTILITY_SOLAR_EXPORT_CREDIT_RATE=0.076

# Charging & Battery Thresholds
BATTERY_START_PCT=40
BATTERY_STOP_PCT=25
BATTERY_LOW_RESERVE_PCT=15
DEFAULT_CHARGER_AMPERAGE=20
MAX_CHARGER_AMPERAGE=32

# TOU Blackout Window (24h format)
NIGHT_BLACKOUT_START_HOUR=16
NIGHT_BLACKOUT_END_HOUR=9
CHECK_INTERVAL_MINUTES=15
GRID_EXPORT_ALERT_THRESHOLD_KW=1.0
```

---

## 🚀 Usage

To run the automation daemon locally:
```bash
python main.py
```

To run continuously in the background using Docker:
```bash
docker-compose up -d
```

---

## 🤖 Telegram Bot AI Capabilities & Daily AI Planner

Text your charger naturally via Telegram! Powered by Gemini function calling:

### 🧠 Daily 7:00 AM Morning AI Briefing
Every morning at **7:00 AM**, the Daily AI Planner automatically sends a Telegram update containing:
- 📊 **Yesterday's Energy & Bill Summary** (Total bill $, EV charging share $, home appliances share $, self-powered %, solar kWh, grid kWh).
- ⚙️ **Today's Charging Strategy** (Dynamically selected charge window and battery thresholds).
- 💡 **AI Energy Suggestion & Reasoning** (Personalized advice on when to run heavy appliances based on solar production).

### 💡 Bill Reduction & Appliance Advice
- *"How can I reduce my electric bill?"*
- *"When should I run my washing machine, dryer, or AC?"*
- *"When is the best time to charge my car for free?"*

### 💰 Cost & Energy Inquiries
- *"How much did EV charging cost me today / this week / this month?"*
- *"How many grid units (kWh) did I pull for charging this week?"*
- *"How much electricity did my home consume today and what is my total electric bill?"*

### 📊 Session & History Lookup
- *"Why did charging stop last time?"*
- *"When was the car charger stopped last and why?"*
- *"What was the previous session charge time?"*

### ⚡ Amperage & Charging Overrides
- *"Charge with full power"* → Sets charger amperage to **32A** and forces manual charging.
- *"Set default charging speed"* → Sets charger amperage to **20A**.
- *"Force start the charger"* / *"Force stop the charger"*

---

## 📁 Repository Structure

```
smart_ev_charger/
├── main.py              # Main automation loop & scheduled tasks (Daily Agent at 07:00 AM)
├── daily_agent.py       # Daily AI Planner & morning energy briefing generator
├── decision.py          # State-machine evaluator (safety, battery, solar rules)
├── telegram_bot.py      # Telegram Bot & Gemini AI function calling tools
├── csv_logger.py        # CSV logging, session history, cost & bill advice tools
├── api_chargepoint.py   # Async ChargePoint API client with error sanitization
├── api_netzero.py       # Tesla Powerwall stats client via NetZero API
├── manual_override.py   # Manual override tracking & 9 AM daily auto-reset
├── tou.py               # TOU rates (MID / PG&E / Custom), surcharges & schedule calculator
├── config.py            # Configuration loader & dynamic JSON state manager
├── alerts.py            # Custom dynamic notifications & grid export alerts
├── sheets_db.py         # Google Sheets synchronization
├── notifications.py     # Pushover & Telegram notification client
├── requirements.txt     # Python dependencies
└── docker-compose.yml   # Docker container setup
```
