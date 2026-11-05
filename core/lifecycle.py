"""Start-up notice: why the app last stopped, how long it was down, and what is running now.

A planned exit (Telegram /update, or a stop signal) leaves a marker file next
to the heartbeat. Starting without one means the process died without warning:
a crash, an out-of-memory kill, or the NAS losing power.
"""
import json
import os
import subprocess
from datetime import datetime

from core import config
from reporting.logger import log

EXIT_MARKER_FILE = os.path.join(os.path.dirname(config.HEARTBEAT_FILE) or ".", "last_exit.json")


def record_exit(reason: str):
    """Notes a planned exit so the next start can report it."""
    try:
        with open(EXIT_MARKER_FILE, "w") as f:
            json.dump({"reason": reason, "at": datetime.now(config.TZ).isoformat()}, f)
    except OSError as e:
        log.warning(f"Could not write exit marker: {e}")


def _take_exit_marker() -> dict | None:
    try:
        with open(EXIT_MARKER_FILE) as f:
            marker = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(EXIT_MARKER_FILE)
    except OSError:
        pass
    return marker


def _last_heartbeat() -> datetime | None:
    """When the previous run last completed a cycle (read before this run touches it)."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(config.HEARTBEAT_FILE), config.TZ)
    except OSError:
        return None


def _code_version() -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h %s"], capture_output=True, text=True,
                             timeout=5, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return out.stdout.strip()[:80] or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _memory() -> str:
    """Host memory from /proc/meminfo (Linux only); empty elsewhere."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])  # kB
        total, available = info["MemTotal"] / 1024, info["MemAvailable"] / 1024
        return f"{available:.0f} MB free of {total:.0f} MB"
    except (OSError, KeyError, ValueError):
        return ""


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def startup_message(now: datetime | None = None) -> str:
    """The Telegram notice for this start. Call once, before the first heartbeat."""
    now = now or datetime.now(config.TZ)
    marker = _take_exit_marker()
    last_beat = _last_heartbeat()

    if marker:
        cause = f"after a planned restart ({marker.get('reason', 'unknown')})"
        stopped_at = datetime.fromisoformat(marker["at"]) if marker.get("at") else last_beat
    elif last_beat:
        cause = "⚠️ after an UNEXPECTED stop (crash, out-of-memory, or power loss)"
        stopped_at = last_beat
    else:
        cause = "for the first time (no previous run found)"
        stopped_at = None

    lines = [f"🟢 <b>Smart EV Charger started</b> {cause}",
             f"• Started: {now:%a %b %d %H:%M}"]
    if stopped_at:
        label = "Stopped" if marker else "Last healthy cycle"
        lines.append(f"• {label}: {stopped_at:%a %b %d %H:%M} (down {_duration((now - stopped_at).total_seconds())})")
    lines.append(f"• Code: <code>{_code_version()}</code>")
    lines.append(f"• Mode: {config.MANUAL_MODE_OVERRIDE.upper()} | Model: {config.LLM_MODEL}")
    memory = _memory()
    if memory:
        lines.append(f"• NAS memory: {memory}")
    return "\n".join(lines)
