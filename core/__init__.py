"""Core domain logic, runtime state, and configuration."""
# `state` is the shared StateStore INSTANCE (not the module), and must be
# bound BEFORE core.decision (and the reporting modules it pulls in) so that
# their `from core import state` resolves to the instance rather than falling
# back to the core.state submodule.
from core import config
from core.state import get_session_minutes, state

# isort: split
from core.decision import evaluate
from core.manual_override import check_manual_mode
from core.tou import (
    get_tou_period,
    get_tou_rate,
    is_expensive_period,
    is_in_night_blackout,
    is_weekend,
    provider_label,
)

__all__ = [
    "config", "state", "evaluate", "check_manual_mode", "get_session_minutes",
    "get_tou_period", "get_tou_rate", "is_expensive_period", "is_in_night_blackout",
    "is_weekend", "provider_label",
]
