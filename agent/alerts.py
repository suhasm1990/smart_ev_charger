"""User-defined threshold alerts plus the built-in grid-export notification."""
import json
import os
import time
import uuid
from datetime import datetime, timedelta

from core import config, state
from reporting.logger import log, tail_lines
from reporting.notifications import notify

ALERTS_FILE = os.getenv("ALERTS_FILE", "logs/alerts.json")
MAX_ALERTS = 20
VALID_OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte")

_NUMERIC_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,  "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,  "lte": lambda a, b: a <= b,
}

_cache: list | None = None
_cache_mtime = -1.0


def load_alerts() -> list:
    """Reads the alert rules, re-parsing only when the file changes on disk."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(ALERTS_FILE)
    except OSError:
        _cache, _cache_mtime = [], -1.0
        return []
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    try:
        with open(ALERTS_FILE) as f:
            _cache = json.load(f)
        _cache_mtime = mtime
    except (OSError, ValueError) as e:
        log.error(f"Failed to load alerts: {e}")
        _cache = []
    return _cache


def save_alerts(alerts: list):
    global _cache, _cache_mtime
    try:
        os.makedirs(os.path.dirname(ALERTS_FILE) or ".", exist_ok=True)
        with open(ALERTS_FILE, "w") as f:
            json.dump(alerts, f, indent=4)
        _cache, _cache_mtime = alerts, os.path.getmtime(ALERTS_FILE)
    except OSError as e:
        log.error(f"Failed to save alerts: {e}")


def add_alert(field: str, operator: str, value, message: str, once: bool = True) -> str:
    if operator not in VALID_OPERATORS:
        return f"Error: Invalid operator '{operator}'. Must be one of {list(VALID_OPERATORS)}."

    alerts = list(load_alerts())
    # Replace rather than duplicate a rule on the same field and comparison.
    for alert in alerts:
        if alert["field"] == field and alert["operator"] == operator:
            alert.update(value=value, message=message, once=once, created_at=time.time())
            save_alerts(alerts)
            return f"Success: Updated existing alert for {field} {operator} {value}."

    if len(alerts) >= MAX_ALERTS:
        return f"Error: Maximum of {MAX_ALERTS} active alerts reached. Please clear some first."

    alert = {
        "id": str(uuid.uuid4())[:8], "field": field, "operator": operator,
        "value": value, "message": message, "once": once, "created_at": time.time(),
    }
    alerts.append(alert)
    save_alerts(alerts)
    return f"Success: Created alert '{message}' with ID {alert['id']}."


def remove_alert(alert_id: str) -> str:
    alerts = load_alerts()
    remaining = [a for a in alerts if a["id"] != alert_id]
    if len(remaining) == len(alerts):
        return f"Error: Alert with ID {alert_id} not found."
    save_alerts(remaining)
    return f"Success: Removed alert with ID {alert_id}."


def list_alerts() -> str:
    alerts = load_alerts()
    if not alerts:
        return "No active custom alerts."
    return "\n".join(
        f"• ID: <code>{a['id']}</code> | <code>{a['field']}</code> {a['operator']} "
        f"<code>{a['value']}</code> -> <i>'{a['message']}'</i> "
        f"({'once' if a['once'] else 'persistent'})"
        for a in alerts
    )


def _matches(value, operator: str, target) -> bool:
    """Compares a live reading against a rule, coercing target to value's type."""
    compare = _NUMERIC_OPS.get(operator)
    if compare is None:
        return False
    if isinstance(value, bool):
        if operator not in ("eq", "ne"):
            return False
        return compare(value, str(target).lower() in ("true", "1", "yes", "on"))
    if isinstance(value, (int, float)):
        return compare(value, float(target))
    if operator not in ("eq", "ne"):
        return False
    return compare(str(value).strip().lower(), str(target).strip().lower())


def check_alerts(current_state: dict):
    """Evaluates every rule against the current reading and notifies on matches."""
    triggered = []
    for alert in load_alerts():
        value = current_state.get(alert["field"])
        if value is None:
            continue
        try:
            matched = _matches(value, alert["operator"], alert["value"])
        except (TypeError, ValueError) as e:
            log.warning(f"Failed to evaluate alert {alert['id']} against '{value}': {e}")
            continue
        if matched:
            log.info(f"ALERT TRIGGERED | id={alert['id']} | "
                     f"{alert['field']} {alert['operator']} {alert['value']} | current={value}")
            notify(f"🔔 <b>Alert Triggered</b>\n{alert['message']}")
            if alert["once"]:
                triggered.append(alert["id"])

    if triggered:
        save_alerts([a for a in load_alerts() if a["id"] not in triggered])

    _check_grid_export(current_state)


def _check_grid_export(current_state: dict):
    """Notifies once per day when the site is exporting significant surplus."""
    export_kw = current_state.get("grid_export_kw")
    if export_kw is None:
        grid_kw = current_state.get("grid_kw")
        export_kw = abs(grid_kw) if grid_kw is not None and grid_kw < 0 else current_state.get("surplus_kw", 0)

    threshold = config.GRID_EXPORT_ALERT_THRESHOLD_KW
    if not (threshold > 0 and export_kw and export_kw >= threshold):
        return

    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    if state.last_grid_export_alert_date == today:
        return
    state.last_grid_export_alert_date = today
    notify(
        f"☀️ <b>Real-Time Grid Export Alert</b>\n"
        f"You are exporting <b>{export_kw:.1f} kW</b> to the grid.\n"
        f"EV Status: {'Plugged in' if current_state.get('is_plugged_in') else 'Unplugged'}.\n"
        f"A good moment to plug in the EV or run heavy appliances."
    )


def check_recent_log_errors(interval_minutes: int = 20) -> bool:
    """Whether any ERROR/CRITICAL was logged within the last `interval_minutes`."""
    threshold = datetime.now(config.TZ).replace(tzinfo=None) - timedelta(minutes=interval_minutes)
    for line in tail_lines(config.TEXT_LOG_FILE, 100):
        parts = line.split(" | ")
        if len(parts) < 3 or parts[1].strip() not in ("ERROR", "CRITICAL"):
            continue
        try:
            if datetime.strptime(parts[0].strip().split(",")[0], "%Y-%m-%d %H:%M:%S") >= threshold:
                return True
        except ValueError:
            continue
    return False
