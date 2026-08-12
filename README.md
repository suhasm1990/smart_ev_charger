# Smart EV Charger Automation ⚡☀️

An intelligent, fully automated Python daemon that optimally charges your Electric Vehicle based on real-time solar generation, Tesla Powerwall battery levels, ChargePoint hardware metrics, and Time-Of-Use (TOU) utility rates.

Designed to maximize free solar energy consumption, isolate EV charging costs from general household loads (like AC or washing machines), provide AI-driven bill reduction advice, and dynamically calculate electric bills matching your utility provider's precise rate structures (MID Rate N2-EVD, PG&E EV2-A, or Custom).

---

## 🌟 Key Features

- ☀️ **Solar & Powerwall Synchronization**: Integrates directly with the **NetZero API** to monitor Tesla Powerwall State of Charge (SoC %), solar generation (kW), home consumption (kW), and grid import/export (kW). Acts as a "daytime solar sponge," starting EV charging when house battery level exceeds thresholds (e.g. `> 40%`) and stopping when battery drops (e.g. `< 25%`).
- ⚡ **ChargePoint Hardware Integration**: Direct asynchronous interface with ChargePoint Home Flex chargers to monitor real-time charging status, charging power (`kW`), energy delivered (`kWh`), range added (`miles`), and dynamically adjust amperage limits (8A–32A).
- ⚙️ **Dynamic Utility Provider Rate Engine (MID, PG&E, or Custom)**: Easily switch utility rate schedules by setting `UTILITY_PROVIDER` (`MID`, `PGE`, or `CUSTOM`) in `.env`. Full support for fixed monthly service fees, volumetric surcharges, local tax multipliers, and NEM solar export credits.
- 🧠 **Daily 7:00 AM AI Planner & Morning Briefing**: Every morning at **7:00 AM**, the AI Planner analyzes the past 7 days of solar production and home load logs, incorporates user instructions (from Google Sheets or Telegram), and posts a Telegram morning update:
  - 📊 **Yesterday's Energy & Bill Summary**: Total estimated electric bill ($), EV charging share ($), home appliances share ($), self-powered percentage (%), solar kWh generated, and grid kWh imported.
  - ⚙️ **Today's Charging Strategy**: Charge windows (`ALLOWED_CHARGE_START_HOUR` – `ALLOWED_CHARGE_END_HOUR`) and Powerwall battery thresholds.
  - 💡 **AI Energy Suggestions & Reasoning**: Actionable appliance scheduling recommendations tailored to the upcoming day's solar generation forecast.
- 🤖 **Model-Agnostic AI Telegram Assistant**: Conversational natural-language interface powered by your choice of AI model (NVIDIA Nemotron, OpenAI, Claude, or Gemini) with 15+ tool integrations. Ask questions or issue commands like:
  - *"How can I reduce my electric bill?"*
  - *"When is the best time to run my washing machine or AC?"*
  - *"Why did charging stop last time?"*
  - *"How much did EV charging cost me this week?"*
  - *"What is my total electric bill for this month?"*
  - *"Charge with full power (32A)"* or *"Set default charging speed (20A)"*
  - *"Set battery start threshold to 50% and stop at 30%"*
- 💰 **EV vs. Home Cost Isolation & Tracking**: Intelligently isolates EV charger grid energy draw from heavy home appliances (AC, washing machine, fridge) to calculate exact grid kWh pulled, grid costs ($), solar energy used, and solar savings ($) for **today**, **yesterday**, **this week**, **this month**, or custom dates.
- 🕐 **TOU & Night Blackout Optimization**: Enforces a customizable weekday nighttime blackout window (default 4 PM to 9 AM) to prevent EV charging during expensive Peak rate hours or draining the Powerwall overnight.
- 🚨 **Custom Dynamic Alerts & Error Monitoring**: Create real-time alerts for battery, solar, grid export, or log error conditions via Telegram.
- 📊 **Google Sheets & Dual Logging System**: Every 15-minute automation cycle logs detailed telemetry to `logs/charger_log.csv` and syncs with Google Sheets. Supports two-way dynamic configuration via `logs/config_dynamic.json` and Google Sheets.
- 🛡️ **Emergency Safety Overrides**: Automatically halts EV charging if the Powerwall goes Off-Grid (`island_mode`) or if Tesla activates Storm Watch mode to preserve backup reserve power.

---

## ⚙️ Configuration Reference

All settings can be configured via environment variables in `.env`. Dynamic settings can also be modified at runtime via Telegram or Google Sheets.

