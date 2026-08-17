"""Core domain logic, state management, and configuration."""
from core import config, state
from core.tou import get_tou_period, get_tou_rate, is_in_night_blackout, is_weekend
from core.decision import evaluate
from core.manual_override import check_manual_mode
