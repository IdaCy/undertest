import hashlib
import json
import re
from collections import Counter


PAYLOAD_VERSION = "effects-canonical-construct-payload-v1"
CHANNELS = ("text", "reasoning", "reasoning_summary")
AVAILABILITIES = ("readable", "empty", "redacted", "unavailable_field", "unresolved_attachment")
COVERAGE = {"available": "complete_recorded_surface", "partial": "partial_or_unestablished", "unavailable": "unavailable"}
CHANNEL_STATUS = {"returned_segments", "not_returned_or_not_recorded", "unavailable_or_incomplete_transcript"}
REASONING_STATUS = {
    "unavailable_transcript", "unavailable_incomplete_transcript", "not_returned_or_not_recorded", "readable_returned_blocks",
    "partially_readable_returned_blocks", "redacted_returned_blocks", "returned_blocks_without_readable_reasoning",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _identity(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _integer(value):
    return type(value) is int and value >= 0


def _segment(value):
    _require(type(value) is dict and _identity(value.get("segment_id")), "Invalid canonical segment identity")
    kind = value.get("kind")
    message, content = value.get("message_index"), value.get("content_index")
    _require(kind in CHANNELS and _integer(message) and (content is None or _integer(content)), "Invalid canonical segment coordinates")
    _require(content is not None or kind == "text", "Typed canonical segment needs a content-block index")
    field = "content" if content is None else {"text": "text", "reasoning": "reasoning", "reasoning_summary": "summary"}[kind]
    pointer = f"/{message}/content" + (f"/{content}/{field}" if content is not None else "")
    _require(value.get("field") == field and value.get("json_pointer") == pointer, "Noncanonical segment field or pointer")
    availability, text = value.get("availability"), value.get("value")
    _require(availability in AVAILABILITIES, "Unknown canonical availability")
    if availability == "readable":
        _require(type(text) is str and bool(text) and not text.startswith("attachment://"), "Readable canonical text must be resolved")
    elif availability == "empty":
        _require(type(text) is str and text == "", "Empty canonical text must be empty")
    else:
        _require(text is None, "Unavailable canonical text cannot carry a value")
    return {"segment_id": value["segment_id"], "kind": kind, "message_index": message,
            "content_index": content, "field": field, "json_pointer": pointer,
            "availability": availability, "value": text}


def canonical_construct_payload(item):
    _require(type(item) is dict and _identity(item.get("annotation_id")), "Invalid canonical episode identity")
    status = item.get("evidence_status")
    _require(type(status) is str and status in COVERAGE, "Unknown canonical evidence status")
    _require(item.get("provided_surface_coverage") == COVERAGE[status], "Canonical coverage/status disagreement")
    _require(item.get("episode_wide_absence") == "not_established", "Episode-wide absence cannot be promoted")
    _require(type(item.get("segments")) is list and type(item.get("channels")) is dict, "Canonical segment/channel inventory required")
    segments = [_segment(value) for value in item["segments"]]
    _require(len({value["segment_id"] for value in segments}) == len(segments), "Duplicate canonical segment identity")
    _require(len({(value["json_pointer"], value["kind"]) for value in segments}) == len(segments), "Duplicate canonical segment coordinates")
    _require(status != "unavailable" or not segments, "Unavailable canonical item cannot contain selected segments")
    _require(status != "available" or all(value["availability"] in ("readable", "empty") for value in segments), "Available canonical item has unresolved segments")
    channels = {}
    for kind in CHANNELS:
        source = item["channels"].get(kind)
        _require(type(source) is dict, "Missing canonical channel")
        selected = [value for value in segments if value["kind"] == kind]
        count = Counter(value["availability"] for value in selected)
        expected = {state: count[state] for state in AVAILABILITIES}
        declared = source.get("availability_counts")
        _require(type(declared) is dict and all(_integer(declared.get(state)) and declared[state] == expected[state] for state in AVAILABILITIES), "Canonical channel counts disagree")
        _require(type(source.get("returned_segment_count")) is int and source["returned_segment_count"] == len(selected), "Canonical channel total disagrees")
        channel_status = source.get("status")
        _require(type(channel_status) is str and channel_status in CHANNEL_STATUS, "Unknown canonical channel status")
        _require((channel_status == "returned_segments") == bool(selected), "Canonical channel status disagrees with inventory")
        if not selected and status != "partial":
            expected_status = "not_returned_or_not_recorded" if status == "available" else "unavailable_or_incomplete_transcript"
            _require(channel_status == expected_status, "Empty canonical channel status disagrees with episode availability")
        _require(source.get("full_channel_capture") == "not_established", "Full channel capture cannot be promoted")
        channels[kind] = {"returned_segment_count": len(selected), "availability_counts": expected,
                          "status": channel_status, "full_channel_capture": "not_established"}
        if kind == "reasoning":
            reasoning_status = source.get("index_availability")
            _require(type(reasoning_status) is str and reasoning_status in REASONING_STATUS, "Unknown indexed reasoning availability")
            if selected:
                expected_reasoning = ("readable_returned_blocks" if count["readable"] == len(selected)
                                      else "partially_readable_returned_blocks" if count["readable"]
                                      else "redacted_returned_blocks" if count["redacted"] == len(selected)
                                      else "returned_blocks_without_readable_reasoning")
                _require(reasoning_status == expected_reasoning, "Indexed reasoning availability disagrees with segments")
            else:
                expected_reasoning = ({"unavailable_transcript"} if status == "unavailable"
                                      else {"not_returned_or_not_recorded"} if status == "available"
                                      else {"not_returned_or_not_recorded", "unavailable_incomplete_transcript"})
                _require(reasoning_status in expected_reasoning, "Indexed reasoning availability disagrees with absent segments")
            channels[kind]["index_availability"] = reasoning_status
    return {"annotation_id": item["annotation_id"], "evidence_status": status, "segments": segments,
            "channels": channels, "provided_surface_coverage": COVERAGE[status], "episode_wide_absence": "not_established"}


def render_construct_request(item, instructions):
    _require(type(instructions) is str and bool(instructions.strip()), "Exact nonempty instructions required")
    payload = canonical_construct_payload(item)
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps({"data_class": "untrusted_quoted_actor_text", "construct_item": payload}, ensure_ascii=False, sort_keys=True)}]
    return {"payload_version": PAYLOAD_VERSION, "annotation_id": payload["annotation_id"], "messages": messages,
            "instruction_utf8_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "messages_utf8_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "context_preflight": "not_performed", "actual_model_calls": 0}