| Variable | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `NETZERO_SITE_ID` | NetZero API Site ID for Tesla Powerwall telemetry | - | Yes |
| `NETZERO_API_TOKEN` | NetZero API Authentication Token | - | Yes |
| `CHARGEPOINT_USERNAME` | ChargePoint account username/email | - | Yes |
| `CHARGEPOINT_COULOMB_TOKEN` | ChargePoint Coulomb auth token | - | Yes |
| `CHARGEPOINT_DEVICE_ID` | ChargePoint Home Flex device ID | `0` | Yes |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token from @BotFather | - | Yes |
| `TELEGRAM_ALLOWED_USER_ID` | Authorized Telegram User ID (restricts access) | - | Yes |
| `LLM_PROVIDER` | AI Provider: `nvidia`, `openai`, `anthropic`, `gemini` | `gemini` (auto-detected) | No |
| `LLM_MODEL` | AI Model name override | Provider default | No |
| `NVIDIA_API_KEY` | NVIDIA AI Foundation / Nemotron API Key | - | No |
| `OPENAI_API_KEY` | OpenAI API Key (for GPT-4o / o3 models) | - | No |
| `ANTHROPIC_API_KEY` | Anthropic Claude API Key | - | No |
| `GEMINI_API_KEY` | Google Gemini API Key | - | Optional |
| `LLM_BASE_URL` | Optional custom base URL (for NIM or vLLM/Ollama) | - | No |
| `UTILITY_PROVIDER` | Utility rate provider: `MID`, `PGE`, or `CUSTOM` | `MID` | No |
| `UTILITY_FIXED_MONTHLY_FEE` | Fixed monthly service fee ($) | `32.00` (MID) | No |
| `UTILITY_VOLUMETRIC_ADDER` | Volumetric surcharge per kWh ($) | `0.0151` (MID) | No |
| `UTILITY_TAX_MULTIPLIER` | Local utility tax multiplier (e.g. 1.065 = 6.5% tax) | `1.065` (MID) | No |
| `UTILITY_SOLAR_EXPORT_CREDIT_RATE` | Solar export credit rate per kWh ($) | `0.076` (MID) | No |
| `BATTERY_START_PCT` | Minimum Powerwall battery % required to start charging | `40` | No |
| `BATTERY_STOP_PCT` | Powerwall battery % threshold to stop charging | `25` | No |
| `BATTERY_LOW_RESERVE_PCT` | Low battery reserve percentage limit | `15` | No |
| `NIGHT_BLACKOUT_START_HOUR` | Night blackout start hour (24h format, e.g. 16 for 4 PM) | `16` | No |
| `NIGHT_BLACKOUT_END_HOUR` | Night blackout end hour (24h format, e.g. 9 for 9 AM) | `9` | No |
| `ALLOWED_CHARGE_START_HOUR` | Daily allowed charging start hour (0-24) | `0` | No |
| `ALLOWED_CHARGE_END_HOUR` | Daily allowed charging end hour (0-24) | `24` | No |
| `DEFAULT_CHARGER_AMPERAGE` | Default charger current limit in Amps | `20` | No |
| `MAX_CHARGER_AMPERAGE` | Maximum charger current limit in Amps | `32` | No |
| `MIN_CHARGE_MINUTES` | Minimum duration (minutes) for a charging session | `15` | No |
| `CHECK_INTERVAL_MINUTES` | Interval (minutes) between automation cycles | `15` | No |
| `GRID_EXPORT_ALERT_THRESHOLD_KW` | Threshold (kW) to trigger grid export alerts | `1.0` | No |
| `DAILY_RESET_TIME` | Daily counter reset time in target timezone | `00:00` | No |
| `DAILY_AGENT_TIME` | Daily AI Agent briefing time in target timezone | `07:00` | No |
| `TZ` | Timezone string (e.g., `America/Los_Angeles`) | `America/Los_Angeles` | No |
| `GOOGLE_SHEET_URL` | Google Sheets URL for logging & dynamic settings sync | - | No |
| `PUSHOVER_USER_KEY` | Pushover User Key (optional push notifications) | - | No |
| `PUSHOVER_API_TOKEN` | Pushover API Token (optional push notifications) | - | No |

---

### 🤖 Changing AI / LLM Providers

The system is model-agnostic and supports **NVIDIA Nemotron**, **OpenAI**, **Anthropic Claude**, and **Google Gemini**. Simply set the appropriate API key and provider in `.env`:

#### NVIDIA Nemotron:
```env
LLM_PROVIDER=nvidia
LLM_MODEL=nvidia/llama-3.1-nemotron-70b-instruct
NVIDIA_API_KEY=nvapi-your-key-here
```

#### OpenAI (GPT-4o / o3-mini):
```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-your-key-here
```

