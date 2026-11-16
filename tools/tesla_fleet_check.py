#!/usr/bin/env python3
"""Prove the Tesla Fleet API works for this Powerwall before any app changes.

Standalone: needs only `requests`. Run it on your own computer, in this order:

    export TESLA_CLIENT_ID=...        # from developer.tesla.com/dashboard
    export TESLA_CLIENT_SECRET=...
    export TESLA_DOMAIN=suhasm1990.github.io          # where the public key is hosted
    export TESLA_REDIRECT_URI=https://suhasm1990.github.io/callback

    python tools/tesla_fleet_check.py register   # one time: partner token + register domain
    python tools/tesla_fleet_check.py login      # one time: sign in, paste the code back
    python tools/tesla_fleet_check.py check      # any time: live + history calls

Tokens are saved to ~/.tesla_fleet/tokens.json (outside the repo). Raw API
responses go to ~/.tesla_fleet/responses/ so you can see exactly what came back.

Cost: `check` makes about 5 billable "data" requests (500 for $1), well inside
Tesla's $10/month per-account discount. Auth calls are not billed.
"""
import json
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
API_BASE = os.environ.get("TESLA_API_BASE", "https://fleet-api.prd.na.vn.cloud.tesla.com")
SCOPES = "openid offline_access energy_device_data"
TZ_NAME = os.environ.get("TZ_NAME", "America/Los_Angeles")

HOME = Path(os.environ.get("TESLA_FLEET_HOME", Path.home() / ".tesla_fleet"))
TOKENS_FILE = HOME / "tokens.json"
RESPONSES_DIR = HOME / "responses"


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Set {name} first (see the top of this file).")
    return value


def _show(label: str, response: requests.Response) -> dict:
    """Print the status, save the raw body, and return the JSON (or exit)."""
    print(f"  {label}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text[:2000]}
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    out = RESPONSES_DIR / f"{label.replace(' ', '_').replace('/', '_')}.json"
    out.write_text(json.dumps(body, indent=2))
    if not response.ok:
        print(f"  FAILED. Body: {json.dumps(body)[:800]}")
        sys.exit(1)
    return body


def _save_tokens(tokens: dict):
    HOME.mkdir(parents=True, exist_ok=True)
    tokens["saved_at"] = datetime.now().isoformat()
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    TOKENS_FILE.chmod(0o600)


# ── Step 1: register the app's domain (partner token) ────────────────────────
def cmd_register():
    client_id, secret = _need("TESLA_CLIENT_ID"), _need("TESLA_CLIENT_SECRET")
    domain = _need("TESLA_DOMAIN")

    key_url = f"https://{domain}/.well-known/appspecific/com.tesla.3p.public-key.pem"
    print(f"1. Public key reachable at {key_url} ?")
    r = requests.get(key_url, timeout=15)
    if not r.ok or "BEGIN PUBLIC KEY" not in r.text:
        sys.exit(f"  No. HTTP {r.status_code}. Tesla must be able to fetch this URL first.")
    print("  Yes.")

    print("2. Partner token (client_credentials)")
    body = _show("partner_token", requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": secret,
        "scope": SCOPES,
        "audience": API_BASE,
    }, timeout=20))
    partner = body["access_token"]
    headers = {"Authorization": f"Bearer {partner}"}

    print(f"3. Register domain {domain}")
    _show("partner_register", requests.post(
        f"{API_BASE}/api/1/partner_accounts", json={"domain": domain}, headers=headers, timeout=20))

    print("4. Confirm Tesla has the key on file")
    _show("partner_public_key", requests.get(
        f"{API_BASE}/api/1/partner_accounts/public_key", params={"domain": domain},
        headers=headers, timeout=20))
    print("Registered. Next: python tools/tesla_fleet_check.py login")


