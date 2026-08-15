import requests
import re
import html
import config
from logger import log

def notify(message: str):
    # 1. Pushover Notifications (if configured)
    if config.PUSHOVER_USER_KEY and config.PUSHOVER_API_TOKEN:
        try:
            plain_msg = re.sub(r'<[^>]+>', '', message)
            requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": config.PUSHOVER_API_TOKEN,
                    "user": config.PUSHOVER_USER_KEY,
                    "message": plain_msg,
                    "title": "⚡ EV Charger"
                },
                timeout=15,
            )
        except Exception as e:
            log.warning(f"Pushover failed: {e}")

    # 2. Telegram Bot Notifications (if configured)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_ID:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_ALLOWED_USER_ID,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=15,
            )
            r.raise_for_status()
        except Exception as e:
            log.warning(f"Telegram HTML notify failed ({e}). Falling back to plain text...")
            try:
                plain_text = re.sub(r'<[^>]+>', '', message)
                r_fallback = requests.post(
                    url,
                    json={
                        "chat_id": config.TELEGRAM_ALLOWED_USER_ID,
                        "text": plain_text
                    },
                    timeout=15,
                )
                r_fallback.raise_for_status()
            except Exception as e2:
                log.warning(f"Telegram notify failed completely: {e2}")
