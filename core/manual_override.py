"""Manual override mode and its automatic morning reset."""
from datetime import datetime, timedelta

from core import config
from core.state import state
from reporting.logger import log_mode
from reporting.notifications import notify

# Manual mode always lasts at least this long, so an override set just before
# the reset hour (e.g. "charge full" at 23:51) isn't cut off minutes later.
MIN_MANUAL_HOURS = 6


def manual_reset_at(set_at: datetime) -> datetime:
    """First reset hour at least MIN_MANUAL_HOURS after manual mode was set."""
    earliest = set_at + timedelta(hours=MIN_MANUAL_HOURS)
    reset_at = earliest.replace(hour=config.NIGHT_BLACKOUT_END_HOUR, minute=0, second=0, microsecond=0)
    return reset_at if reset_at >= earliest else reset_at + timedelta(days=1)


def _auto_reset_due(now: datetime) -> bool:
    """True once the reset time for the current manual override has passed."""
    if not (state.manual_mode and state.manual_mode_set_at):
        return False
    return now >= manual_reset_at(state.manual_mode_set_at)


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
        # A manual session can outlive the reset; auto mode adopts it without
        # sending a start, so restore the default limit here.
        try:
            from services.chargepoint import set_charger_amperage_limit  # avoids a core<->services import cycle
            set_charger_amperage_limit(config.DEFAULT_CHARGER_AMPERAGE)
        except Exception as e:
            log_mode.warning(f"Could not restore default amperage: {e}")
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
        until = manual_reset_at(now).strftime("%a %H:%M")
        log_mode.warning(f"MANUAL MODE ACTIVATED | Automation paused until {until}")
        notify(f"⚙️ Manual mode ON — automation paused until {until}")
    elif not is_manual and state.prev_manual_mode:
        state.manual_mode = False
        state.manual_mode_set_at = None
        state.clear_manual_guards()
        log_mode.info("AUTO MODE RESTORED | Automation resumed")
        notify("✅ Auto mode restored — automation resumed")

    state.prev_manual_mode = is_manual
    return state.manual_mode
