import hashlib
import json
import re
from collections import Counter

from signals.reasoning.context_annotations.payload import AVAILABILITIES
from signals.reasoning.context_annotations.payload import CHANNELS
from signals.reasoning.context_annotations.payload import COVERAGE
from signals.reasoning.context_annotations.payload import _identity
from signals.reasoning.context_annotations.payload import _integer
from signals.reasoning.context_annotations.payload import _require
from signals.reasoning.context_annotations.payload import _segment
from signals.reasoning.context_annotations.payload import canonical_construct_payload


PAYLOAD_VERSION = "effects-combined-construct-payload-v1"
COORDINATE_NAMESPACE = "canonical-transcript-and-supplementary-model-event-v1"
CANONICAL = "canonical_transcript"
SUPPLEMENTARY = "supplementary_model_event"


def _content_pointer(content, kind):
    _require(content is not None or kind == "text", "Scalar content must be text")
    field = "content" if content is None else {"text": "text", "reasoning": "reasoning", "reasoning_summary": "summary"}[kind]
    return field, "/content" + (f"/{content}/{field}" if content is not None else "")


def _combined_segment(value):
    _require(type(value) is dict, "Combined segment must be an object")
    namespace = value.get("source_namespace")
    if namespace == CANONICAL:
        result = _segment(value)
    elif namespace == SUPPLEMENTARY:
        identity, kind = value.get("segment_id"), value.get("kind")
        content = value.get("content_index")
        _require(_identity(identity) and kind in CHANNELS, "Invalid supplementary identity or channel")
        _require(content is None or _integer(content), "Invalid supplementary content index")
        for name in ("event_index", "model_event_index", "choice_index"):
            _require(_integer(value.get(name)), "Invalid supplementary " + name)
        field, pointer = _content_pointer(content, kind)
        pointer = f"/choices/{value['choice_index']}/message" + pointer
        _require(value.get("field") == field and value.get("json_pointer") == pointer,
                 "Noncanonical supplementary field or pointer")
        availability, text = value.get("availability"), value.get("value")
        _require(availability in AVAILABILITIES, "Unknown supplementary availability")
        if availability == "readable":
            _require(type(text) is str and bool(text) and not text.startswith("attachment://"),
                     "Readable supplementary text must be resolved")
        elif availability == "empty":
            _require(text == "" and type(text) is str, "Empty supplementary text must be empty")
        else:
            _require(text is None, "Unavailable supplementary text cannot carry a value")
        result = {"segment_id": identity, "kind": kind, "content_index": content, "field": field,
                  "json_pointer": pointer, "availability": availability, "value": text,
                  **{name: value[name] for name in ("event_index", "model_event_index", "choice_index")}}
    else:
        raise ValueError("Unknown combined source namespace")
    position = value.get("surface_block")
    _require(type(position) is list and len(position) == 3 and all(_integer(x) for x in position),
             "Explicit nonnegative model/choice/content block coordinate required")
    _require(position[2] == (0 if result["content_index"] is None else result["content_index"]),
             "Surface coordinate changes native content-block order")
    if namespace == SUPPLEMENTARY:
        _require(position[:2] == [result["model_event_index"], result["choice_index"]],
                 "Supplementary model/choice position disagrees")
    return {**result, "source_namespace": namespace, "surface_block": list(position)}


def _positions(segments):
    native_coordinates, identities, blocks, messages, model_messages = set(), set(), {}, {}, {}
    event_models, model_events = {}, {}
    previous = None
    for segment in segments:
        namespace, position = segment["source_namespace"], tuple(segment["surface_block"])
        if namespace == CANONICAL:
            message = (namespace, segment["message_index"])
        else:
            message = (namespace, segment["event_index"], segment["choice_index"])
            event, model = segment["event_index"], segment["model_event_index"]
            _require(event not in event_models or event_models[event] == model,
                     "One native event has contradictory model ordinals")
            _require(model not in model_events or model_events[model] == event,
                     "Different native events share a model ordinal")
            event_models[event], model_events[model] = model, event
        coordinate = (*message, segment["json_pointer"], segment["kind"])
        block = (*message, segment["content_index"])
        _require(segment["segment_id"] not in identities and coordinate not in native_coordinates,
                 "Duplicate segment identity or native coordinate")
        _require(previous is None or previous <= position, "Combined blocks are out of order")
        _require(position not in blocks or blocks[position] == block,
                 "Different native blocks share a surface coordinate")
        _require(message not in messages or messages[message] == position[:2],
                 "One native message has contradictory model positions")
        _require(position[:2] not in model_messages or model_messages[position[:2]] == message,
                 "Different native messages share a model/choice position")
        identities.add(segment["segment_id"])
        native_coordinates.add(coordinate)
        blocks[position], messages[message], model_messages[position[:2]] = block, position[:2], message
        previous = position
    for namespace in (CANONICAL, SUPPLEMENTARY):
        positions = [position for message, position in sorted(messages.items()) if message[0] == namespace]
        _require(all(a < b for a, b in zip(positions, positions[1:])),
                 "Surface order contradicts native message/event order")
    models = [model for _, model in sorted(event_models.items())]
    _require(all(a < b for a, b in zip(models, models[1:])),
             "Model ordinal order contradicts native event order")


