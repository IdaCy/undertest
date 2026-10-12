"""Offline counts for the text-only Hugging Face path in vLLM 0.28.

No tokenizer is downloaded or constructed here. Callers supply a tokenizer and
bind its assets, implementation, template and serving configuration separately.
The supported request subset deliberately excludes media, custom templates,
truncation, embedding prompts and forwarded token IDs.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from typing import Any


def _require(value: bool, reason: str) -> None:
    if not value:
        raise ValueError(reason)


def text_conversation(messages: list[dict[str, Any]], *, content_format: str = "string") -> list[dict[str, Any]]:
    """Normalize the supported subset of vLLM's string-content conversation."""
    _require(isinstance(messages, list) and bool(messages), "messages_required")
    _require(content_format in ("string", "openai"), "explicit_content_format_required")
    conversation = []
    for message in messages:
        role = message.get("role")
        _require(role in ("system", "user", "assistant", "tool"), "unsupported_role")
        content = message.get("content")
        if content is None:
            parts = []
        elif isinstance(content, str):
            parts = [content]
        else:
            _require(isinstance(content, list), "unsupported_content")
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                else:
                    _require(isinstance(part, dict) and set(part) <= {"type", "text"} and part.get("type") == "text"
                             and isinstance(part.get("text"), str), "unsupported_content_part")
                    parts.append(part["text"])
        # Native string-content parsing ignores empty text parts before joining.
        if content_format == "openai":
            normalized = "\n".join(parts) if role == "tool" else [{"type": "text", "text": p} for p in parts]
        else:
            normalized = "\n".join(p for p in parts if p)
        result: dict[str, Any] = {"role": role, "content": normalized}
        if role == "assistant":
            calls = message.get("tool_calls")
            if calls is not None:
                _require(isinstance(calls, list), "unsupported_tool_calls")
                calls = copy.deepcopy(calls)
                for call in calls:
                    _require(isinstance(call, dict) and call.get("type", "function") == "function"
                             and isinstance(call.get("function"), dict), "unsupported_tool_call")
                    arguments = call["function"].get("arguments")
                    if not arguments:
                        arguments = {}
                    elif not isinstance(arguments, (dict, list)):
                        arguments = json.loads(arguments)
                        if arguments is None:
                            arguments = {}
                    call["function"]["arguments"] = arguments
                if calls:
                    result["tool_calls"] = calls
            reasoning = message.get("reasoning")
            if reasoning is None:
                reasoning = message.get("reasoning_content")
            if reasoning is not None:
                _require(isinstance(reasoning, str), "unsupported_reasoning")
                result["reasoning"] = result["reasoning_content"] = reasoning
        elif role == "tool" and "tool_call_id" in message:
            result["tool_call_id"] = message["tool_call_id"]
        for key in ("name", "task"):
            if isinstance(message.get(key), str):
                result[key] = message[key]
        conversation.append(result)
    return conversation


