import requests
import html
import config
from logger import log

def notify(message: str):
    # 1. Pushover Notifications (if configured)
    if config.PUSHOVER_USER_KEY and config.PUSHOVER_API_TOKEN:
        try:
            requests.post(
                "https://api.pushover.net/1/messages.json",
                data={"token": config.PUSHOVER_API_TOKEN, "user": config.PUSHOVER_USER_KEY,
                      "message": message, "title": "⚡ EV Charger"},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Pushover failed: {e}")

    # 2. Telegram Bot Notifications (if configured)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_ID:
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            r = requests.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_ALLOWED_USER_ID,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            log.warning(f"Telegram notify failed: {e}")
