"""Model-agnostic LLM access (Gemini, NVIDIA, OpenAI, Anthropic) via litellm."""
import inspect
import json
import re
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
    "nvidia":    "nvidia/nemotron-3-super-120b-a12b",
    "openai":    "gpt-4o",
    "anthropic": "claude-sonnet-5",
}

RETRY_ATTEMPTS = 3
JSON_TIMEOUT = 90
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
    kwargs = {"model": model_name, "api_key": cfg["api_key"]}
    if cfg["base_url"]:
        kwargs["api_base"] = cfg["base_url"]
    kwargs.update(get_thinking_kwargs(cfg["provider"], model_name))
    return model_name, kwargs


def extract_json_from_text(text: str) -> dict:
    """Pulls a JSON object out of a reply, tolerating code fences and think tags."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text.strip()).strip()
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text) or re.search(r"(\{[\s\S]*\})", text)
    return json.loads(match.group(1) if match else text)


def _complete(**kwargs):
    """Calls the model, backing off on rate limits."""
    if litellm is None:
        raise RuntimeError("litellm is not installed.")
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return litellm.completion(**kwargs)
        except Exception as e:
            transient = any(t in str(e).lower() for t in ("429", "rate limit", "too many requests", "overloaded"))
            if not transient or attempt == RETRY_ATTEMPTS - 1:
                raise
            wait = 3 * (attempt + 1)
            log.warning(f"Rate limited; retrying in {wait}s ({attempt + 1}/{RETRY_ATTEMPTS})")
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

def generate_json(prompt: str, system_instruction: str = "") -> dict:
    """Asks the model for a structured JSON object and parses the reply."""
    cfg = resolve_llm_config()
    if not cfg["api_key"]:
        raise ValueError(f"No API key configured for LLM provider '{cfg['provider']}'.")
    log.info(f"Generating JSON via {cfg['provider']} ({cfg['model']})")

    messages = ([{"role": "system", "content": system_instruction}] if system_instruction else []) + \
               [{"role": "user", "content": prompt}]
    _, kwargs = _completion_kwargs(cfg)
    try:
        response = _complete(messages=messages, temperature=0.2, request_timeout=JSON_TIMEOUT, **kwargs)
        return extract_json_from_text(response.choices[0].message.content)
    except Exception as e:
        log.error(f"Error generating JSON via '{cfg['provider']}': {e}", exc_info=True)
        raise


def chat_with_tools(history: list, user_text: str, tools: list,
                    system_instruction: str = "", max_iterations: int = 12) -> tuple[str, list]:
    """Runs a tool-calling conversation turn and returns (reply, new history)."""
    cfg = resolve_llm_config()
    if not cfg["api_key"]:
        message = f"API key is missing for LLM provider '{cfg['provider']}'. Please configure it."
        log.error(message)
        return message, history
    log.info(f"Chat via {cfg['provider']} ({cfg['model']})")

    tool_map = {fn.__name__: fn for fn in tools}
    tool_schemas = [function_to_openai_tool(fn) for fn in tools]

    messages = list(history or [])
    if system_instruction and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": system_instruction})
    if user_text:
        messages.append({"role": "user", "content": user_text})

    _, kwargs = _completion_kwargs(cfg)
    try:
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
                return message.content or "", messages

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
        return final.choices[0].message.content or "Completed actions.", messages
    except Exception as e:
        log.error(f"Error in chat via '{cfg['provider']}': {e}", exc_info=True)
        return f"Error interacting with the AI model: {e}", messages
