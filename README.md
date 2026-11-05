# Smart EV Charger ⚡☀️

A Python daemon on a home NAS that charges the EV from solar and the Tesla Powerwall, avoids on-peak
rates, and is run day to day from Telegram.

## How it works

Every 15 minutes it reads the Powerwall (NetZero API) and the ChargePoint Home Flex, then:

- **Starts** charging inside today's **charge window** when the Powerwall is at or above the **start %**.
- **Stops** when the Powerwall drops below the **stop %** or the window ends. Solar readings don't start or stop it.
- **Never** charges below the 15% critical reserve, or in a manual night blackout.

At **07:00** (or on `/plan`) an AI planner sets the window and the start/stop %. It uses the last 7 days'
hourly solar, the Powerwall's current level, evening needs and today's rates. The window is kept between 06:00
and 17:00 (the start of on-peak).

**Miles goals** (*"add 100 miles by tomorrow"*): the window is used first; only the shortfall charges at the
cheapest night rate. The goal clears itself when met or expired.

## Using it (Telegram)

| Command / message | What it does |
| :--- | :--- |
| `/plan` | Run the planner now: window, start/stop %, appliance tip |
| `/update` | Pull the latest code from `main` and restart (~10 s) |
| `/logs 30 ERROR` | Recent events / errors |
| `/model` | Show or switch the AI model |
| `/bill` | Monthly bill infographic |
| *"Add 100 miles by tomorrow"* | Set a miles goal |
| *"Charge at 32A until 16:00"* | Manual boost with an auto-stop |
| *"How many miles did I add today?"* | Miles, kWh and cost |

Every start sends a 🟢 message: a **planned restart** or an **⚠️ unexpected stop** (crash, out-of-memory,
power loss), with downtime, code version and NAS memory.

Web dashboard: `http://<nas>:8080` (set `DASHBOARD_TOKEN` to protect it).

## Deploying changes

The image holds only `.env` and `service_account.json`. On every start, `entrypoint.sh` pulls `origin/main`
and runs `pip install`.

- **Code change:** push to `main`, then send `/update`. No rebuild is needed.
- **New `.env` value:** needs an image rebuild (`./build_docker.sh`, then redeploy). Avoid this: give new
  settings a default in `core/config.py`, or make them runtime-tunable (below).

## Settings

Most are set in `.env` (see `.env.example`). These can be changed live and are stored in the Google Sheets
**Settings** tab, which overrides `.env`:
`BATTERY_START_PCT` / `BATTERY_STOP_PCT`, `ALLOWED_CHARGE_START_HOUR` / `END_HOUR`, `NIGHT_BLACKOUT_*`,
`LLM_PROVIDER` / `LLM_MODEL`, `MANUAL_MODE_OVERRIDE`, `EV_MILES_PER_KWH`, `CHECK_INTERVAL_MINUTES`.

| Key `.env` values | Purpose |
| :--- | :--- |
| `NETZERO_*`, `CHARGEPOINT_*`, `TELEGRAM_*` | API credentials (required) |
| `LLM_PROVIDER`, `LLM_MODEL`, `NVIDIA_API_KEY`, `GEMINI_API_KEY` | AI model (NVIDIA Nemotron, with Gemini as fallback) |
| `UTILITY_PROVIDER` | Rate plan: `MID` (default), `PGE`, `CUSTOM` |
| `POWERWALL_USABLE_KWH`, `EV_MILES_PER_KWH` | 13.5 kWh, 3.53 mi/kWh |

Google Sheets also holds **Telemetry** (~62 days of 15-minute readings) and **System Logs** (last 500 events).

## Code map

| Path | What's there |
| :--- | :--- |
| `main.py` | Scheduler and the 15-minute control cycle |
| `core/decision.py` | Start/stop rules (pure function, easiest place to change behaviour) |
| `core/config.py`, `core/state.py` | Settings (env → Sheets) and runtime state |
| `core/tou.py`, `core/goal.py`, `core/lifecycle.py` | Rates, miles goals, startup notice |
| `agent/daily_agent.py` | Morning planner prompt and validation |
| `agent/telegram_bot.py`, `agent/llm_client.py` | Telegram bot, AI tools, model fallback |
| `services/` | NetZero, ChargePoint and Google Sheets clients |
| `reporting/` | Telemetry, energy/bill analytics, notifications |
| `dashboard/` | Web dashboard |

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
python -m unittest discover -s tests -t .
```

Tests run offline and never touch the live sheet. Each test file starts with `import tests` (kept first by
`# isort: split`), which points Sheets at a fake credentials file. CI runs the same lint and tests on every
push and PR.

## Security

- `.env` and `service_account.json` are git-ignored but baked into the image. Never push or share the image
  (or build with `--build-arg EMBED_SECRETS=0` and mount them instead).
- The bot only answers `TELEGRAM_ALLOWED_USER_ID`.