def _channels(segments, status):
    result = {}
    for kind in CHANNELS:
        selected = [s for s in segments if s["kind"] == kind]
        count = Counter(s["availability"] for s in selected)
        row = {"returned_segment_count": len(selected),
               "availability_counts": {state: count[state] for state in AVAILABILITIES},
               "status": "returned_segments" if selected else "not_returned_or_not_recorded" if status == "available"
               else "unavailable_or_incomplete_transcript", "full_channel_capture": "not_established"}
        if kind == "reasoning":
            row["index_availability"] = (
                "readable_returned_blocks" if selected and count["readable"] == len(selected)
                else "partially_readable_returned_blocks" if count["readable"]
                else "redacted_returned_blocks" if selected and count["redacted"] == len(selected)
                else "returned_blocks_without_readable_reasoning" if selected
                else "not_returned_or_not_recorded" if status == "available"
                else "unavailable_transcript" if status == "unavailable" else "unavailable_incomplete_transcript")
        result[kind] = row
    return result


def combined_construct_payload(item):
    _require(type(item) is dict and _identity(item.get("annotation_id")), "Invalid combined episode identity")
    _require(item.get("payload_version") == PAYLOAD_VERSION
             and item.get("coordinate_namespace") == COORDINATE_NAMESPACE, "Unknown combined surface version")
    status = item.get("evidence_status")
    _require(type(status) is str and status in COVERAGE, "Invalid combined evidence status")
    _require(item.get("provided_surface_coverage") == COVERAGE[status]
             and item.get("episode_wide_absence") == "not_established", "Combined coverage cannot be promoted")
    _require(type(item.get("segments")) is list, "Combined segment inventory required")
    segments = [_combined_segment(value) for value in item["segments"]]
    _positions(segments)
    _require(status != "unavailable" or not segments, "Unavailable combined item contains segments")
    _require(status != "available" or all(s["availability"] in ("readable", "empty") for s in segments),
             "Available combined item contains unresolved text")
    channels = _channels(segments, status)
    _require(type(item.get("channels")) is dict, "Combined channels required")
    for kind, expected in channels.items():
        declared = item["channels"].get(kind)
        _require(type(declared) is dict and _integer(declared.get("returned_segment_count"))
                 and type(declared.get("availability_counts")) is dict, "Malformed combined channel counts")
        counts = declared["availability_counts"]
        _require(all(_integer(counts.get(state)) for state in AVAILABILITIES), "Noninteger combined availability count")
        selected = {key: declared.get(key) for key in expected}
        selected["availability_counts"] = {state: counts[state] for state in AVAILABILITIES}
        _require(selected == expected, "Combined channel counts or capture status disagree")
    return {"payload_version": PAYLOAD_VERSION, "coordinate_namespace": COORDINATE_NAMESPACE,
            "annotation_id": item["annotation_id"], "evidence_status": status, "segments": segments,
            "channels": channels, "provided_surface_coverage": COVERAGE[status],
            "episode_wide_absence": "not_established"}


def construct_payload(item):
    _require(type(item) is dict, "Construct item must be an object")
    if "payload_version" in item or "coordinate_namespace" in item:
        return combined_construct_payload(item)
    return canonical_construct_payload(item)


