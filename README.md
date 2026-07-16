# Smart EV Charger Automation

An intelligent, fully automated Python daemon that optimally charges your EV based on solar production, Tesla Powerwall battery levels, and Time-Of-Use (TOU) rates. 

Designed specifically to maximize free solar energy usage and strictly prevent expensive grid consumption during peak evening hours.

## 🌟 Key Features

- **Solar & Battery Synchronized:** Integrates directly with the NetZero API to read your Tesla Powerwall state. It acts as a "daytime solar sponge," only charging your EV when your house battery is healthy (e.g., `> 40%`) and stopping when it dips too low (e.g., `< 25%`).
- **TOU Rate Optimization:** Implements a customized nighttime blackout window (default 4 PM to 9 AM) to prevent the EV from draining your Powerwall overnight or pulling from the grid during expensive evening Peak hours.
- **AI Daily Planner:** Uses Google Gemini AI to analyze your past 7 days of solar production logs and automatically adjusts your charge window and battery thresholds every night at 11:50 PM.
- **Telegram Bot Control:** Talk directly to your charger via Telegram! You can start/stop charging, check status, or give the AI special instructions (like "Prioritize charging tonight for a road trip").
- **Google Sheets Sync:** All logs and active AI settings are seamlessly synced to a Google Sheet using a Service Account for easy viewing and tracking.
- **Emergency Safety Overrides:** Automatically halts EV charging if your Powerwall goes Off-Grid (`island_mode`) or if Tesla activates Storm Watch, protecting your home's backup reserve.

---

## 🛠 Setup & Installation

### Requirements
- Python 3.10+
- ChargePoint Home Flex (and account credentials)
- Tesla Powerwall (via NetZero API)
- Pushover (for push notifications)
- Google Sheets (for remote control webhook)

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

Open `.env` and configure your settings. You can tweak the behavioral thresholds directly in this file:
- `BATTERY_START_PCT`: Minimum Powerwall % required to start charging (Default: `40`).
- `BATTERY_STOP_PCT`: Powerwall % where charging is forced to stop (Default: `25`).
- `CHECK_INTERVAL_MINUTES`: How often the automation loop runs (Default: `15`).

---

## 🚀 Usage

To run the automation daemon manually:
```bash
python main.py
```

To run it continuously in the background using Docker (recommended for 24/7 server deployment):
```bash
docker-compose up -d
```

## 🧠 How the Logic Works

The script operates on a continuous cycle (default every 15 minutes):
1. It queries the **NetZero API** to check your Powerwall battery percentage, solar production, and grid draw.
2. It queries the **ChargePoint API** to check if the car is plugged in or actively charging.
3. It evaluates a custom state-machine (`decision.py`) to determine if it should `start`, `stop`, or `hold` the charge based on the time of day, current battery levels, and safety overrides.
4. It logs the decision (and estimated grid cost) to Google Sheets and sleeps until the next cycle.

---

## 🤖 Telegram Bot & AI Features

The integration with Telegram provides a powerful natural language interface to your Smart Charger:

### Standard Commands
- `/status` - Get a real-time snapshot of your home's energy (Battery %, Solar kW, Grid kW) and the EV charger state.
- `/start` - Manually start the EV charger.
- `/stop` - Manually stop the EV charger.
- `/manual` - Pause all automation so you can control the charger natively from the ChargePoint app.
- `/auto` - Resume normal automation.
- `/config` - View the active AI-chosen thresholds (Battery limits, Charge Windows).

### Natural Language AI Instructions
You don't need to use slash commands! You can talk to the bot naturally. The Telegram bot will forward your message to the **Daily AI Agent**, which will intelligently adjust your thresholds at 11:50 PM. 

Here are some examples of what you can say:

- **For a road trip (Ignore solar, charge ASAP):**
  > *"I have a long drive tomorrow, please make sure the car charges fully tonight no matter what."*
- **For strict savings (Only charge on pure solar):**
  > *"I want to be super aggressive about solar savings tomorrow. Only charge the car if the battery is above 70% and stop immediately if it dips below 50%."*
- **For skipping a day (Don't charge):**
  > *"I'm working from home tomorrow and won't need the car. Don't bother charging it at all."*
- **For weather anticipation:**
  > *"It's going to be really cloudy tomorrow morning, so please push the charge window to the late afternoon."*

At 11:50 PM, the AI will read your instruction, look at your historical solar logs, dynamically expand or restrict your charging windows and battery thresholds, and text you a summary of its plan!
