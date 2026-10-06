"""Prepare independent Qwen interview requests without dispatching or executing tools."""

import copy
import hashlib
import json
import math
from pathlib import Path

from signals.validation.controls.runner import digest


SCHEMA_VERSION = "sigres-qwen-interview-preparation-v1"
_GENERATION_KEYS = {
    "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
    "frequency_penalty", "presence_penalty", "chat_template_kwargs",
}
_ASSISTANT_KEYS = {
    "role", "content", "reasoning", "reasoning_content", "refusal", "tool_calls",
}


class UnsupportedHistory(ValueError):
    """The source is retained, but this adapter cannot prepare a faithful fork."""


def _finite_json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _finite_json(item)
        return
    elif type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _finite_json(item)
        return
    raise ValueError("Expected finite JSON with string object keys")


def read_original(path: Path, *, expected_bytes: int, expected_file_sha256: str) -> dict:
    """Read exact frozen bytes; reject duplicate keys instead of normalizing them."""
    if type(expected_bytes) is not int or expected_bytes < 1:
        raise ValueError("Expected a positive source size")
    if path.stat().st_size != expected_bytes:
        raise ValueError("Original trajectory size changed")
    raw = path.read_bytes()
    if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_file_sha256:
        raise ValueError("Original trajectory bytes changed")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key in original trajectory")
            result[key] = value
        return result

    original = json.loads(raw, object_pairs_hook=unique)
    _finite_json(original)
    if type(original) is not dict:
        raise ValueError("Original trajectory must be an object")
    return original


def _prefix(original: dict) -> list[dict]:
    if (type(original.get("schema_version")) is not int or original["schema_version"] != 1
            or "continuation" in original or "branch_id" in original):
        raise UnsupportedHistory("This adapter requires an original runner-v1 episode")
    if original.get("status") not in {"completed", "call_limit", "token_limit"}:
        raise UnsupportedHistory("The original does not establish a completed native boundary")
    initial, messages, calls = (original.get(key) for key in ("initial_context", "messages", "calls"))
    if type(initial) is not list or len(initial) != 2:
        raise UnsupportedHistory("Expected the original system and user turns")
    for message, role in zip(initial, ("system", "user")):
        if (type(message) is not dict or set(message) != {"role", "content"}
                or message["role"] != role or type(message["content"]) is not str):
            raise UnsupportedHistory("Unsupported original instruction format")
    if type(messages) is not list or not messages or type(calls) is not list:
        raise UnsupportedHistory("Missing complete original messages or generation records")
    if type(original.get("retrospective_cutoff")) is not int or original["retrospective_cutoff"] != len(messages):
        raise UnsupportedHistory("The end cutoff does not match the original history")
    if type(original.get("completed_calls")) is not int or original["completed_calls"] != len(calls):
        raise UnsupportedHistory("Generation accounting does not match the original calls")

    tools = original.get("tools")
    if type(tools) is not list or not tools:
        raise UnsupportedHistory("Original tool definitions are missing")
    names = set()
    for tool in tools:
        if type(tool) is not dict or tool.get("type") != "function" or type(tool.get("function")) is not dict:
            raise UnsupportedHistory("Unsupported original tool definition")
        name = tool["function"].get("name")
        if type(name) is not str or not name or name in names:
            raise UnsupportedHistory("Tool definitions need distinct names")
        names.add(name)

    pending = set()
    assistant_index = 0
    previous_role = "user"
    for message in messages:
        if type(message) is not dict:
            raise UnsupportedHistory("Invalid native message")
        role = message.get("role")
        if role == "assistant":
            if pending or previous_role == "assistant":
                raise UnsupportedHistory("Assistant response is not at a completed turn boundary")
            if not {"role", "content"} <= set(message) <= _ASSISTANT_KEYS:
                raise UnsupportedHistory("Unknown assistant fields must not be silently dropped")
            if message.get("content") is not None and type(message["content"]) is not str:
                raise UnsupportedHistory("This Qwen path supports text or null content only")
            if message.get("refusal") not in (None, ""):
                raise UnsupportedHistory("The pinned Qwen template does not render refusal fields")
            first, alias = message.get("reasoning"), message.get("reasoning_content")
            if any(value is not None and type(value) is not str for value in (first, alias)):
                raise UnsupportedHistory("This Qwen path requires text reasoning")
            if first is not None and alias is not None and first != alias:
                raise UnsupportedHistory("Conflicting reasoning aliases")
            if assistant_index >= len(calls):
                raise UnsupportedHistory("Assistant message has no generation record")
            call = calls[assistant_index]
            if (type(call) is not dict or type(call.get("index")) is not int
                    or call["index"] != assistant_index or call.get("finish_reason") not in {"stop", "tool_calls"}):
                raise UnsupportedHistory("Incomplete or unsupported generation boundary")
            tool_calls = message.get("tool_calls")
            if tool_calls is None:
                tool_calls = []
            if type(tool_calls) is not list:
                raise UnsupportedHistory("Malformed tool-call list")
            if call["finish_reason"] == "tool_calls" and not tool_calls:
                raise UnsupportedHistory("Tool-call finish reason without a call")
            for tool_call in tool_calls:
                if (type(tool_call) is not dict or set(tool_call) != {"id", "type", "function"}
                        or tool_call["type"] != "function"):
                    raise UnsupportedHistory("Unsupported function call")
                call_id, function = tool_call["id"], tool_call["function"]
                if type(call_id) is not str or not call_id or call_id in pending:
                    raise UnsupportedHistory("Duplicate or invalid pending tool-call ID")
                if (type(function) is not dict or set(function) != {"name", "arguments"}
                        or type(function["name"]) is not str or function["name"] not in names
                        or type(function["arguments"]) is not str):
                    raise UnsupportedHistory("Tool call does not match the original tool interface")
                pending.add(call_id)
            if not tool_calls and not (message.get("content") or "").strip():
                raise UnsupportedHistory("Stopped response has no rendered public text")
            assistant_index += 1
        elif role == "tool":
            if (set(message) != {"role", "tool_call_id", "content"}
                    or type(message["content"]) is not str
                    or type(message["tool_call_id"]) is not str
                    or message["tool_call_id"] not in pending):
                raise UnsupportedHistory("Orphan, duplicate or unsupported tool result")
            pending.remove(message["tool_call_id"])
        else:
            raise UnsupportedHistory("Non-original role appears in generated history")
        previous_role = role
    if pending or assistant_index != len(calls):
        raise UnsupportedHistory("Original tool exchanges or generations are incomplete")
    return copy.deepcopy(initial + messages)