def function_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Reproduce the native function-tool model_dump defaults and field order."""
    if tools is None:
        return None
    _require(isinstance(tools, list) and bool(tools), "nonempty_tools_required")
    output = []
    for tool in tools:
        _require(isinstance(tool, dict) and tool.get("type", "function") == "function"
                 and isinstance(tool.get("function"), dict), "unsupported_tool")
        original = tool["function"]
        _require(isinstance(original.get("name"), str), "function_name_required")
        _require(original.get("description") is None or isinstance(original["description"], str), "function_description_type")
        _require(original.get("parameters") is None or isinstance(original["parameters"], dict), "function_parameters_type")
        for value in (original.get("strict"), original.get("defer_loading"), tool.get("defer_loading")):
            _require(value is None or type(value) is bool, "function_boolean_type")
        function = {"name": original["name"], "description": original.get("description"),
                    "parameters": copy.deepcopy(original.get("parameters"))}
        if original.get("strict") is not None:
            function["strict"] = original["strict"]
        deferred = original.get("defer_loading")
        if deferred is None:
            deferred = tool.get("defer_loading")
        if deferred is not None:
            function["defer_loading"] = deferred
        row = {"type": "function", "function": function}
        if tool.get("defer_loading") is not None:
            row["defer_loading"] = tool["defer_loading"]
        output.append(row)
    return output


def render_text_chat(request: dict[str, Any], tokenizer: Any, *,
                     template_variables: set[str], base_parameters: set[str],
                     content_format: str,
                     exclude_tools_when_tool_choice_none: bool = False) -> str:
    """Render the native text path without retaining or emitting its contents.

    ``template_variables`` must come from parsing the exact pinned Jinja template;
    ``base_parameters`` from the installed HF PythonBackend.apply_chat_template.
    vLLM filters optional kwargs against those plus the concrete tokenizer method.
    """
    for key in ("chat_template", "truncate_prompt_tokens", "truncation_side", "documents",
                "mm_processor_kwargs", "media_io_kwargs", "kv_transfer_params",
                "prompt_embeds", "prompt_token_ids"):
        _require(request.get(key) is None, "unsupported_render_option:" + key)
    _require(request.get("add_special_tokens", False) is False, "extra_special_tokens")
    _require(request.get("continue_final_message", False) is False, "continued_final_message")
    _require(request.get("add_generation_prompt", True) is True, "generation_prompt_required")
    _require(request.get("return_assistant_tokens_mask", False) is False, "assistant_mask_unsupported")
    _require(request.get("tool_choice", "none") == "none", "tool_choice_none_required")
    _require(isinstance(tokenizer.chat_template, str) and bool(tokenizer.chat_template), "template_required")
    supplied = request.get("chat_template_kwargs") or {}
    _require(isinstance(supplied, dict), "chat_kwargs_object_required")
    _require(not ({"chat_template", "tokenize", "tools", "add_generation_prompt",
                   "continue_final_message", "return_dict", "return_assistant_tokens_mask"} & supplied.keys()),
             "reserved_chat_kwarg")
    tools = function_tools(request.get("tools"))
    if exclude_tools_when_tool_choice_none:
        tools = None
    # ChatCompletionRequest.build_chat_params: non-null explicit API fields
    # override nested kwargs; inferred thinking applies only without an override.
    kwargs = dict(supplied)
    kwargs.update(add_generation_prompt=True, continue_final_message=False)
    effort = request.get("reasoning_effort")
    if effort is not None:
        kwargs["reasoning_effort"] = effort
        if "enable_thinking" not in supplied:
            kwargs["enable_thinking"] = effort != "none"
    # ChatParams.with_defaults ignores unset request overrides. The serving
    # defaults contain tools/tokenize, so this merge runs for this native path.
    kwargs = {k: v for k, v in kwargs.items() if v not in (None, "auto")}
    signature = inspect.signature(tokenizer.apply_chat_template)
    accepted = template_variables | base_parameters | {
        k for k, v in signature.parameters.items()
        if v.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    }
    kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    rendered = tokenizer.apply_chat_template(
        conversation=text_conversation(request["messages"], content_format=content_format), tools=tools,
        chat_template=tokenizer.chat_template, tokenize=False, return_dict=False, **kwargs)
    _require(isinstance(rendered, str), "text_render_required")
    return rendered


def count_text_chat(request: dict[str, Any], tokenizer: Any, **render_options: Any) -> dict[str, Any]:
    """Return counts and opaque hashes only, without storing prompt/token text."""
    rendered = render_text_chat(request, tokenizer, **render_options)
    tokens = tokenizer.encode(rendered, add_special_tokens=False)
    _require(isinstance(tokens, list) and all(type(x) is int and x >= 0 for x in tokens), "token_ids_required")
    maximum = request.get("max_completion_tokens", request.get("max_tokens"))
    _require(type(maximum) is int and maximum > 0, "positive_output_allowance_required")
    return {"prompt_tokens": len(tokens), "output_allowance": maximum,
            "maximum_total_tokens": len(tokens) + maximum,
            "rendered_utf8_bytes": len(rendered.encode()),
            "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "token_ids_sha256": hashlib.sha256(json.dumps(tokens, separators=(",", ":")).encode()).hexdigest()}
