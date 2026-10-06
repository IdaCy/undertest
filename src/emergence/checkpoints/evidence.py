import copy
import re
from typing import Any


SCHEMA_VERSION = "development-evidence-v1"
_THINK_TAG = re.compile(r"<(/?)think\s*>", re.IGNORECASE)
_PARTIAL_TAG = re.compile(r"<(/?)t(?:h(?:i(?:n(?:k)?)?)?)?\s*$", re.IGNORECASE)
_TRUNCATED = {"max_tokens", "model_length", "length"}
_KNOWN_NONTRUNCATED = {"stop", "eos", "tool_calls", "function_call", "content_filter"}


def _split_think(text: str, reasoning_prefix_in_prompt: bool = False) -> tuple[str, list[dict[str, Any]]]:
    if reasoning_prefix_in_prompt:
        depth = 1
        closing = None
        for match in _THINK_TAG.finditer(text):
            depth += -1 if match.group(1) else 1
            if depth == 0:
                closing = match
                break
        prefix_segment = {
            "source": "think_tag", "text": text[:closing.start()] if closing else text,
            "start": 0, "end": closing.start() if closing else len(text),
            "status": "complete" if closing else "missing_close", "redacted": False,
            "opening_source": "prompt",
        }
        if closing is None:
            return "", [prefix_segment]
        action, remaining = _split_think(text[closing.end():])
        for segment in remaining:
            segment["start"] += closing.end()
            segment["end"] += closing.end()
        return action, [prefix_segment, *remaining]

    action = []
    segments = []
    depth = 0
    outside_start = 0
    body_start = 0
    cursor = 0
    parts = []

    def retain(start: int, end: int, value: str, status: str) -> None:
        segments.append({
            "source": "think_tag", "text": value,
            "start": start, "end": end, "status": status, "redacted": False,
            "opening_source": "unknown" if status in {"missing_open", "partial_close"} else "generated",
        })

    for match in _THINK_TAG.finditer(text):
        closing = bool(match.group(1))
        if depth:
            parts.append(text[cursor:match.start()])
            depth += -1 if closing else 1
            cursor = match.end()
            if not depth:
                retain(body_start, match.start(), "".join(parts), "complete")
                outside_start = match.end()
                parts = []
        elif closing:
            # A continuation may omit the opening tag supplied in its prompt.
            retain(outside_start, match.start(), text[outside_start:match.start()], "missing_open")
            outside_start = match.end()
        else:
            action.append(text[outside_start:match.start()])
            depth = 1
            body_start = match.end()
            cursor = match.end()

    if depth:
        parts.append(text[cursor:])
        retain(body_start, len(text), "".join(parts), "missing_close")
    else:
        tail = text[outside_start:]
        partial = _PARTIAL_TAG.search(tail)
        if partial:
            start = outside_start + partial.start()
            if partial.group(1):
                retain(outside_start, start, tail[:partial.start()], "partial_close")
            else:
                action.append(tail[:partial.start()])
                retain(start, len(text), "", "partial_open")
        else:
            action.append(tail)
    # Removing rationale must not join fragments into a new action token.
    return "\n".join(part for part in action if part).strip(), segments