def prepare_qwen_interview(original: dict, *, expected_trajectory_sha256: str,
                           assignment_id: str, question: str, seed: int,
                           max_tokens: int = 2048) -> dict:
    """Build one isolated request; native rendering/context admission is still required."""
    _finite_json(original)
    if digest(original) != expected_trajectory_sha256:
        raise ValueError("Original trajectory content changed")
    if type(assignment_id) is not str or not assignment_id or type(question) is not str or not question.strip():
        raise ValueError("An assignment identity and exact nonempty question are required")
    if type(seed) is not int or not 0 <= seed < 2**32 or type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("Invalid interview seed or output reserve")
    for field in ("model", "model_revision"):
        if type(original.get(field)) is not str or not original[field]:
            raise UnsupportedHistory("Missing original model identity")
    if (type(original.get("assignment")) is not dict
            or type(original["assignment"].get("assignment_id")) is not str
            or not original["assignment"]["assignment_id"]):
        raise UnsupportedHistory("Missing original assignment identity")
    generation = original.get("generation")
    if type(generation) is not dict or not set(generation) <= _GENERATION_KEYS:
        raise UnsupportedHistory("Unsupported generation fields")
    rendering = generation.get("chat_template_kwargs")
    if (type(rendering) is not dict or rendering.get("enable_thinking") is not True
            or rendering.get("preserve_thinking") is not True
            or rendering.get("reasoning_effort") not in {"low", "medium", "high", "xhigh"}):
        raise UnsupportedHistory("Original collection must pin effective Qwen reasoning preservation and effort")
    prefix = _prefix(original)
    request = {
        **copy.deepcopy(generation), "model": original["model"],
        "messages": prefix + [{"role": "user", "content": question}],
        "tools": copy.deepcopy(original["tools"]), "tool_choice": "none",
        "parallel_tool_calls": False, "max_tokens": max_tokens, "seed": seed,
    }
    return {
        "schema_version": SCHEMA_VERSION, "status": "prepared_unrendered",
        "assignment_id": assignment_id, "original_assignment_id": original["assignment"]["assignment_id"],
        "original_trajectory_sha256": expected_trajectory_sha256,
        "original_status": original["status"], "model_revision": original["model_revision"],
        "original_prefix_sha256": digest(prefix), "original_prefix_messages": len(prefix),
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "request": request, "request_sha256": digest(request),
        "admission_required": ["native_rendering", "context_with_output_reserve", "finite_resource_allocation"],
    }


def prepare_interview_ledger(original: dict, *, expected_trajectory_sha256: str,
                             assignments: list[dict]) -> list[dict]:
    """Retain every validly assigned fork, including unsupported original histories."""
    identifiers = [row.get("assignment_id") for row in assignments]
    if any(type(value) is not str or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Interview assignment IDs must be distinct nonempty strings")
    rows = []
    for assignment in assignments:
        try:
            row = prepare_qwen_interview(original, expected_trajectory_sha256=expected_trajectory_sha256,
                                         **assignment)
        except UnsupportedHistory as exc:
            row = {
                "schema_version": SCHEMA_VERSION, "status": "unsupported_history",
                "assignment_id": assignment["assignment_id"], "assignment": copy.deepcopy(assignment),
                "original_trajectory_sha256": expected_trajectory_sha256, "reason": str(exc),
            }
        rows.append(row)
    return rows
