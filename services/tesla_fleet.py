"""Powerwall live status and metered energy history from the Tesla Fleet API.

Auth: Tesla refresh tokens are single use. Every refresh returns a new one,
which is written to TESLA_TOKEN_FILE before anything else happens, so a crash
right after a refresh never loses the token chain. TESLA_REFRESH_TOKEN in .env
is only the seed: it is adopted on first start, and again whenever it changes
(a new sign-in), and ignored otherwise.
"""
import hashlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

import requests

from core import config
from services.netzero import build_stats

log = logging.getLogger("TESLA")

TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
_REFRESH_MARGIN_S = 300        # refresh access tokens 5 minutes before they expire
_DEFAULT_TOKEN_TTL_S = 8 * 3600
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 2.0)

_session = requests.Session()
_lock = threading.RLock()


class TeslaAuthError(RuntimeError):
    """The refresh token was rejected: someone has to sign in again."""


class TeslaBudgetError(RuntimeError):
    """This month's call budget is used up; callers fall back or skip."""


def is_configured() -> bool:
    return bool(config.TESLA_CLIENT_ID and config.TESLA_REFRESH_TOKEN)


# ── Token store ─────────────────────────────────────────────────────────────

def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _load_store() -> dict:
    try:
        with open(config.TESLA_TOKEN_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_store(store: dict):
    """Atomic write, so a crash mid-write cannot corrupt the only valid token."""
    path = config.TESLA_TOKEN_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _current_store() -> dict:
    """The token store, re-seeded from .env when the seed there is new."""
    store = _load_store()
    seed = config.TESLA_REFRESH_TOKEN
    seed_fp = _fingerprint(seed) if seed else ""
    if seed and store.get("seed_fingerprint") != seed_fp:
        log.info("Adopting TESLA_REFRESH_TOKEN from .env as the start of a new token chain")
        store = {"seed_fingerprint": seed_fp, "refresh_token": seed,
                 "energy_site_id": store.get("energy_site_id", "")}
        _save_store(store)
    return store


def _refresh(store: dict) -> dict:
    response = _session.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": config.TESLA_CLIENT_ID,
        "refresh_token": store["refresh_token"],
    }, timeout=20)
    if response.status_code in (400, 401, 403):
        raise TeslaAuthError(
            f"Tesla rejected the refresh token (HTTP {response.status_code}). Sign in again with "
            f"tools/tesla_fleet_check.py and put the new TESLA_REFRESH_TOKEN in .env.")
    response.raise_for_status()
    body = response.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise TeslaAuthError("Tesla token response had no access/refresh token")

    store = {
        **store,
        "refresh_token": body["refresh_token"],
        "access_token": body["access_token"],
        "expires_at": time.time() + float(body.get("expires_in") or _DEFAULT_TOKEN_TTL_S),
        "refreshed_at": datetime.now(config.TZ).isoformat(),
    }
    _save_store(store)
    log.debug("Refreshed Tesla access token")
    return store


def _access_token(force_refresh: bool = False) -> str:
    with _lock:
        store = _current_store()
        if not store.get("refresh_token"):
            raise TeslaAuthError("No Tesla refresh token configured")
        fresh = store.get("access_token") and store.get("expires_at", 0) - time.time() > _REFRESH_MARGIN_S
        if force_refresh or not fresh:
            store = _refresh(store)
        return store["access_token"]


# ── Monthly call budget ─────────────────────────────────────────────────────

def _month_key() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m")


def calls_this_month() -> int:
    with _lock:
        usage = _load_store().get("usage") or {}
    return int(usage.get("calls", 0)) if usage.get("month") == _month_key() else 0


def _check_budget():
    if calls_this_month() >= config.TESLA_MONTHLY_CALL_BUDGET:
        raise TeslaBudgetError(
            f"Tesla call budget reached ({config.TESLA_MONTHLY_CALL_BUDGET} calls this month)")


def _count_call():
    """Tesla bills every response below HTTP 500, errors included."""
    with _lock:
        store = _load_store()
        usage = store.get("usage") or {}
        month = _month_key()
        calls = int(usage.get("calls", 0)) + 1 if usage.get("month") == month else 1
        store["usage"] = {"month": month, "calls": calls}
        _save_store(store)


# ── API calls ───────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    """GET with one token refresh on 401 and retries on network errors / 5xx.

    Tesla bills every response below HTTP 500, so 4xx are never retried.
    """
    url = f"{config.TESLA_API_BASE}{path}"
    refreshed = False
    attempt = 0
    while True:
        _check_budget()
        try:
            response = _session.get(url, params=params, timeout=20,
                                    headers={"Authorization": f"Bearer {_access_token(refreshed)}"})
            if response.status_code < 500:
                _count_call()
            if response.status_code == 401 and not refreshed:
                refreshed = True
                continue
            response.raise_for_status()
            return response.json().get("response") or {}
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            attempt += 1
            if (status is not None and status < 500) or attempt >= _MAX_ATTEMPTS:
                raise
            log.warning(f"Tesla request failed (attempt {attempt}/{_MAX_ATTEMPTS}): {e}")
            time.sleep(_BACKOFF_SECONDS[attempt - 1])


def energy_site_id() -> str:
    if config.TESLA_ENERGY_SITE_ID:
        return str(config.TESLA_ENERGY_SITE_ID)
    with _lock:
        cached = _load_store().get("energy_site_id")
    if cached:
        return str(cached)
    products = _get("/api/1/products")
    sites = [p for p in (products if isinstance(products, list) else []) if p.get("energy_site_id")]
    if not sites:
        raise ValueError("No Tesla energy site found on this account")
    site_id = str(sites[0]["energy_site_id"])
    with _lock:
        _save_store({**_current_store(), "energy_site_id": site_id})
    return site_id


def get_powerwall_stats(now: datetime = None) -> dict:
    """Live solar, home, grid and battery figures, in the same shape as NetZero's."""
    live = _get(f"/api/1/energy_sites/{energy_site_id()}/live_status")
    if not isinstance(live, dict) or not live:
        raise ValueError("Tesla live_status response was empty")
    return build_stats(live, now, source="Tesla")


def get_energy_intervals(first: date, last: date = None) -> list[dict]:
    """Metered energy for local days first..last, as Tesla's time_series (Wh per interval).

    One day comes back in 5-minute steps (period=day); a longer range is asked
    for as period=month, which Tesla answers in 30-minute steps. Either costs
    one call.
    """
    last = last or first
    tz = config.TZ
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tz) - timedelta(seconds=1)
    body = _get(f"/api/1/energy_sites/{energy_site_id()}/calendar_history", params={
        "kind": "energy",
        "period": "day" if first == last else "month",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "time_zone": str(tz),
    })
    series = body.get("time_series") if isinstance(body, dict) else None
    return series if isinstance(series, list) else []
