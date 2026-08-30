"""Manual override mode and its automatic morning reset."""
from datetime import datetime, timedelta

from core import config
from core.state import state
from reporting.logger import log_mode
from reporting.notifications import notify


def _auto_reset_due(now: datetime) -> bool:
    """True once the morning reset hour has passed since manual mode was set."""
    if not (state.manual_mode and state.manual_mode_set_at):
        return False
    reset_at = state.manual_mode_set_at.replace(
        hour=config.NIGHT_BLACKOUT_END_HOUR, minute=0, second=0, microsecond=0
    )
    if state.manual_mode_set_at.hour >= config.NIGHT_BLACKOUT_END_HOUR:
        reset_at += timedelta(days=1)
    return now >= reset_at


def check_manual_mode() -> bool:
    """Reconciles the configured override with runtime state; returns is-manual."""
    now = datetime.now(config.TZ)

    if _auto_reset_due(now):
        log_mode.info(
            f"MANUAL→AUTO | Auto-reset at {config.NIGHT_BLACKOUT_END_HOUR}:00 "
            f"(set at {state.manual_mode_set_at.strftime('%H:%M')})"
        )
        state.manual_mode = state.prev_manual_mode = False
        state.manual_mode_set_at = None
        state.clear_manual_guards()
        if config.MANUAL_MODE_OVERRIDE == "manual":
            config.update(MANUAL_MODE_OVERRIDE="default")
            notify(
                f"⚙️ <b>Auto Mode Restored</b>\n"
                f"Daily reset at {config.NIGHT_BLACKOUT_END_HOUR}:00 ended the manual override. "
                f"Solar automation and battery thresholds are active again."
            )

    # 'default' and 'auto' both mean automation is in control.
    is_manual = config.MANUAL_MODE_OVERRIDE == "manual"

    if is_manual and not state.prev_manual_mode:
        state.manual_mode = True
        state.manual_mode_set_at = now
        log_mode.warning(
            f"MANUAL MODE ACTIVATED | Automation paused until {config.NIGHT_BLACKOUT_END_HOUR}:00"
        )
        notify(f"⚙️ Manual mode ON — automation paused until {config.NIGHT_BLACKOUT_END_HOUR}:00")
    elif not is_manual and state.prev_manual_mode:
        state.manual_mode = False
        state.manual_mode_set_at = None
        state.clear_manual_guards()
        log_mode.info("AUTO MODE RESTORED | Automation resumed")
        notify("✅ Auto mode restored — automation resumed")

    state.prev_manual_mode = is_manual
    return state.manual_mode
