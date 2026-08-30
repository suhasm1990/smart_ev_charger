"""User notifications (Pushover + Telegram), delivered off the caller's thread.

notify() enqueues and returns immediately so a slow notification service can
never stall the control cycle; one daemon worker preserves delivery order.
"""
import queue
import re
import threading
import time

import requests

from reporting.logger import log

_queue: queue.Queue = queue.Queue(maxsize=200)
_worker_lock = threading.Lock()
_worker_started = False


def _worker():
    while True:
        message = _queue.get()
        try:
            _deliver(message)
        except Exception as e:
            log.warning(f"Notification delivery failed: {e}")
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="Notifier").start()
            _worker_started = True


def notify(message: str):
    """Queues a notification for background delivery; never blocks the caller."""
    _ensure_worker()
    try:
        _queue.put_nowait(message)
    except queue.Full:
        log.warning("Notification queue is full — dropping a message")


def notify_flush(timeout: float = 5.0):
    """Best-effort wait for queued notifications to finish delivering."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


def _deliver(message: str):
    from core import config
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
                timeout=10,
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
                timeout=10,
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
                    timeout=10,
                )
                r_fallback.raise_for_status()
            except Exception as e2:
                log.warning(f"Telegram notify failed completely: {e2}")
