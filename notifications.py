import requests
import config
from logger import log

def notify(message: str):
    if not config.PUSHOVER_USER_KEY or not config.PUSHOVER_API_TOKEN:
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": config.PUSHOVER_API_TOKEN, "user": config.PUSHOVER_USER_KEY,
                  "message": message, "title": "⚡ EV Charger"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Pushover failed: {e}")