def _choice_evidence(
    choice: dict[str, Any], index: int, output_error: Any, reasoning_prefix_in_prompt: bool,
) -> dict[str, Any]:
    message = choice.get("message") or {}
    content = message.get("content")
    typed_segments = []
    text_parts = []
    rendered_parts = []
    unsupported = []
    if isinstance(content, str):
        text_parts.append(content)
        rendered_parts.append(content)
    elif isinstance(content, list):
        for content_index, block in enumerate(content):
            if not isinstance(block, dict):
                unsupported.append(type(block).__name__)
                text_parts.append("\n")
                continue
            kind = block.get("type")
            if kind == "text":
                value = block.get("text")
                if isinstance(value, str):
                    text_parts.append(value)
                    rendered_parts.append(value)
                else:
                    unsupported.append("nonstring_text")
                    text_parts.append("\n")
            elif kind == "reasoning":
                redacted = bool(block.get("redacted", False))
                value = block.get("reasoning")
                visible = value if isinstance(value, str) and not redacted else ""
                typed_segments.append({
                    "source": "typed", "content_index": content_index,
                    "text": visible, "status": "redacted" if redacted else "complete",
                    "redacted": redacted, "summary": block.get("summary"),
                })
                rendered_parts.append(visible)
                text_parts.append("\n")
            else:
                unsupported.append(str(kind))
                text_parts.append("\n")
    elif content is not None:
        unsupported.append(type(content).__name__)

    text_projection = "".join(text_parts)
    prefix_applied = reasoning_prefix_in_prompt and not typed_segments and bool(message)
    if not reasoning_prefix_in_prompt:
        prefix_handling = "none"
    elif typed_segments:
        prefix_handling = "typed_channel"
    else:
        prefix_handling = "prompt_prefix" if prefix_applied else "no_message"
    action_text, inline_segments = _split_think(text_projection, prefix_applied)
    segments = typed_segments + inline_segments
    visible = [segment["text"] for segment in segments if segment["text"].strip()]
    partial = any(segment["status"] not in {"complete", "redacted"} for segment in segments)
    redacted = any(segment["redacted"] for segment in segments)
    if visible:
        reasoning_status = "partial" if partial or redacted else "available"
    elif redacted:
        reasoning_status = "redacted"
    elif partial:
        reasoning_status = "partial"
    else:
        reasoning_status = "empty" if segments else "unavailable"

    stop_reason = choice.get("stop_reason") or "unknown"
    is_truncated = (
        True if stop_reason in _TRUNCATED
        else False if stop_reason in _KNOWN_NONTRUNCATED and not output_error
        else None
    )
    if output_error or stop_reason not in _TRUNCATED | _KNOWN_NONTRUNCATED:
        completion_status = "unknown"
    elif is_truncated or partial or stop_reason in {"tool_calls", "function_call", "content_filter"}:
        completion_status = "incomplete"
    else:
        completion_status = "complete"
    if visible and is_truncated:
        reasoning_status = "partial"
    if partial or redacted or is_truncated:
        reasoning_complete = False
    elif not visible or completion_status != "complete":
        reasoning_complete = None
    else:
        reasoning_complete = True

    return {
        "choice_index": index,
        "stop_reason": stop_reason,
        "stop_details": copy.deepcopy(choice.get("stop_details")),
        "is_truncated": is_truncated,
        "completion_status": completion_status,
        "raw_content": copy.deepcopy(content),
        "raw_rendered_text": "".join(rendered_parts),
        "raw_rendered_text_is_projection": not isinstance(content, str),
        "text_projection": text_projection,
        "action_text": action_text,
        "reasoning_text": "\n\n".join(visible).strip(),
        "reasoning_status": reasoning_status,
        "reasoning_available": bool(visible),
        "reasoning_complete": reasoning_complete,
        "reasoning_has_partial_tags": partial,
        "reasoning_has_redactions": redacted,
        "reasoning_sources": list(dict.fromkeys(segment["source"] for segment in segments)),
        "reasoning_segments": segments,
        "reasoning_prefix_in_prompt": reasoning_prefix_in_prompt,
        "reasoning_prefix_applied": prefix_applied,
        "reasoning_prefix_handling": prefix_handling,
        "unsupported_content_types": unsupported,
    }


def extract_evidence(output_dict: dict, reasoning_prefix_in_prompt: bool = False) -> dict:
    """Extract transport evidence without assigning awareness or action scores.

    Raw content is retained exactly. Rendered text is a projection of readable text/reasoning blocks; redacted payloads remain only in raw_output/raw_content. Inline spans index text_projection, and top-level action fields always describe choice zero.

    Enable reasoning_prefix_in_prompt only when a verified prompt already ends inside a think block. Typed reasoning channels take precedence over this raw-text continuation assumption.
    """
    if not isinstance(output_dict, dict):
        raise TypeError("output_dict must be a ModelOutput JSON dictionary")
    if not isinstance(reasoning_prefix_in_prompt, bool):
        raise TypeError("reasoning_prefix_in_prompt must be a boolean")
    raw_choices = output_dict.get("choices") or []
    if not isinstance(raw_choices, list) or any(not isinstance(choice, dict) for choice in raw_choices):
        raise TypeError("choices must be a list of dictionaries")
    output_error = output_dict.get("error")
    choices = [
        _choice_evidence(choice, index, output_error, reasoning_prefix_in_prompt)
        for index, choice in enumerate(raw_choices)
    ]
    primary = choices[0] if choices else _choice_evidence({}, 0, output_error, reasoning_prefix_in_prompt)
    return {
        "schema_version": SCHEMA_VERSION,
        "model": output_dict.get("model"),
        "selected_choice": 0 if choices else None,
        "choice_count": len(choices),
        **{key: copy.deepcopy(value) for key, value in primary.items() if key != "choice_index"},
        "output_error": copy.deepcopy(output_error),
        "choices": choices,
        "raw_output": copy.deepcopy(output_dict),
    }
