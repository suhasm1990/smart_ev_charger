"""Core domain logic, runtime state, and configuration."""
from core import config, state
from core.decision import evaluate
from core.manual_override import check_manual_mode
from core.state import get_session_minutes
from core.tou import (
    get_tou_period, get_tou_rate, is_expensive_period,
    is_in_night_blackout, is_weekend, provider_label,
)

__all__ = [
    "config", "state", "evaluate", "check_manual_mode", "get_session_minutes",
    "get_tou_period", "get_tou_rate", "is_expensive_period", "is_in_night_blackout",
    "is_weekend", "provider_label",
]
