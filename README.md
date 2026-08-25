# Smart EV Charger Automation ⚡☀️

An intelligent, model-agnostic Python daemon that automatically charges your Electric Vehicle using free solar power and Tesla Powerwall battery reserves, avoiding expensive peak utility rates.

---

## 🌟 Key Features

- ☀️ **Solar & Powerwall Synchronization**: Continuously monitors Tesla Powerwall battery levels and solar surplus via NetZero API. Automatically starts charging when house battery is healthy (e.g. `> 40%`) and stops when reserves drop (e.g. `< 25%`).
- ⚡ **ChargePoint Flex Control & Dynamic Amperage**: Controls ChargePoint Home Flex amperage (8A–32A) and tracks exact session duration (`minutes`), delivered energy (`kWh`), and driving range added (`miles`) scaled dynamically to active amperage.
- 🛡️ **Guarded Manual / Boost Mode**: Force charge at 32A (full power) with smart auto-stop guardrails (e.g. *"Charge at 32A until 16:00 or stop if battery drops below 20%"*). Automatically restores default 20A amperage when returning to Auto mode.
- 🧠 **Model-Agnostic AI Assistant**: Conversational Telegram interface powered by your choice of AI model (**Gemini**, **NVIDIA Nemotron**, **OpenAI**, or **Claude**).
- 🌅 **7:00 AM Morning AI Planner**: Automatically evaluates yesterday's energy bills and optimizes today's charging window & battery reserve thresholds based on evening appliance loads.
- 📊 **Monthly PNG Bill Infographic**: Automatically generates and sends high-resolution energy & utility bill cards on the 1st of every month (or on-demand).
- 📈 **Optimized Google Sheets Cloud Database**:
  - **`Telemetry` Tab**: 6,000-row rolling ring buffer (~62 days of 15-minute resolution metrics).
  - **`System Logs` Tab**: 500-row rolling ring buffer of events, AI daily plans, warnings, and errors.
  - **`Settings` Tab**: Real-time cloud sync for user instructions and dynamic configurations.
  - **Zero-Bloat Caching**: 15-minute worksheet handle caching, chunked auto-trimming, and 60-second read TTL cache to stay well below Google API rate limits.
- 📋 **Remote Log Inspection (`/logs`)**: Inspect live system events, AI plans, and errors directly on your phone via Telegram (`/logs 30`) without needing to fetch NAS log files.
- 🚗 **Miles & Range Tracking**: Calculates driving range added per session, day, week, or month using configurable vehicle efficiency (`EV_MILES_PER_KWH`).
- 🛠️ **Autonomous Dev Agent & Instant Updates**: Instruct the bot to investigate logs, inspect source code, or open GitHub Pull Requests. Deploy updates instantly with `/update`.
- 💰 **EV vs. Home Load Isolation**: Intelligently separates EV charger consumption from heavy household appliances (AC, laundry) for exact utility cost tracking.

---

## 🚀 Quick Setup & Deployment

### Local Setup
```bash
git clone https://github.com/suhasm1990/smart_ev_charger.git
cd smart_ev_charger
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

### Docker Deployment
```bash
docker-compose up -d
```

---

## 🤖 Telegram Commands & Natural Language Prompts

| User Prompt / Command | Description |
| :--- | :--- |
| `/logs` or `/logs 30 ERROR` | View recent system events, AI daily plans, or errors directly in Telegram |
| `/model` or *"Switch model to nvidia"* | View or switch active AI model (Gemini, NVIDIA Nemotron, GPT-4o, Claude) |
| `/update` or `/restart` | Restarts container, pulls latest GitHub code, and comes online in ~5s |
| `/daily_agent` or *"Run daily agent"* | Runs morning AI planner to optimize charging strategy & thresholds |
| `/monthly_report` or *"Report for July"* | Generates & sends high-resolution PNG utility bill infographic |
| *"How many miles were added today?"* | Calculates total driving range (miles) and energy (kWh) added |
| *"Charge with full power"* | Forces 32A charging (reverts to 20A on completion/auto-return) |
| *"Charge at 32A until 16:00"* | Forces 32A charging with auto-stop cutoff at 4:00 PM |
| *"What's today's usage and cost?"* | Shows home consumption, solar generated, EV share, and current bill |
| *"Set battery start to 50% and stop at 30%"* | Dynamically updates Powerwall start/stop thresholds |
| *"Why did charging stop last time?"* | Queries recent session log history and stop reasons |
| *"Create a PR to fix XYZ"* | Dispatches autonomous background developer agent to open a GitHub PR |

---

## 📁 Repository Structure

```
smart_ev_charger/
├── Dockerfile              # Production container build
├── docker-compose.yml      # Compose service configuration
├── entrypoint.sh           # Auto-pulls latest Git updates on boot
├── requirements.txt        # Python dependencies
├── main.py                 # Core scheduler daemon & decision cycle
│
├── core/                   # Configuration & state machines
│   ├── config.py           # Environment schema & dynamic settings
│   ├── state.py            # Runtime state & guardrails
│   ├── decision.py         # Solar/battery evaluation rules
│   ├── manual_override.py  # Manual mode manager & auto-reset
│   └── tou.py              # Time-Of-Use rate schedule calculations
│
├── services/               # Hardware & Cloud IoT APIs
│   ├── chargepoint.py      # ChargePoint Flex API wrapper
│   ├── netzero.py          # Tesla Powerwall NetZero API client
│   └── sheets_db.py        # Google Sheets cloud database sync
│
├── agent/                  # AI Intelligence & Telegram Interface
│   ├── telegram_bot.py     # Telegram Bot & function calling tools
│   ├── llm_client.py       # Multi-model LLM client with retry & thinking
│   ├── dev_agent.py        # Autonomous GitHub PR developer loop
│   ├── daily_agent.py      # 7:00 AM morning energy briefing
│   └── alerts.py           # Custom dynamic metric alerts
│
├── reporting/              # Telemetry, Reports & Notifications
│   ├── csv_logger.py       # Telemetry logging & energy analytics engine
│   ├── report_generator.py # PNG monthly billing card infographic builder
│   ├── notifications.py    # Telegram & Pushover notifier
│   └── logger.py           # Rotating system logger
│
└── tests/                  # Automated unit test suite
```

---

## ⚙️ Configuration Reference (`.env`)

| Variable | Description | Default |
| :--- | :--- | :--- |
| `NETZERO_SITE_ID` / `NETZERO_API_TOKEN` | Tesla Powerwall NetZero API credentials | Required |
| `CHARGEPOINT_USERNAME` / `CHARGEPOINT_COULOMB_TOKEN` | ChargePoint account credentials | Required |
| `CHARGEPOINT_DEVICE_ID` | ChargePoint Home Flex charger device ID | Required |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_ID` | Telegram bot token and authorized chat ID | Required |
| `LLM_PROVIDER` / `LLM_MODEL` | Active AI provider (`gemini`, `nvidia`, `openai`, `anthropic`) | `gemini` / `gemini-2.5-flash` |
| `EV_MILES_PER_KWH` | Vehicle efficiency in miles per kWh | `3.53` |
| `UTILITY_PROVIDER` | Rate schedule plan (`MID`, `PGE`, or `CUSTOM`) | `MID` |
| `BATTERY_START_PCT` / `BATTERY_STOP_PCT` | Solar charging battery start/stop thresholds | `40` / `25` |
| `NIGHT_BLACKOUT_START_HOUR` / `END_HOUR` | Weekday peak rate blackout window | `16` / `9` |