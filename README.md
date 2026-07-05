# Smart EV Charger Automation

An intelligent, fully automated Python daemon that optimally charges your EV based on solar production, Tesla Powerwall battery levels, and Time-Of-Use (TOU) rates. 

Designed specifically to maximize free solar energy usage and strictly prevent expensive grid consumption during peak evening hours.

## 🌟 Key Features

- **Solar & Battery Synchronized:** Integrates directly with the NetZero API to read your Tesla Powerwall state. It acts as a "daytime solar sponge," only charging your EV when your house battery is healthy (e.g., `> 40%`) and stopping when it dips too low (e.g., `< 25%`).
- **TOU Rate Optimization:** Implements a customized nighttime blackout window (default 4 PM to 9 AM) to prevent the EV from draining your Powerwall overnight or pulling from the grid during expensive evening Peak hours (tailored for rates like the Modesto Irrigation District EV-D).
- **Physical State Sync:** Pings the ChargePoint API every 15 minutes to stay perfectly synchronized. If you manually plug in or start a session from the native ChargePoint mobile app, the script instantly detects and adopts the session.
- **Emergency Safety Overrides:** Automatically halts EV charging if your Powerwall goes Off-Grid (`island_mode`) or if Tesla activates Storm Watch, protecting your home's backup reserve.
- **Google Sheets Remote Control:** Provides a dead-simple manual override. Just type `manual` into cell A1 of a linked Google Sheet to pause automation, and `auto` to resume.
- **Detailed CSV Logging:** Logs all charging sessions, exact grid draw, and estimates grid costs based on the current TOU rate tier.

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
4. It logs the decision (and estimated grid cost) to `logs/charger_log.csv` and sleeps until the next cycle.