#### Anthropic Claude:
```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-5-sonnet-20241022
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

#### Google Gemini:
```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIzaSy-your-key-here
```

---

## 🛠 Setup & Installation

### Requirements
- Python 3.10+
- ChargePoint Home Flex charger & account credentials
- Tesla Powerwall (accessible via NetZero API token)
- Telegram Bot Token & AI Provider API Key (NVIDIA, OpenAI, Claude, or Gemini)

---

### 1. Local Virtual Environment Setup
```bash
git clone https://github.com/suhasm1990/smart_ev_charger.git
cd smart_ev_charger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and enter your credentials:

```bash
cp .env.example .env
```

Edit `.env` to configure your API keys and preferred charging thresholds.

### 3. Google Sheets Integration (Optional)

To enable cloud telemetry logging and settings sync:
1. Create a Google Cloud Service Account and download the JSON key as `service_account.json` in the root directory.
2. Share your Google Sheet with the service account client email.
3. Add `GOOGLE_SHEET_URL` to your `.env` file.

---

## 🚀 Usage

### Running Locally
To run the automation daemon locally:
```bash
python main.py
```

### Running with Docker / Docker Compose
To run continuously in the background using Docker:
```bash
docker-compose up -d
```

View live logs:
```bash
docker-compose logs -f
```

---

## 🤖 Telegram AI Bot & Tool Function Calling

The daemon includes a model-agnostic AI Telegram bot interface with function calling tools:

### 🧠 7:00 AM Morning AI Briefing
Every morning at **7:00 AM**, the Daily AI Planner automatically sends a Telegram update containing:
- 📊 **Yesterday's Energy & Bill Summary** (Total bill $, EV charging share $, home appliances share $, self-powered %, solar kWh, grid kWh).
- ⚙️ **Today's Charging Strategy** (Selected charge window and battery thresholds).
- 💡 **AI Energy Suggestions & Reasoning** (Personalized advice on when to run heavy appliances based on solar production).

### 💡 Conversational Capabilities
- **Bill Reduction & Appliance Advice**: Ask *"How can I reduce my electric bill?"* or *"When should I run my washing machine, dryer, or AC?"*
- **Cost & Energy Tracking**: Ask *"How much did EV charging cost me today / this week / this month?"* or *"What is my total home electric bill for yesterday?"*
- **Session History Lookup**: Ask *"Why did charging stop last time?"* or *"Show me recent charging sessions."*
- **Amperage & Manual Overrides**: Ask *"Charge with full power (32A)"*, *"Force start charger"*, *"Force stop charger"*, or *"Switch to auto mode"*.
- **Threshold & Blackout Updates**: Ask *"Set battery start to 50% and stop to 30%"* or *"Set blackout hours from 4 PM to 9 AM"*.
- **Alert Management**: Ask *"Set alert if grid export > 2kW"* or *"List active custom alerts"*.
- **Log Inspection**: Ask *"Show me the last 30 log lines"*.
- **Agent Instructions**: Ask *"Add instruction: Charge car to max on Saturday morning"*.

---

## 📁 Repository Structure

```
smart_ev_charger/
├── main.py              # Main automation loop, signal handling & scheduled tasks (Daily Agent at 07:00 AM)
├── llm_client.py        # Model-agnostic LLM adapter (NVIDIA Nemotron, OpenAI, Anthropic, Gemini)
├── daily_agent.py       # Daily AI Planner & morning energy briefing generator
├── decision.py          # State-machine evaluator (safety, battery, solar & blackout rules)
├── telegram_bot.py      # Telegram Bot & AI tool function calling integration (15+ tools)
├── csv_logger.py        # CSV telemetry logging, session history, cost calculations & advice tools
├── api_chargepoint.py   # Async ChargePoint API client (status, start, stop, amperage control)
├── api_netzero.py       # NetZero API client for Tesla Powerwall telemetry (solar, battery, grid, home)
├── manual_override.py   # Manual override tracking & 9 AM daily auto-reset logic
├── tou.py               # Utility rate engines (MID N2-EVD, PG&E EV2-A, Custom), surcharges & TOU schedules
├── config.py            # Configuration loader, timezone converter & dynamic JSON state manager
├── alerts.py            # Dynamic custom notification system & grid export alerts
├── sheets_db.py         # Google Sheets synchronization (telemetry logging & dynamic settings sync)
├── notifications.py     # Telegram & Pushover notification client
├── state.py             # In-memory runtime state definitions
├── logger.py            # Centralized logging configuration
├── .env.example         # Example environment configuration template
├── requirements.txt     # Python package dependencies
├── Dockerfile           # Docker container build specification
└── docker-compose.yml   # Docker Compose orchestration definition
```
