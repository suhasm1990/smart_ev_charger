import os
import re
import json
import inspect
import typing

from core import config
from reporting.logger import log

try:
    import litellm
    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.drop_params = True
except Exception:
    pass

# ── Tool / Function Spec Generator -----------------------------------------

def function_to_openai_tool(fn: typing.Callable) -> dict:
    """Converts a Python function into an OpenAI-compatible Tool specification."""
    sig = inspect.signature(fn)
    doc = (fn.__doc__ or "").strip()
    
    properties = {}
    required = []
    
    for param_name, param in sig.parameters.items():
        param_type = "string"
        ann = param.annotation
        origin = typing.get_origin(ann)
        if origin is typing.Union:
            non_none = [a for a in typing.get_args(ann) if a is not type(None)]
            if non_none:
                ann = non_none[0]

        if ann in (int,):
            param_type = "integer"
        elif ann in (float,):
            param_type = "number"
        elif ann in (bool,):
            param_type = "boolean"
        elif ann in (dict, list):
            param_type = "object"
            
        param_doc = f"Parameter {param_name}"
        if doc and ("Args:" in doc or f"{param_name}:" in doc):
            for line in doc.split("\n"):
                line = line.strip()
                if line.startswith(f"{param_name}:") or line.startswith(f"{param_name} "):
                    param_doc = line.split(":", 1)[-1].strip()
                    break

        properties[param_name] = {
            "type": param_type,
            "description": param_doc
        }
        
        if param.default == inspect.Parameter.empty:
            required.append(param_name)
            
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }


PROVIDER_DEFAULTS = {
    "gemini": ("GEMINI_API_KEY", None, "gemini/"),
    "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "openai/"),
    "openai": ("OPENAI_API_KEY", None, ""),
    "anthropic": ("ANTHROPIC_API_KEY", None, ""),
}

def resolve_llm_config() -> dict:
    """Resolves provider, model, api_key, and base_url directly from environment/config."""
    provider = (config.LLM_PROVIDER or "").lower().strip()
    model = config.LLM_MODEL
    api_key = config.LLM_API_KEY
    base_url = config.LLM_BASE_URL

    if not provider:
        for p, (key_attr, _, _) in PROVIDER_DEFAULTS.items():
            if getattr(config, key_attr, ""):
                provider = p
                break
        provider = provider or "gemini"

    key_attr, default_base_url, _ = PROVIDER_DEFAULTS.get(provider, ("", None, ""))
    if not api_key and key_attr:
        api_key = getattr(config, key_attr, "")

    if not base_url and default_base_url:
        base_url = default_base_url

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url
    }


def format_model_name(provider: str, model_name: str) -> str:
    """Formats model name with appropriate provider prefix for litellm."""
    if provider == "nvidia" and not model_name.startswith("openai/"):
        return f"openai/{model_name}"
    elif provider == "gemini" and not model_name.startswith("gemini/"):
        return f"gemini/{model_name}"
    return model_name


