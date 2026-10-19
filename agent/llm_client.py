"""Model-agnostic LLM access (Gemini, NVIDIA, OpenAI, Anthropic) via litellm."""
import html
import inspect
import json
import re
import threading
import time
import typing

from core import config
from reporting.logger import log

try:
    import litellm
    litellm.suppress_debug_info = True
    litellm.drop_params = True
except Exception:
    litellm = None

# provider -> (config attribute holding the key, default base URL, litellm prefix)
PROVIDER_DEFAULTS = {
    "gemini":    ("GEMINI_API_KEY", None, "gemini/"),
    "nvidia":    ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "openai/"),
    "openai":    ("OPENAI_API_KEY", None, ""),
    "anthropic": ("ANTHROPIC_API_KEY", None, ""),
}

PROVIDER_MODELS = {
    "gemini":    "gemini-2.5-flash",
    "nvidia":    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "openai":    "gpt-4o",
    "anthropic": "claude-sonnet-5",
}

RETRY_ATTEMPTS = 3
JSON_TIMEOUT = 120  # Max gap between streamed chunks; hosted reasoning models can pause ~25s+.
CHAT_TIMEOUT = 35


# ── Tool schema generation ──────────────────────────────────────────────────

_JSON_TYPES = {int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _param_type(annotation) -> str:
    if typing.get_origin(annotation) is typing.Union:
        annotation = next((a for a in typing.get_args(annotation) if a is not type(None)), annotation)
    return _JSON_TYPES.get(annotation, "string")


def _param_doc(doc: str, name: str) -> str:
    for line in doc.splitlines():
        line = line.strip()
        if line.startswith(f"{name}:") or line.startswith(f"{name} "):
            return line.split(":", 1)[-1].strip()
    return f"Parameter {name}"


def function_to_openai_tool(fn: typing.Callable) -> dict:
    """Derives an OpenAI-compatible tool schema from a function's signature."""
    doc = (fn.__doc__ or "").strip()
    params = inspect.signature(fn).parameters
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": {
                    name: {"type": _param_type(p.annotation), "description": _param_doc(doc, name)}
                    for name, p in params.items()
                },
                "required": [n for n, p in params.items() if p.default is inspect.Parameter.empty],
            },
        },
    }


# ── Provider resolution ─────────────────────────────────────────────────────

def resolve_llm_config() -> dict:
    """Resolves the active provider, model, key, and base URL from config."""
    provider = (config.LLM_PROVIDER or "").lower().strip()
    if not provider:
        provider = next(
            (p for p, (key_attr, _, _) in PROVIDER_DEFAULTS.items() if getattr(config, key_attr, "")),
            "gemini",
        )
    key_attr, default_base_url, _ = PROVIDER_DEFAULTS.get(provider, ("", None, ""))
    return {
        "provider": provider,
        "model": config.LLM_MODEL or PROVIDER_MODELS.get(provider, ""),
        "api_key": config.LLM_API_KEY or getattr(config, key_attr, ""),
        "base_url": config.LLM_BASE_URL or default_base_url,
    }


def resolve_fallback_config(primary: dict) -> dict | None:
    """The backup provider used when the primary keeps failing transiently.

    None when no fallback is configured, it has no API key, or it is the same
    model as the primary (retrying the same model is what already failed).
    """
    provider = (config.LLM_FALLBACK_PROVIDER or "").lower().strip()
    if provider not in PROVIDER_DEFAULTS:
        return None
    key_attr, default_base_url, _ = PROVIDER_DEFAULTS[provider]
    model = config.LLM_FALLBACK_MODEL or PROVIDER_MODELS[provider]
    api_key = getattr(config, key_attr, "")
    if not api_key or (provider, model) == (primary["provider"], primary["model"]):
        return None
    return {"provider": provider, "model": model, "api_key": api_key,
            "base_url": default_base_url, "fallback": True}


# The config that answered this thread's most recent generate_json or
# chat_with_tools call, so footers name the model actually used.
_last_used = threading.local()


def model_label(cfg: dict = None) -> str:
    """Telegram HTML footer naming the model that produced a message."""
    cfg = cfg or getattr(_last_used, "cfg", None) or resolve_llm_config()
    suffix = " (fallback)" if cfg.get("fallback") else ""
    return f"🤖 <i>Model: {html.escape(cfg['model'])}{suffix}</i>"


def format_model_name(provider: str, model_name: str) -> str:
    """Applies the litellm routing prefix a provider needs."""
    prefix = PROVIDER_DEFAULTS.get(provider, ("", None, ""))[2]
    return f"{prefix}{model_name}" if prefix and not model_name.startswith(prefix) else model_name


def get_thinking_kwargs(provider: str, model_name: str) -> dict:
    """Enables extended reasoning where the provider supports it."""
    budget = config.LLM_THINKING_BUDGET
    if budget <= 0:
        return {}
    model = model_name.lower()
    if provider == "anthropic" or "claude" in model:
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if re.search(r"\bo[134]\b|^o[134]-", model):
        return {"reasoning_effort": "high"}
    return {}


