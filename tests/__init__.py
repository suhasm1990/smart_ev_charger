"""Test suite.

Point the app at throwaway files and disable the Google Sheets integration so
running the tests never mutates the live spreadsheet or the real log files.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_tmp = tempfile.mkdtemp(prefix="ev_charger_tests_")
os.environ.setdefault("GOOGLE_CREDENTIALS_FILE", os.path.join(_tmp, "no-service-account.json"))
os.environ.setdefault("CSV_LOG_FILE", os.path.join(_tmp, "charger_log.csv"))
os.environ.setdefault("TEXT_LOG_FILE", os.path.join(_tmp, "charger.log"))
os.environ.setdefault("DYNAMIC_CONFIG_FILE", os.path.join(_tmp, "config_dynamic.json"))
os.environ.setdefault("ALERTS_FILE", os.path.join(_tmp, "alerts.json"))
os.environ.setdefault("HEARTBEAT_FILE", os.path.join(_tmp, "heartbeat"))
os.environ.setdefault("TESLA_TOKEN_FILE", os.path.join(_tmp, "tesla_fleet.json"))
os.environ.setdefault("ENERGY_HISTORY_FILE", os.path.join(_tmp, "energy_history.csv"))
# A developer's real .env must never make tests call Tesla.
for _key in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REFRESH_TOKEN", "TESLA_ENERGY_SITE_ID"):
    os.environ[_key] = ""