# ── Step 2: sign in as the Powerwall owner (authorization code) ──────────────
def cmd_login():
    client_id, secret = _need("TESLA_CLIENT_ID"), _need("TESLA_CLIENT_SECRET")
    redirect = _need("TESLA_REDIRECT_URI")
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
        "locale": "en-US",
        "prompt_missing_scopes": "true",
    })
    print("1. Open this URL, sign in with your Tesla account and approve:\n")
    print(f"   {url}\n")
    print("2. You land on your redirect URL (a 404 page there is fine).")
    pasted = input("   Paste the FULL URL from the address bar here: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if query.get("state", [""])[0] != state:
        sys.exit("State does not match. Start login again and paste the newest URL.")
    if "code" not in query:
        sys.exit(f"No code in that URL: {query.get('error', query)}")

    print("3. Exchange code for tokens")
    tokens = _show("user_token", requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": secret,
        "code": query["code"][0],
        "audience": API_BASE,
        "redirect_uri": redirect,
    }, timeout=20))
    if "refresh_token" not in tokens:
        sys.exit("No refresh_token returned; offline_access scope is missing.")
    _save_tokens(tokens)
    print(f"Saved tokens to {TOKENS_FILE}. Next: python tools/tesla_fleet_check.py check")


def _refresh(tokens: dict) -> dict:
    # Refresh tokens are single use: always save the new one immediately.
    new = _show("refresh_token", requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": _need("TESLA_CLIENT_ID"),
        "refresh_token": tokens["refresh_token"],
    }, timeout=20))
    _save_tokens(new)
    return new


# ── Step 3: the calls the charger would actually make ────────────────────────
def _kwh(wh) -> str:
    return f"{(wh or 0) / 1000:.2f} kWh"


def _summarise_history(label: str, body: dict):
    series = (body.get("response") or {}).get("time_series") or []
    print(f"  {label}: {len(series)} entries")
    if not series:
        return
    first, last = series[0], series[-1]
    print(f"    first timestamp {first.get('timestamp')}, last {last.get('timestamp')}")
    print(f"    fields: {', '.join(sorted(k for k in first if k != 'timestamp'))}")
    totals = {}
    for row in series:
        for key, value in row.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    for key in ("solar_energy_exported", "consumer_energy_imported_from_grid",
                "grid_energy_imported", "grid_energy_exported_from_solar",
                "grid_energy_exported_from_battery", "battery_energy_exported",
                "battery_energy_imported_from_solar", "consumer_energy_imported_from_solar",
                "consumer_energy_imported_from_battery"):
        if key in totals:
            print(f"    {key:40s} {_kwh(totals[key])}")


def cmd_check():
    if not TOKENS_FILE.exists():
        sys.exit("No tokens yet. Run: python tools/tesla_fleet_check.py login")
    tokens = _refresh(json.loads(TOKENS_FILE.read_text()))
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    def get(label, path, **params):
        return _show(label, requests.get(f"{API_BASE}{path}", headers=headers,
                                         params=params or None, timeout=30))

    print("1. Products on this account")
    products = get("products", "/api/1/products").get("response") or []
    sites = [p for p in products if "energy_site_id" in p]
    if not sites:
        sys.exit("  No energy site found on this Tesla account.")
    site_id = sites[0]["energy_site_id"]
    print(f"  energy_site_id = {site_id} ({sites[0].get('site_name', 'unnamed')})")

    print("2. Live status (what NetZero gives today)")
    live = get("live_status", f"/api/1/energy_sites/{site_id}/live_status").get("response") or {}
    for key in ("solar_power", "load_power", "grid_power", "battery_power",
                "percentage_charged", "grid_status", "island_status", "storm_mode_active", "timestamp"):
        print(f"    {key:20s} {live.get(key)}")

    tz = ZoneInfo(TZ_NAME)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    print("3. History: yesterday, interval detail (period=day)")
    _summarise_history("yesterday", get(
        "history_day", f"/api/1/energy_sites/{site_id}/calendar_history",
        kind="energy", period="day", time_zone=TZ_NAME,
        start_date=yesterday.isoformat(), end_date=(today - timedelta(seconds=1)).isoformat()))

    print("4. History: this month so far, daily totals (period=month)")
    _summarise_history("month", get(
        "history_month", f"/api/1/energy_sites/{site_id}/calendar_history",
        kind="energy", period="month", time_zone=TZ_NAME,
        start_date=month_start.isoformat(), end_date=datetime.now(tz).isoformat()))

    print(f"\nAll calls worked. Raw responses are in {RESPONSES_DIR}/")
    print("Please share the printed summary (it holds no secrets).")


if __name__ == "__main__":
    commands = {"register": cmd_register, "login": cmd_login, "check": cmd_check}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"Usage: python {sys.argv[0]} [{'|'.join(commands)}]")
    commands[sys.argv[1]]()
