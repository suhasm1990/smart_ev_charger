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
        if param.annotation in (int,):
            param_type = "integer"
        elif param.annotation in (float,):
            param_type = "number"
        elif param.annotation in (bool,):
            param_type = "boolean"
        elif param.annotation in (dict, list):
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


# ── Configuration Resolver --------------------------------------------------

def resolve_llm_config() -> dict:
    """Resolves provider, model, api_key, and base_url directly from environment/config."""
    provider = config.LLM_PROVIDER
    model = config.LLM_MODEL
    api_key = config.LLM_API_KEY
    base_url = config.LLM_BASE_URL

    # Auto-detect provider if not explicitly specified
    if not provider:
        if config.NVIDIA_API_KEY:
            provider = "nvidia"
        elif config.OPENAI_API_KEY:
            provider = "openai"
        elif config.ANTHROPIC_API_KEY:
            provider = "anthropic"
        elif config.GEMINI_API_KEY:
            provider = "gemini"

    # Select provider-specific key if LLM_API_KEY is not set
    if not api_key:
        if provider == "nvidia":
            api_key = config.NVIDIA_API_KEY
        elif provider == "openai":
            api_key = config.OPENAI_API_KEY
        elif provider == "anthropic":
            api_key = config.ANTHROPIC_API_KEY
        elif provider == "gemini":
            api_key = config.GEMINI_API_KEY

    # Set default base URL for known providers if not specified
    if provider == "nvidia" and not base_url:
        base_url = "https://integrate.api.nvidia.com/v1"

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url
    }


def extract_json_from_text(text: str) -> dict:
    """Safely extracts JSON dict from response text even if wrapped in markdown codeblocks or thinking tags."""
    text = text.strip()
    # Strip <think>...</think> reasoning blocks if present
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        text = match.group(1)
    else:
        match_raw = re.search(r"(\{[\s\S]*\})", text)
        if match_raw:
            text = match_raw.group(1)
    return json.loads(text)


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

        model_name = cfg["model"]
        if cfg["provider"] == "nvidia" and not model_name.startswith("openai/") and not model_name.startswith("nvidia/"):
            model_name = f"openai/{model_name}"
        elif cfg["provider"] == "nvidia" and model_name.startswith("nvidia/"):
            model_name = f"openai/{model_name}"
        elif cfg["provider"] == "gemini" and not model_name.startswith("gemini/"):
            model_name = f"gemini/{model_name}"

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


def chat_with_tools(history: list, user_text: str, tools: list, system_instruction: str = "") -> tuple[str, list]:
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

        model_name = cfg["model"]
        if cfg["provider"] == "nvidia" and not model_name.startswith("openai/") and not model_name.startswith("nvidia/"):
            model_name = f"openai/{model_name}"
        elif cfg["provider"] == "gemini" and not model_name.startswith("gemini/"):
            model_name = f"gemini/{model_name}"

        for iteration in range(5):
            response = litellm.completion(
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
                        result = tool_map[fn_name](**fn_args)
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

    except Exception as err:
        log.error(f"Error in chat interaction with LLM provider '{cfg['provider']}': {err}", exc_info=True)
        return (f"Error interacting with AI model: {err}", messages)

    return ("Unable to process request after max iterations.", messages)