def build_combined_construct_payload(canonical_item, supplementary_item, order_item):
    canonical = canonical_construct_payload(canonical_item)
    annotation_id, status = canonical["annotation_id"], canonical["evidence_status"]
    _require(type(supplementary_item) is dict and type(order_item) is dict, "Owner crosswalk and supplement required")
    for value in (supplementary_item, order_item):
        _require(value.get("annotation_id") == annotation_id and value.get("canonical_evidence_status") == status,
                 "Owner crosswalk/supplement identity or evidence status differs")
    _require(supplementary_item.get("canonical_report_unchanged") is True
             and supplementary_item.get("episode_wide_absence") == "not_established"
             and supplementary_item.get("report_completeness") == "not_established", "Supplement cannot upgrade the report")
    supplied = supplementary_item.get("segments")
    _require(type(supplied) is list, "Explicit supplementary inventory required")
    _require(not supplied or supplementary_item.get("selection_status") == "unique_verified_mapping",
             "Supplementary mapping is not unique")
    originals = {s["segment_id"]: s for s in canonical["segments"]}
    supplements = {}
    for value in supplied:
        _require(type(value) is dict and _identity(value.get("segment_id")), "Invalid supplementary identity")
        identity = value["segment_id"]
        _require(identity not in originals and identity not in supplements, "Duplicate supplementary identity")
        supplements[identity] = value
    ordered = order_item.get("ordered_sources")
    _require(type(ordered) is list and len(ordered) == len(originals) + len(supplements), "Incomplete owner order inventory")
    for name, expected in (("canonical_segment_count", len(originals)), ("supplementary_segment_count", len(supplements))):
        _require(type(order_item.get(name)) is int and order_item[name] == expected, "Owner order count disagrees")
    result, seen, native_events = [], set(), {}
    previous = None
    for row in ordered:
        _require(type(row) is dict and _identity(row.get("segment_id")) and row["segment_id"] not in seen,
                 "Malformed or duplicate owner crosswalk row")
        identity, order = row.get("segment_id"), row.get("order")
        _require(type(order) is list and len(order) == 3 and all(_integer(x) for x in order), "Malformed owner order")
        _require(previous is None or previous <= tuple(order), "Nonmonotone owner crosswalk")
        previous = tuple(order)
        event = row.get("raw_event_index")
        _require(_integer(event), "Native model event identity required")
        _require(order[0] not in native_events or native_events[order[0]] == event, "Model ordinal has contradictory native events")
        native_events[order[0]] = event
        if identity in originals:
            source = originals[identity]
            _require(row.get("source_surface") == "canonical" and row.get("native_pointer") == source["json_pointer"]
                     and type(row.get("canonical_message_index")) is int
                     and row.get("canonical_message_index") == source["message_index"], "Canonical crosswalk changed native coordinates")
            segment = {**source, "source_namespace": CANONICAL}
        else:
            _require(identity in supplements and row.get("source_surface") == SUPPLEMENTARY
                     and row.get("canonical_message_index") is None, "Unknown supplementary crosswalk row")
            source = supplements[identity]
            _require(source.get("kind") in CHANNELS, "Unknown supplementary source channel")
            _require(row.get("native_pointer") == source.get("inspect_view_pointer"), "Supplementary owner pointer differs")
            match = re.fullmatch(r"/samples/[0-9]+/events/([0-9]+)/output(/choices/([0-9]+)/message(/content(?:/([0-9]+)/(text|reasoning|summary))?))", row["native_pointer"])
            _require(match is not None and int(match[1]) == event and int(match[3]) == order[1]
                     and match[2] == source.get("output_pointer") and match[4] == source.get("message_pointer"),
                     "Supplementary native pointer/index binding differs")
            content = int(match[5]) if match[5] is not None else None
            field, _ = _content_pointer(content, source["kind"])
            segment = {"segment_id": identity, "kind": source["kind"], "content_index": content, "field": field,
                       "json_pointer": source["output_pointer"], "availability": source["availability"],
                       "value": source.get("value"), "source_namespace": SUPPLEMENTARY,
                       "event_index": event, "model_event_index": order[0], "choice_index": order[1]}
            if type(segment["value"]) is str:
                raw = segment["value"].encode("utf-8")
                _require(type(source.get("characters")) is int and source["characters"] == len(segment["value"])
                         and type(source.get("utf8_bytes")) is int and source["utf8_bytes"] == len(raw)
                         and source.get("utf8_sha256") == hashlib.sha256(raw).hexdigest(), "Supplementary text digest or size differs")
        _require(row.get("kind") == segment["kind"], "Crosswalk channel differs")
        segment["surface_block"] = [order[0], order[1], 0 if segment["content_index"] is None else segment["content_index"]]
        result.append(_combined_segment(segment))
        seen.add(identity)
    events = [event for _, event in sorted(native_events.items())]
    _require(all(a < b for a, b in zip(events, events[1:])), "Model order contradicts native event order")
    _require(seen == originals.keys() | supplements.keys(), "Missing source segments")
    item = {"payload_version": PAYLOAD_VERSION, "coordinate_namespace": COORDINATE_NAMESPACE,
            "annotation_id": annotation_id, "evidence_status": status, "segments": result,
            "channels": _channels(result, status), "provided_surface_coverage": COVERAGE[status],
            "episode_wide_absence": "not_established"}
    return combined_construct_payload(item)


def render_combined_construct_request(item, instructions):
    _require(type(instructions) is str and bool(instructions.strip()), "Exact nonempty instructions required")
    payload = combined_construct_payload(item)
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps({"data_class": "untrusted_quoted_actor_text", "construct_item": payload}, ensure_ascii=False, sort_keys=True)}]
    return {"payload_version": PAYLOAD_VERSION, "annotation_id": payload["annotation_id"], "messages": messages,
            "instruction_utf8_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "messages_utf8_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "context_preflight": "not_performed", "actual_model_calls": 0}
