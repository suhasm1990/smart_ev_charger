import requests
from datetime import datetime, timedelta
import config
import state
from logger import log_mode
from notifications import notify

def check_manual_mode() -> bool:
    now = datetime.now(config.TZ)

    # Auto-reset manual mode at NIGHT_BLACKOUT_END_HOUR each morning
    if state.manual_mode and state.manual_mode_set_at:
        reset_time = state.manual_mode_set_at.replace(
            hour=config.NIGHT_BLACKOUT_END_HOUR, minute=0, second=0, microsecond=0
        )
        if state.manual_mode_set_at.hour >= config.NIGHT_BLACKOUT_END_HOUR:
            reset_time += timedelta(days=1)
        if now >= reset_time:
            log_mode.info(
                f"MANUAL→AUTO | Auto-reset at {config.NIGHT_BLACKOUT_END_HOUR}:00 "
                f"(was set at {state.manual_mode_set_at.strftime('%H:%M')})"
            )
            state.manual_mode        = False
            state.manual_mode_set_at = None
            if config.MANUAL_MODE_OVERRIDE == "manual":
                config.MANUAL_MODE_OVERRIDE = "default"
                config.save_dynamic_config()

    # Load dynamic configs first to ensure we check local override
    config.load_dynamic_config()

    if config.MANUAL_MODE_OVERRIDE == "manual":
        new_manual = True
        log_mode.debug("Local override active: manual mode forced")
    elif config.MANUAL_MODE_OVERRIDE == "auto":
        new_manual = False
        log_mode.debug("Local override active: auto mode forced")
    else:
        # Default is auto
        new_manual = False

    # Log transitions only
    if new_manual and not state.prev_manual_mode:
        state.manual_mode        = True
        state.manual_mode_set_at = now
        log_mode.warning(
            f"MANUAL MODE ACTIVATED | Automation paused | "
            f"Will auto-resume at {config.NIGHT_BLACKOUT_END_HOUR}:00"
        )
        notify(f"⚙️ Manual mode ON — automation paused until {config.NIGHT_BLACKOUT_END_HOUR}:00 AM")

    elif not new_manual and state.prev_manual_mode:
        state.manual_mode        = False
        state.manual_mode_set_at = None
        log_mode.info("AUTO MODE RESTORED | Automation resumed")
        notify("✅ Auto mode restored — automation resumed")

    state.prev_manual_mode = new_manual
    return state.manual_mode
