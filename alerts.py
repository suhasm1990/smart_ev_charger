import os
import json
import uuid
import time
import datetime
from logger import log
from notifications import notify

ALERTS_FILE = "logs/alerts.json"
MAX_ALERTS = 20

def load_alerts() -> list:
    if not os.path.exists(ALERTS_FILE):
        return []
    try:
        with open(ALERTS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load alerts: {e}")
        return []

def save_alerts(alerts: list):
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    try:
        with open(ALERTS_FILE, "w") as f:
            json.dump(alerts, f, indent=4)
    except Exception as e:
        log.error(f"Failed to save alerts: {e}")

def add_alert(field: str, operator: str, value, message: str, once: bool = True) -> str:
    alerts = load_alerts()
    if len(alerts) >= MAX_ALERTS:
        return f"Error: Maximum limit of {MAX_ALERTS} active alerts reached. Please clear some alerts first."
        
    # Standardize operators
    valid_operators = ["eq", "ne", "gt", "gte", "lt", "lte"]
    if operator not in valid_operators:
        return f"Error: Invalid operator '{operator}'. Must be one of {valid_operators}."
        
    # De-duplicate: if alert for same field and operator exists, update it to save space and prevent duplicate rules
    for alert in alerts:
        if alert["field"] == field and alert["operator"] == operator:
            alert["value"] = value
            alert["message"] = message
            alert["once"] = once
            alert["created_at"] = time.time()
            save_alerts(alerts)
            return f"Success: Updated existing alert for {field} {operator} {value}."
            
    # Add new alert
    new_alert = {
        "id": str(uuid.uuid4())[:8],
        "field": field,
        "operator": operator,
        "value": value,
        "message": message,
        "once": once,
        "created_at": time.time()
    }
    alerts.append(new_alert)
    save_alerts(alerts)
    return f"Success: Created alert '{message}' with ID {new_alert['id']}."

def remove_alert(alert_id: str) -> str:
    alerts = load_alerts()
    filtered = [a for a in alerts if a["id"] != alert_id]
    if len(filtered) == len(alerts):
        return f"Error: Alert with ID {alert_id} not found."
    save_alerts(filtered)
    return f"Success: Removed alert with ID {alert_id}."

def list_alerts() -> str:
    alerts = load_alerts()
    if not alerts:
        return "No active custom alerts."
    lines = []
    for a in alerts:
        once_str = "once" if a["once"] else "persistent"
        lines.append(f"• ID: <code>{a['id']}</code> | <code>{a['field']}</code> {a['operator']} <code>{a['value']}</code> -> <i>'{a['message']}'</i> ({once_str})")
    return "\n".join(lines)

def check_alerts(current_state: dict):
    alerts = load_alerts()
    if alerts:
        triggered_ids = []
        for alert in alerts:
            field = alert["field"]
            operator = alert["operator"]
            target = alert["value"]
            
            if field not in current_state:
                continue
                
            val = current_state[field]
            if val is None:
                continue
                
            matched = False
            
            try:
                # Handle boolean comparison
                if isinstance(val, bool):
                    target_bool = str(target).lower() in ["true", "1", "yes", "on"]
                    if operator == "eq": matched = (val == target_bool)
                    elif operator == "ne": matched = (val != target_bool)
                # Handle numeric comparison
                elif isinstance(val, (int, float)):
                    target_num = float(target)
                    if operator == "eq": matched = (val == target_num)
                    elif operator == "ne": matched = (val != target_num)
                    elif operator == "gt": matched = (val > target_num)
                    elif operator == "gte": matched = (val >= target_num)
                    elif operator == "lt": matched = (val < target_num)
                    elif operator == "lte": matched = (val <= target_num)
                # Handle string comparison
                else:
                    target_str = str(target).strip()
                    val_str = str(val).strip()
                    if operator == "eq": matched = (val_str.lower() == target_str.lower())
                    elif operator == "ne": matched = (val_str.lower() != target_str.lower())
            except Exception as e:
                log.warning(f"Failed to evaluate alert rule {alert['id']} for state value '{val}': {e}")
                continue
                
            if matched:
                log.info(f"ALERT TRIGGERED | id={alert['id']} | field={field} {operator} {target} | current={val}")
                notify(f"🔔 <b>Alert Triggered</b>\n{alert['message']}")
                if alert["once"]:
                    triggered_ids.append(alert["id"])
                    
        if triggered_ids:
            remaining = [a for a in alerts if a["id"] not in triggered_ids]
            save_alerts(remaining)

    # Built-in Real-Time Grid Export Alert (Configurable threshold)
    import config
    import state

    grid_export_kw = current_state.get("grid_export_kw")
    if grid_export_kw is None:
        grid_kw = current_state.get("grid_kw")
        if grid_kw is not None and grid_kw < 0:
            grid_export_kw = abs(grid_kw)
        else:
            grid_export_kw = current_state.get("surplus_kw", 0)

    threshold = getattr(config, "GRID_EXPORT_ALERT_THRESHOLD_KW", 1.0)
    if threshold > 0 and grid_export_kw and grid_export_kw >= threshold:
        today_str = datetime.datetime.now(config.TZ).strftime("%Y-%m-%d")
        if getattr(state, "last_grid_export_alert_date", None) != today_str:
            is_plugged = current_state.get("is_plugged_in")
            status_str = "Plugged in" if is_plugged else "Unplugged"
            notify(
                f"☀️ <b>Real-Time Grid Export Alert</b>\n"
                f"You are currently exporting <b>{grid_export_kw:.1f} kW</b> to the grid!\n"
                f"EV Status: {status_str}.\n"
                f"This is a great time to plug in the EV or run heavy appliances (AC, washing machine)."
            )
            state.last_grid_export_alert_date = today_str
            state.last_surplus_alert_date = today_str


def check_recent_log_errors(interval_minutes: int = 20) -> bool:
    log_file = "logs/charger.log"
    if not os.path.exists(log_file):
        return False
        
    now = datetime.datetime.now()
    threshold = now - datetime.timedelta(minutes=interval_minutes)
    
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
            last_lines = lines[-100:]
            for line in last_lines:
                parts = line.split(" | ")
                if len(parts) >= 3:
                    ts_str = parts[0].strip()
                    level = parts[1].strip()
                    
                    if level in ["ERROR", "CRITICAL"]:
                        try:
                            ts_clean = ts_str.split(",")[0]
                            dt = datetime.datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
                            if dt >= threshold:
                                return True
                        except Exception:
                            pass
    except Exception as e:
        log.warning(f"Error reading log file for errors: {e}")
        
    return False
