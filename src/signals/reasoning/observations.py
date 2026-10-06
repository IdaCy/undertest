"""Allowlisted views of an explicitly bounded actor transcript."""

from copy import deepcopy
from typing import Any, Literal


Channel = Literal["context", "cot", "action", "combined"]
CHANNELS = ("context", "cot", "action", "combined")
_SCHEMA_SCALARS = {
    "type", "title", "description", "enum", "const", "default", "$ref", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "required",
}


def _json_value(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("expected JSON-compatible data")


def _schema(value: Any) -> dict | bool:
    if type(value) is bool:
        return value
    if not isinstance(value, dict):
        raise ValueError("tool parameters must be a JSON schema")
    result: dict[str, Any] = {}
    for key in sorted(_SCHEMA_SCALARS & value.keys()):
        result[key] = _json_value(value[key])
    for key in ("properties", "$defs", "definitions", "patternProperties"):
        if key in value:
            children = value[key]
            if not isinstance(children, dict) or not all(isinstance(k, str) for k in children):
                raise ValueError(f"schema {key} must be an object")
            result[key] = {name: _schema(child) for name, child in children.items()}
    for key in ("items", "additionalProperties", "not", "if", "then", "else"):
        if key in value:
            result[key] = _schema(value[key])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if key in value:
            if not isinstance(value[key], list):
                raise ValueError(f"schema {key} must be a list")
            result[key] = [_schema(child) for child in value[key]]
    return result


def _tools(tools: Any) -> list[dict]:
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    result = []
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("only OpenAI function tool definitions are supported")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("tool names must be nonempty and unique")
        names.add(name)
        clean = {"name": name}
        if "description" in function:
            if not isinstance(function["description"], str):
                raise ValueError("tool description must be text")
            clean["description"] = function["description"]
        if "parameters" in function:
            clean["parameters"] = _schema(function["parameters"])
        if "strict" in function:
            if type(function["strict"]) is not bool:
                raise ValueError("tool strict must be boolean")
            clean["strict"] = function["strict"]
        result.append({"type": "function", "function": clean})
    return result


def _text(content: Any, *, initial: bool = False) -> str | list[dict] | None:
    if isinstance(content, str):
        return content
    if content is None and not initial:
        return None
    if not isinstance(content, list):
        raise ValueError("message content must be text, typed content, or null")
    result = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("content blocks must be objects")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("text content must contain text")
            result.append({"type": "text", "text": block["text"]})
        elif initial:
            raise ValueError("initial context supports text blocks only")
        # Other generated content is not public text, including reasoning.
    return result


def _initial_context(initial_context: Any) -> list[dict]:
    if not isinstance(initial_context, list):
        raise ValueError("initial_context must be a list")
    result = []
    for message in initial_context:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user"):
            raise ValueError("initial context must contain system/user messages only")
        result.append({"role": message["role"], "content": _text(message.get("content"), initial=True)})
    return result


def _reasoning(message: dict) -> tuple[list[str], bool, bool]:
    parts: list[str] = []
    explicit_empty = False
    unavailable = False
    seen = False

    def add(value: Any, *, redacted: bool = False) -> None:
        nonlocal explicit_empty, unavailable, seen
        seen = True
        if redacted or value is None:
            unavailable = True
        elif isinstance(value, str):
            if value.strip():
                parts.append(value)
            else:
                explicit_empty = True
        else:
            unavailable = True

    for key in ("reasoning_content", "reasoning"):
        if key in message:
            add(message[key])
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "reasoning":
                add(block.get("reasoning", block.get("text")), redacted=bool(block.get("redacted")))
    return parts, explicit_empty, unavailable or not seen


def _tool_calls(value: Any, aliases: dict[str, str], counter: int) -> tuple[list[dict], int]:
    if not isinstance(value, list):
        raise ValueError("tool_calls must be a list")
    result = []
    batch_ids = set()
    for call in value:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("only function tool calls are supported")
        original_id = call.get("id")
        function = call.get("function")
        if not isinstance(original_id, str) or not original_id or original_id in batch_ids:
            raise ValueError("tool call IDs must be nonempty and unique within a batch")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ValueError("tool call function must have a name")
        if not isinstance(function.get("arguments"), str):
            raise ValueError("OpenAI function arguments must be a string")
        counter += 1
        alias = f"call_{counter}"
        aliases[original_id] = alias
        batch_ids.add(original_id)
        result.append({"id": alias, "type": "function", "function": {
            "name": function["name"], "arguments": function["arguments"],
        }})
    return result, counter


def project_observation(
    initial_context: list[dict], messages: list[dict], tools: list[dict],
    channel: Channel, cutoff: int,
) -> dict:
    """Project only the first ``cutoff`` generated messages into one channel.

    Inputs are actor-visible context and raw OpenAI-format actor messages;
    trusted checker outputs must never be put in their visible content.
    Unknown metadata fields are dropped. Tool-call IDs become local ordinals.
    ``cutoff`` is audit metadata, not context-baseline model input when episode
    lengths differ. ``present`` means some readable reasoning was supplied,
    not that its capture is complete. ``absent`` means explicitly empty text,
    never an absence of awareness. Missing/null/redacted reasoning is unavailable.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown observation channel: {channel}")
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    if type(cutoff) is not int or not 0 <= cutoff <= len(messages):
        raise ValueError("cutoff must be an integer within the generated message count")
    result = {
        "schema_version": 1, "channel": channel, "cutoff": cutoff,
        "initial_context": _initial_context(initial_context), "tools": _tools(tools),
        "messages": [],
    }
    if channel == "context":
        return result

    include_reasoning = channel in ("cot", "combined")
    include_action = channel in ("action", "combined")
    any_reasoning = False
    any_empty = False
    any_unavailable = False
    aliases: dict[str, str] = {}
    counter = 0
    for message in messages[:cutoff]:
        if not isinstance(message, dict) or message.get("role") not in ("assistant", "tool"):
            raise ValueError("generated messages must have assistant/tool roles")
        if message["role"] == "tool":
            if include_action:
                original_id = message.get("tool_call_id")
                if not isinstance(original_id, str) or original_id not in aliases:
                    raise ValueError("tool result has no preceding tool call in the prefix")
                result["messages"].append({
                    "role": "tool", "tool_call_id": aliases[original_id],
                    "content": _text(message.get("content")),
                })
            continue

        projected: dict[str, Any] = {"role": "assistant"}
        if include_action:
            content = _text(message.get("content"))
            if content is not None and content != []:
                projected["content"] = content
            if message.get("refusal") is not None:
                if not isinstance(message["refusal"], str):
                    raise ValueError("assistant refusal must be text")
                if message["refusal"]:
                    projected["refusal"] = message["refusal"]
            if message.get("tool_calls") is not None:
                calls, counter = _tool_calls(message["tool_calls"], aliases, counter)
                if calls:
                    projected["tool_calls"] = calls
        if include_reasoning:
            parts, empty, unavailable = _reasoning(message)
            any_reasoning |= bool(parts)
            any_empty |= empty
            any_unavailable |= unavailable
            if parts:
                projected["reasoning"] = parts
        if len(projected) > 1:
            result["messages"].append(projected)

    if include_reasoning:
        result["reasoning_status"] = (
            "present" if any_reasoning else
            "absent" if any_empty and not any_unavailable else "unavailable"
        )
    return deepcopy(result)
