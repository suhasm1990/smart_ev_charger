"""Powerwall live status and metered energy history from the Tesla Fleet API.

Tesla refresh tokens are single use: every refresh returns a new one, saved to
TESLA_TOKEN_FILE and backed up to the Google Sheet settings tab, because a new
image starts with an empty logs/ folder. TESLA_REFRESH_TOKEN in .env is only
the seed, adopted on first start and again whenever it changes (a new sign-in).

Requests are not retried: Tesla bills every response below HTTP 500, and the
caller falls back to NetZero for that cycle anyway.
"""
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from core import config
from services import sheets_db
from services.netzero import build_stats

TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
SHEETS_KEY = "TESLA_TOKEN_STATE"
_REFRESH_MARGIN_S = 300  # refresh the access token 5 minutes before it expires

_session = requests.Session()
_lock = threading.Lock()


class TeslaAuthError(RuntimeError):
    """The refresh token was rejected: someone has to sign in again."""


class TeslaBudgetError(RuntimeError):
    """This month's call budget is used up."""


def is_configured() -> bool:
    return bool(config.TESLA_CLIENT_ID and config.TESLA_REFRESH_TOKEN and config.TESLA_ENERGY_SITE_ID)


# ── Token store (also holds this month's call count) ────────────────────────

def _load() -> dict:
    """The local store; on a fresh container, the copy backed up in Sheets."""
    try:
        with open(config.TESLA_TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    if sheets_db.is_disabled():
        return {}
    try:
        backup = json.loads(sheets_db.get_settings(force_refresh=True).get(SHEETS_KEY) or "{}")
    except Exception:
        return {}
    if backup:
        _save(backup)
    return backup


def _save(store: dict):
    """Atomic write, so a crash mid-write cannot lose the only valid token."""
    path = config.TESLA_TOKEN_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(f"{path}.tmp", "w") as f:
        json.dump(store, f)
    os.replace(f"{path}.tmp", path)


def _store() -> dict:
    """The saved tokens, restarted from the .env seed when that seed is new."""
    store = _load()
    seed = hashlib.sha256(config.TESLA_REFRESH_TOKEN.encode()).hexdigest()[:16]
    if store.get("seed") != seed:
        store = {"seed": seed, "refresh_token": config.TESLA_REFRESH_TOKEN, "usage": store.get("usage")}
        _save(store)
    return store


def _access_token(store: dict, force: bool) -> str:
    if not force and store.get("expires_at", 0) - time.time() > _REFRESH_MARGIN_S:
        return store["access_token"]
    response = _session.post(TOKEN_URL, timeout=20, data={
        "grant_type": "refresh_token",
        "client_id": config.TESLA_CLIENT_ID,
        "refresh_token": store["refresh_token"],
    })
    if response.status_code in (400, 401, 403):
        raise TeslaAuthError(
            f"Tesla rejected the refresh token (HTTP {response.status_code}). Sign in again with "
            f"tools/tesla_fleet_check.py and put the new TESLA_REFRESH_TOKEN in .env.")
    response.raise_for_status()
    body = response.json()
    store.update(refresh_token=body["refresh_token"], access_token=body["access_token"],
                 expires_at=time.time() + float(body["expires_in"]))
    _save(store)
    if not sheets_db.is_disabled():
        sheets_db.update_settings({SHEETS_KEY: json.dumps(store)}, blocking=True)
    return store["access_token"]


def _month() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m")


def calls_this_month() -> int:
    usage = _load().get("usage") or {}
    return usage.get("calls", 0) if usage.get("month") == _month() else 0


# ── API ─────────────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    """One billed GET on the energy site. A 401 (token revoked early) gets one refresh and retry."""
    url = f"{config.TESLA_API_BASE}/api/1/energy_sites/{config.TESLA_ENERGY_SITE_ID}{path}"
    with _lock:
        for force_refresh in (False, True):
            store = _store()
            calls = calls_this_month()
            if calls >= config.TESLA_MONTHLY_CALL_BUDGET:
                raise TeslaBudgetError(f"Tesla call budget reached ({calls} calls this month)")
            token = _access_token(store, force_refresh)
            response = _session.get(url, params=params, timeout=20, headers={"Authorization": f"Bearer {token}"})
            if response.status_code < 500:
                store["usage"] = {"month": _month(), "calls": calls + 1}
                _save(store)
            if response.status_code != 401:
                break
    response.raise_for_status()
    return response.json().get("response") or {}


def get_powerwall_stats(now: datetime = None) -> dict:
    """Live solar, home, grid and battery figures, in the same shape as NetZero's."""
    return build_stats(_get("/live_status"), now, source="Tesla")


def get_energy_intervals(first: date, last: date) -> list[dict]:
    """Metered energy for local days first..last as Tesla's time_series (Wh per interval).

    One call either way: a single day comes in 5-minute steps (period=day),
    a longer range in 30-minute steps (period=month).
    """
    start = datetime(first.year, first.month, first.day, tzinfo=config.TZ)
    end = datetime(last.year, last.month, last.day, tzinfo=config.TZ) + timedelta(days=1, seconds=-1)
    body = _get("/calendar_history", params={
        "kind": "energy",
        "period": "day" if first == last else "month",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "time_zone": str(config.TZ),
    })
    return body.get("time_series") or []
