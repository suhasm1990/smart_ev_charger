# Smart EV Charger Automation ⚡☀️

An intelligent Python daemon that automatically charges your EV using free solar power and Powerwall reserves, avoiding peak utility rates.

---

## Installation

**Local:**
```bash
git clone https://github.com/suhasm1990/smart_ev_charger.git
cd smart_ev_charger
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

**Docker:**
```bash
docker-compose up -d
```

---

## Basic Operation

The daemon runs as a scheduler, charging your EV when solar production exceeds household demand and Powerwall reserves are sufficient. Key behaviors:

- Charges when battery >40%, stops at <25%
- Respects custom amperage settings (8A–32A)
- Enforces safety blackout windows (4 PM – 9 AM weekdays)
- Isolates EV charger energy from home appliance usage

---

## Key Features

- **Solar & Powerwall Sync**: Auto-start charging when battery >40%, stop at <25%
- **ChargePoint Flex Control**: Adjust amperage (8A–32A), track kWh, kW, and range added
- **Guarded Manual/Boost Mode**: Force charge at custom amperage with smart guardrails
- **Monthly PNG Reports**: Infographic on the 1st of each month at 07:00 AM
- **7:00 AM AI Briefing**: Daily Telegram summary with bill breakdown and scheduling advice
- **Model-Agnostic AI Assistant**: Natural language control and status queries via Telegram
- **EV vs. Home Cost Isolation**: Separates EV charger energy from heavy appliances
- **Safety Night Blackouts**: Customizable weekday blackout window (4 PM – 9 AM) and emergency shutoffs

---

## Telegram Commands

| Command | Description |
|---|---|
| `/daily_agent` / *"Run daily agent"* | Trigger morning optimization briefing |
| `/monthly_report` / *"Generate report for July"* | Generate PNG bill report for any past month |
| *"Charge with full power now"* | Start 32A charging with guardrails |
| *"Charge at 32A until 16:00"* | Force 32A with auto-stop at 4 PM |
| *"What's today's usage?"* | Compute home consumption, solar, EV share, and current bill |
| *"Set battery start to 50% and stop at 30%"* | Update Powerwall thresholds dynamically |

---

## Repository Structure

```
smart_ev_charger/
├── Dockerfile          # Production Docker image
├── docker-compose.yml  # Compose services
├── requirements.txt    # Dependencies
├── main.py             # Main entry point (scheduler & daemon)
├── core/               # Config, state, decision models
├── services/           # API integrations (ChargePoint, NetZero, Sheets)
├── agent/              # AI intelligence & Telegram bot
├── reporting/          # Telemetry, reports, notifications
├── tests/              # Unit & integration tests
└── docs/               # Documentation assets
```

---

## Troubleshooting

- Ensure `.env` file is configured with API credentials
- Verify Telegram bot token and AI model key are set
- Check that charging guardrails respect safety blackout windows and Powerwall state
- Reports exclude fixed daily connection fees for accurate cost tracking