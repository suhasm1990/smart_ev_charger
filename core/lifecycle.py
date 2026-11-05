"""Start-up notice: why the app last stopped, how long it was down, and what is running now.

A planned exit (Telegram /update, or a stop signal) leaves a marker file next
to the heartbeat. Starting without one means the process died without warning:
a crash, an out-of-memory kill, or the NAS losing power.
"""
import html
import json
import os
import re
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
    """Short hash and commit subject, e.g. "a0d4f0c · Telegram notice on every start"."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h %s"], capture_output=True, text=True,
                             timeout=5, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha, _, subject = out.stdout.strip().partition(" ")
    if not sha:
        return "unknown"
    subject = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", subject)  # drop "feat:" style prefixes
    subject = subject.split(";")[0].strip()
    if len(subject) > 45:
        subject = subject[:45].rsplit(" ", 1)[0].rstrip(",") + "…"
    return f"{sha} · {subject}" if subject else sha


def _memory() -> str:
    """Host memory from /proc/meminfo (Linux only); empty elsewhere."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])  # kB
        total, available = info["MemTotal"] / 1024 ** 2, info["MemAvailable"] / 1024 ** 2
        return f"{available:.1f} of {total:.1f} GB free"
    except (OSError, KeyError, ValueError):
        return ""


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _when(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %b %d %H:%M}"


def startup_message(now: datetime | None = None) -> str:
    """The Telegram notice for this start. Call once, before the first heartbeat."""
    now = now or datetime.now(config.TZ)
    marker = _take_exit_marker()
    last_beat = _last_heartbeat()

    if marker:
        title = "🟢 <b>Charger back online</b> · planned restart"
        detail = f"Reason: <code>{html.escape(str(marker.get('reason', 'unknown')))}</code>"
        stopped_at = datetime.fromisoformat(marker["at"]) if marker.get("at") else last_beat
    elif last_beat:
        title = "⚠️ <b>Charger back online</b> · UNEXPECTED stop"
        detail = "<i>Likely a crash, out-of-memory kill, or power loss.</i>"
        stopped_at = last_beat
    else:
        title = "🟢 <b>Charger online</b> · first start"
        detail = "<i>No previous run found.</i>"
        stopped_at = None

    timing = f"🕐 Up {_when(now, now)}"
    if stopped_at:
        label = "stopped" if marker else "last cycle"
        timing += f" · {label} {_when(stopped_at, now)} · down {_duration((now - stopped_at).total_seconds())}"

    model = config.LLM_MODEL.split("/")[-1]
    lines = [title, detail, "", timing,
             f"📦 <code>{html.escape(_code_version())}</code>",
             f"⚙️ {config.MANUAL_MODE_OVERRIDE.upper()} · {html.escape(model)}"]
    memory = _memory()
    if memory:
        lines.append(f"💾 NAS {memory}")
    return "\n".join(lines)