def _completion_kwargs(cfg: dict) -> tuple[str, dict]:
    model_name = format_model_name(cfg["provider"], cfg["model"])
    kwargs = {"model": model_name, "api_key": cfg["api_key"], "timeout": 60}
    if cfg["base_url"]:
        kwargs["api_base"] = cfg["base_url"]
    kwargs.update(get_thinking_kwargs(cfg["provider"], model_name))
    return model_name, kwargs


def extract_json_from_text(text: str) -> dict:
    """Pulls a JSON object out of a reply, tolerating code fences and think tags."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text.strip()).strip()
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text) or re.search(r"(\{[\s\S]*\})", text)
    return json.loads(match.group(1) if match else text)


def _is_transient_llm_error(e: Exception) -> bool:
    """Rate limits, provider 5xx, connection drops, and timeouts are retryable."""
    transient_types = tuple(
        t for t in (
            getattr(litellm, "RateLimitError", None),
            getattr(litellm, "InternalServerError", None),
            getattr(litellm, "ServiceUnavailableError", None),
            getattr(litellm, "APIConnectionError", None),
            getattr(litellm, "Timeout", None),
        ) if t is not None
    )
    if transient_types and isinstance(e, transient_types):
        return True
    msg = str(e).lower()
    return any(t in msg for t in
               ("429", "rate limit", "too many requests", "overloaded", "500", "502", "503", "timeout",
                "timed out"))


def _complete(**kwargs):
    """Calls the model, backing off on transient failures."""
    if litellm is None:
        raise RuntimeError("litellm is not installed.")
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return litellm.completion(**kwargs)
        except Exception as e:
            if not _is_transient_llm_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            wait = 3 * (attempt + 1)
            log.warning(f"Transient LLM error ({type(e).__name__}); retrying in {wait}s ({attempt + 1}/{RETRY_ATTEMPTS})")
            time.sleep(wait)


# ── Conversation history ────────────────────────────────────────────────────

def trim_history(messages: list, max_messages: int) -> list:
    """Trims history without splitting an assistant tool-call from its results.

    Cutting blindly can leave a `tool` message whose originating `tool_calls`
    was dropped, which providers reject outright, so the cut point is moved
    back to the first message that is safe to start from.
    """
    if len(messages) <= max_messages:
        return messages
    system = [m for m in messages[:1] if m.get("role") == "system"]
    body = messages[len(system):]
    start = max(0, len(body) - max_messages)
    while start < len(body) and (
        body[start].get("role") == "tool" or
        (body[start].get("role") == "assistant" and body[start].get("tool_calls"))
    ):
        start += 1
    return system + body[start:]


def _tool_call_to_dict(tc) -> dict:
    return tc.model_dump() if hasattr(tc, "model_dump") else dict(tc)


def _execute_tool(tool_map: dict, name: str, raw_args) -> str:
    fn = tool_map.get(name)
    if fn is None:
        return f"Error: Tool '{name}' not recognized."
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except ValueError:
        args = {}
    try:
        if isinstance(args, dict):
            # Ignore hallucinated parameters rather than raising TypeError.
            valid = inspect.signature(fn).parameters.keys()
            return str(fn(**{k: v for k, v in args.items() if k in valid}))
        return str(fn())
    except Exception as e:
        return f"Error executing {name}: {e}"


# ── Public entry points ─────────────────────────────────────────────────────

def _generate_json_with(cfg: dict, messages: list) -> dict:
    log.info(f"Generating JSON via {cfg['provider']} ({cfg['model']})")
    _, kwargs = _completion_kwargs(cfg)
    # litellm honours `timeout` over `request_timeout`, so override the shared default.
    kwargs["timeout"] = JSON_TIMEOUT
    # Streamed so the timeout bounds each chunk, not the whole reply: reasoning
    # models can think for minutes on the planner prompt yet never stall.
    stream = _complete(messages=messages, temperature=0.2, stream=True, **kwargs)
    content = "".join(
        chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices
    )
    return extract_json_from_text(content)


def generate_json(prompt: str, system_instruction: str = "") -> dict:
    """Asks the model for a structured JSON object and parses the reply.

    If the primary model keeps failing transiently, the request is repeated
    once on the configured fallback model.
    """
    cfg = resolve_llm_config()
    if not cfg["api_key"]:
        raise ValueError(f"No API key configured for LLM provider '{cfg['provider']}'.")

    messages = ([{"role": "system", "content": system_instruction}] if system_instruction else []) + \
               [{"role": "user", "content": prompt}]
    _last_used.cfg = cfg
    try:
        result = _generate_json_with(cfg, messages)
    except Exception as e:
        fallback = resolve_fallback_config(cfg) if _is_transient_llm_error(e) else None
        if fallback is None:
            log.error(f"Error generating JSON via '{cfg['provider']}': {e}", exc_info=True)
            raise
        log.warning(f"Primary LLM '{cfg['model']}' failed ({type(e).__name__}); "
                    f"falling back to '{fallback['model']}'")
        try:
            result = _generate_json_with(fallback, messages)
        except Exception as fallback_error:
            log.error(f"Fallback LLM '{fallback['model']}' also failed: {fallback_error}", exc_info=True)
            raise
        cfg = fallback
    _last_used.cfg = cfg
    return result


def _chat_turn(cfg: dict, messages: list, tool_map: dict, tool_schemas: list,
               user_text: str, max_iterations: int) -> str:
    """Runs the tool loop on one model, appending to `messages`; raises on API errors."""
    _, kwargs = _completion_kwargs(cfg)
    for _ in range(max_iterations):
        response = _complete(
            messages=messages, tools=tool_schemas or None,
            temperature=0.1, request_timeout=CHAT_TIMEOUT, **kwargs,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        entry = {"role": "assistant", "content": message.content or ""}
        if tool_calls:
            entry["tool_calls"] = [_tool_call_to_dict(tc) for tc in tool_calls]
        messages.append(entry)

        if not tool_calls:
            content = (message.content or "").strip()
            if content:
                return content

            # If tools were executed previously, prompt for the final summary
            if any(m.get("role") == "tool" for m in messages):
                log.info("Model returned empty content after tools; prompting for summary...")
                messages.append({"role": "user", "content": "Please summarise the results and current status in a friendly, concise response."})
                followup = _complete(messages=messages, temperature=0.1, request_timeout=CHAT_TIMEOUT, **kwargs)
                final_content = (followup.choices[0].message.content or "").strip()
                if final_content:
                    return final_content

            # If still empty on the first turn without tools, recover gracefully
            if not content and any(k in user_text.lower() for k in ("status", "charger", "battery", "solar", "state")):
                log.info("Model returned empty content for status query; fetching get_system_status directly...")
                status_str = _execute_tool(tool_map, "get_system_status", {})
                messages.append({
                    "role": "tool",
                    "tool_call_id": "direct_status",
                    "name": "get_system_status",
                    "content": status_str,
                })
                messages.append({"role": "user", "content": "Summarise the current charger and energy status in friendly Telegram HTML."})
                followup = _complete(messages=messages, temperature=0.1, request_timeout=CHAT_TIMEOUT, **kwargs)
                final_content = (followup.choices[0].message.content or "").strip()
                return final_content or status_str

            return message.content or ""

        for call in tool_calls:
            name = call.function.name
            log.info(f"LLM tool call: {name}({call.function.arguments})")
            result = _execute_tool(tool_map, name, call.function.arguments)
            log.info(f"Tool {name} result: {result[:500]}")
            messages.append({
                "role": "tool",
                "tool_call_id": getattr(call, "id", name),
                "name": name,
                "content": result,
            })

    # Iteration budget exhausted mid-tool-loop: ask for a closing summary.
    messages.append({"role": "user", "content": "Summarise the actions taken and your conclusion."})
    final = _complete(messages=messages, temperature=0.1, request_timeout=CHAT_TIMEOUT, **kwargs)
    return final.choices[0].message.content or "Completed actions."


def chat_with_tools(history: list, user_text: str, tools: list,
                    system_instruction: str = "", max_iterations: int = 12) -> tuple[str, list]:
    """Runs a tool-calling conversation turn and returns (reply, new history).

    If the primary model fails transiently before any tool has run, the turn
    is restarted on the fallback model. Once a tool has acted (e.g. started
    the charger) the turn is not replayed, so no action can run twice.
    """
    cfg = resolve_llm_config()
    if not cfg["api_key"]:
        message = f"API key is missing for LLM provider '{cfg['provider']}'. Please configure it."
        log.error(message)
        return message, history
    log.info(f"Chat via {cfg['provider']} ({cfg['model']})")

    tool_map = {fn.__name__: fn for fn in tools}
    tool_schemas = [function_to_openai_tool(fn) for fn in tools]

    def fresh_messages() -> list:
        messages = list(history or [])
        if system_instruction and not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system_instruction})
        if user_text:
            messages.append({"role": "user", "content": user_text})
        return messages

    messages = fresh_messages()
    turn_start = len(messages)
    _last_used.cfg = cfg
    try:
        reply = _chat_turn(cfg, messages, tool_map, tool_schemas, user_text, max_iterations)
    except Exception as e:
        tools_ran = any(m.get("role") == "tool" for m in messages[turn_start:])
        fallback = resolve_fallback_config(cfg) if _is_transient_llm_error(e) and not tools_ran else None
        if fallback is None:
            log.error(f"Error in chat via '{cfg['provider']}': {e}", exc_info=True)
            return f"Error interacting with the AI model: {e}", messages
        log.warning(f"Primary LLM '{cfg['model']}' failed ({type(e).__name__}); "
                    f"retrying the turn on fallback '{fallback['model']}'")
        messages = fresh_messages()
        try:
            reply = _chat_turn(fallback, messages, tool_map, tool_schemas, user_text, max_iterations)
        except Exception as fallback_error:
            log.error(f"Error in chat via fallback '{fallback['provider']}': {fallback_error}", exc_info=True)
            return f"Error interacting with the AI model: {fallback_error}", messages
        cfg = fallback
    _last_used.cfg = cfg
    return reply, messages
