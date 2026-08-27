"""Autonomous AI agents, chat integration, and alerting."""
from agent.alerts import (
    add_alert, check_alerts, check_recent_log_errors, list_alerts, remove_alert,
)
from agent.daily_agent import run_daily_agent
from agent.llm_client import (
    chat_with_tools, function_to_openai_tool, generate_json, resolve_llm_config, trim_history,
)
from agent.telegram_bot import handle_message_with_llm, start_telegram_bot

__all__ = [
    "add_alert", "remove_alert", "list_alerts", "check_alerts", "check_recent_log_errors",
    "run_daily_agent", "generate_json", "chat_with_tools", "resolve_llm_config",
    "function_to_openai_tool", "trim_history", "start_telegram_bot", "handle_message_with_llm",
]