def extract_json_from_text(text: str) -> dict:
    """Safely extracts JSON dict from response text even if wrapped in markdown codeblocks or thinking tags."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text.strip()).strip()
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text) or re.search(r"(\{[\s\S]*\})", text)
    return json.loads(match.group(1)) if match else json.loads(text)


def get_thinking_kwargs(provider: str, model_name: str) -> dict:
    """Returns provider-appropriate extended thinking / reasoning parameters."""
    kwargs = {}
    budget = getattr(config, "LLM_THINKING_BUDGET", 8192)
    if budget and budget > 0:
        if provider == "anthropic" or "claude" in model_name.lower():
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif "o1" in model_name.lower() or "o3" in model_name.lower() or "o4" in model_name.lower():
            kwargs["reasoning_effort"] = "high"
    return kwargs


# ── Unified LLM Methods ------------------------------------------------------

def generate_json(prompt: str, system_instruction: str = "") -> dict:
    """Generates structured JSON response from the configured LLM."""
    cfg = resolve_llm_config()
    log.info(f"Generating JSON with LLM Provider: '{cfg['provider']}', Model: '{cfg['model']}'")

    if not cfg["api_key"]:
        err_msg = f"No API key found for LLM provider '{cfg['provider']}'."
        log.error(err_msg)
        raise ValueError(err_msg)

    try:
        import litellm
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        extra_kwargs = {}
        if cfg["base_url"]:
            extra_kwargs["api_base"] = cfg["base_url"]

        model_name = format_model_name(cfg["provider"], cfg["model"])
        extra_kwargs.update(get_thinking_kwargs(cfg["provider"], model_name))

        response = litellm.completion(
            model=model_name,
            messages=messages,
            api_key=cfg["api_key"],
            temperature=0.2,
            request_timeout=45,
            **extra_kwargs
        )
        content = response.choices[0].message.content
        return extract_json_from_text(content)
    except Exception as err:
        log.error(f"Error generating JSON with LLM provider '{cfg['provider']}': {err}", exc_info=True)
        raise

        extra_kwargs.update(get_thinking_kwargs(cfg["provider"], model_name))

        response = litellm.completion(
            model=model_name,
            messages=messages,
            api_key=cfg["api_key"],
            temperature=0.2,
            request_timeout=45,
            **extra_kwargs
        )
        content = response.choices[0].message.content
        return extract_json_from_text(content)
    except Exception as err:
        log.error(f"Error generating JSON with LLM provider '{cfg['provider']}': {err}", exc_info=True)
        raise


def _safe_completion_with_retry(litellm_mod, **kwargs):
    """Executes litellm.completion with automatic retry on RateLimit (HTTP 429)."""
    import time
    for attempt in range(3):
        try:
            return litellm_mod.completion(**kwargs)
        except Exception as err:
            err_str = str(err).lower()
            if ("429" in err_str or "rate" in err_str or "too many requests" in err_str) and attempt < 2:
                wait_sec = (attempt + 1) * 3
                log.warning(f"Rate limit encountered. Retrying in {wait_sec}s (attempt {attempt+1}/3)...")
                time.sleep(wait_sec)
            else:
                raise


def chat_with_tools(history: list, user_text: str, tools: list, system_instruction: str = "", max_iterations: int = 12) -> tuple[str, list]:
    """Runs a multi-turn chat interaction with function/tool execution support."""
    cfg = resolve_llm_config()
    log.info(f"Chat interaction with LLM Provider: '{cfg['provider']}', Model: '{cfg['model']}'")

    if not cfg["api_key"]:
        err_msg = f"API key is missing for LLM provider '{cfg['provider']}'. Please configure environment variables."
        log.error(err_msg)
        return (err_msg, history)

    tool_map = {fn.__name__: fn for fn in tools}
    openai_tools = [function_to_openai_tool(fn) for fn in tools]

    messages = list(history) if history else []
    if system_instruction and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": system_instruction})

    if user_text:
        messages.append({"role": "user", "content": user_text})

    try:
        import litellm
        extra_kwargs = {}
        if cfg["base_url"]:
            extra_kwargs["api_base"] = cfg["base_url"]

        model_name = format_model_name(cfg["provider"], cfg["model"])
        extra_kwargs.update(get_thinking_kwargs(cfg["provider"], model_name))

        for iteration in range(max_iterations):
            response = _safe_completion_with_retry(
                litellm,
                model=model_name,
                messages=messages,
                tools=openai_tools if openai_tools else None,
                api_key=cfg["api_key"],
                temperature=0.1,
                request_timeout=35,
                **extra_kwargs
            )

            msg = response.choices[0].message
            msg_dict = {"role": "assistant", "content": msg.content or ""}
            if getattr(msg, "tool_calls", None):
                msg_dict["tool_calls"] = [tc.model_dump() if hasattr(tc, "model_dump") else dict(tc) for tc in msg.tool_calls]
            messages.append(msg_dict)

            if not getattr(msg, "tool_calls", None):
                return (msg.content or "", messages)

            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args_str = tool_call.function.arguments
                tool_call_id = getattr(tool_call, "id", fn_name)

                try:
                    fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                except Exception:
                    fn_args = {}

                log.info(f"LLM requesting tool call: {fn_name}({fn_args})")

                if fn_name in tool_map:
                    try:
                        fn = tool_map[fn_name]
                        if isinstance(fn_args, dict):
                            sig = inspect.signature(fn)
                            valid_params = sig.parameters.keys()
                            filtered_args = {k: v for k, v in fn_args.items() if k in valid_params}
                            result = fn(**filtered_args)
                        else:
                            result = fn()
                    except Exception as exec_err:
                        result = f"Error executing {fn_name}: {exec_err}"
                else:
                    result = f"Error: Tool '{fn_name}' not recognized."

                log.info(f"Tool {fn_name} result: {result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": fn_name,
                    "content": str(result)
                })

        # Graceful wrap-up if max iterations reached during multi-turn tool execution
        messages.append({"role": "user", "content": "Please provide a final summary of all actions taken and the conclusion."})
        final_resp = _safe_completion_with_retry(
            litellm,
            model=model_name,
            messages=messages,
            api_key=cfg["api_key"],
            temperature=0.1,
            request_timeout=35,
            **extra_kwargs
        )
        return (final_resp.choices[0].message.content or "Completed actions.", messages)

    except Exception as err:
        log.error(f"Error in chat interaction with LLM provider '{cfg['provider']}': {err}", exc_info=True)
        return (f"Error interacting with AI model: {err}", messages)
