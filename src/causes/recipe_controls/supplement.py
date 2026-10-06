import copy
import hashlib
import json


VERSION = "effects-model-event-supplement-v1"
CHANNELS = ("text", "reasoning", "reasoning_summary")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def monotone_match(canonical_messages, event_messages, *, order_verified):
    left = [canonical_bytes(message) for message in canonical_messages]
    right = [canonical_bytes(message) for message in event_messages]
    equal = [[j for j, candidate in enumerate(right) if message == candidate] for message in left]
    result = {"equal_candidates": equal, "feasible_candidates": None,
              "mapping_count_capped_at_two": None, "mapping": None, "status": "unknown_order"}
    if order_verified is not True:
        return result
    n, m = len(left), len(right)
    prefix = [[0] * (m + 1) for _ in range(n + 1)]
    prefix[0] = [1] * (m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            prefix[i][j] = min(2, prefix[i][j - 1] + (prefix[i - 1][j - 1] if left[i - 1] == right[j - 1] else 0))
    suffix = [[0] * (m + 1) for _ in range(n + 1)]
    suffix[n] = [1] * (m + 1)
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            suffix[i][j] = min(2, suffix[i][j + 1] + (suffix[i + 1][j + 1] if left[i] == right[j] else 0))
    count = prefix[n][m]
    feasible = [[j for j in equal[i] if prefix[i][j] and suffix[i + 1][j + 1]] for i in range(n)]
    result.update(mapping_count_capped_at_two=count, feasible_candidates=feasible,
                  status="unique" if count == 1 else "disagreement" if count == 0 else "ambiguous")
    if count == 1:
        result["mapping"] = [candidates[0] for candidates in feasible]
    return result


def message_segments(message):
    segments, unsupported = [], []

    def add(pointer, kind, value, redacted=False):
        state = ("redacted" if redacted else "unavailable_field" if type(value) is not str
                 else "unresolved_attachment" if value.startswith("attachment://")
                 else "readable" if value else "empty")
        selected = value if state in ("readable", "empty") else None
        segments.append({"message_pointer": pointer, "kind": kind, "availability": state,
                         "value": selected, "characters": len(selected) if selected is not None else None,
                         "utf8_bytes": len(selected.encode("utf-8")) if selected is not None else None,
                         "utf8_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest() if selected is not None else None})

    if type(message) is not dict or message.get("role") != "assistant":
        return segments, [{"message_pointer": "", "reason": "not_an_assistant_message"}]
    content = message.get("content")
    if type(content) is str:
        add("/content", "text", content)
    elif type(content) is list:
        for i, block in enumerate(content):
            pointer = f"/content/{i}"
            if type(block) is dict and block.get("type") == "text":
                add(pointer + "/text", "text", block.get("text"))
            elif type(block) is dict and block.get("type") == "reasoning":
                add(pointer + "/reasoning", "reasoning", block.get("reasoning"), block.get("redacted") is True)
                if block.get("summary") is not None:
                    add(pointer + "/summary", "reasoning_summary", block["summary"])
            else:
                unsupported.append({"message_pointer": pointer, "reason": "unsupported_content_block", "value": copy.deepcopy(block)})
    else:
        unsupported.append({"message_pointer": "/content", "reason": "invalid_content", "value": copy.deepcopy(content)})
    return segments, unsupported


def inventory_episode(*, raw_sample, canonical_messages, canonical_item, raw_log_reference,
                      sample_index, first_attempt_binding_verified, source_order_verified):
    issues = []
    annotation_id = canonical_item["annotation_id"]
    if canonical_item.get("evidence_status") not in ("available", "partial"):
        issues.append("unavailable_or_unknown_canonical_evidence")
    sample_uuid = raw_sample.get("uuid")
    if type(sample_uuid) is not str or not sample_uuid:
        issues.append("missing_sample_uuid")
    if first_attempt_binding_verified is not True:
        issues.append("unverified_first_attempt_binding")
    if source_order_verified is not True:
        issues.append("unknown_source_order")
    if type(sample_index) is not int or sample_index < 0:
        raise ValueError("Invalid sample index")
    if type(raw_log_reference.get("sha256")) is not str or len(raw_log_reference["sha256"]) != 64:
        raise ValueError("Missing raw log byte identity")
    events, eligible, seen_uuids = [], [], set()
    raw_events = raw_sample.get("events")
    if type(raw_events) is not list:
        issues.append("missing_event_inventory")
        raw_events = []
    for event_index, event in enumerate(raw_events):
        if type(event) is not dict:
            issues.append("malformed_event")
            continue
        event_kind = event.get("event")
        if type(event_kind) is not str or not event_kind.strip():
            issues.append("malformed_event_discriminator")
            continue
        if event_kind != "model":
            continue
        ordinal = len(events)
        uuid = event.get("uuid")
        if type(uuid) is not str or not uuid:
            issues.append("missing_event_uuid")
        elif uuid in seen_uuids:
            issues.append("duplicate_event_uuid")
        seen_uuids.add(uuid if type(uuid) is str else None)
        output = event.get("output")
        event_pointer = f"/samples/{sample_index}/events/{event_index}"
        entry = {"event_index": event_index, "model_event_ordinal": ordinal, "event_uuid": uuid,
                 "inspect_view_pointer": event_pointer, "event_canonical_sha256": digest(event),
                 "output_canonical_sha256": digest(output), "output": copy.deepcopy(output),
                 "event_error": copy.deepcopy(event.get("error")), "pending": event.get("pending"),
                 "retries": copy.deepcopy(event.get("retries")), "choices": [],
                 "disposition": "retained_selection_unknown"}
        events.append(entry)
        if event.get("error") is not None or (type(output) is dict and output.get("error") is not None):
            issues.append("recorded_model_error")
        if event.get("pending") is True:
            issues.append("pending_model_event")
        choices = output.get("choices") if type(output) is dict else None
        if type(choices) is not list:
            issues.append("malformed_output_choices")
            continue
        if len(choices) != 1:
            issues.append("nonunique_or_missing_output_choice")
        for choice_index, choice in enumerate(choices):
            message = choice.get("message") if type(choice) is dict else None
            pointer = f"/output/choices/{choice_index}/message"
            valid = type(message) is dict and message.get("role") == "assistant"
            if not valid:
                issues.append("invalid_output_message")
            segments, unsupported = message_segments(message)
            choice_entry = {"choice_index": choice_index, "message_id": message.get("id") if valid else None,
                            "message_canonical_sha256": digest(message), "segments": segments,
                            "unsupported": unsupported, "canonical_message_index": None,
                            "canonical_segment_bindings": []}
            entry["choices"].append(choice_entry)
            for segment in segments:
                segment["output_pointer"] = pointer.removeprefix("/output") + segment["message_pointer"]
                segment["inspect_view_pointer"] = event_pointer + pointer + segment["message_pointer"]
                segment["segment_id"] = digest([VERSION, annotation_id, raw_log_reference["sha256"], sample_uuid,
                                                uuid, ordinal, choice_index, segment["message_pointer"], segment["kind"]])
            if len(choices) == 1 and valid:
                eligible.append({"event_ordinal": ordinal, "message": message})
    canonical_assistants = []
    if type(canonical_messages) is not list:
        issues.append("invalid_canonical_messages")
        canonical_messages = []
    for index, message in enumerate(canonical_messages):
        if type(message) is not dict:
            issues.append("malformed_canonical_message")
        elif message.get("role") == "assistant":
            canonical_assistants.append((index, message))
    if not canonical_assistants:
        issues.append("missing_canonical_assistant")
    matching = monotone_match([message for _, message in canonical_assistants],
                              [entry["message"] for entry in eligible], order_verified=source_order_verified)
    matching["canonical_message_indices"] = [index for index, _ in canonical_assistants]
    matching["eligible_event_ordinals"] = [entry["event_ordinal"] for entry in eligible]
    if matching["status"] != "unique":
        issues.append("canonical_event_mapping_" + matching["status"])
    canonical_segments = canonical_item.get("segments", [])
    segment_lookup = {(segment["json_pointer"], segment["kind"]): segment for segment in canonical_segments}
    if len(segment_lookup) != len(canonical_segments):
        issues.append("duplicate_canonical_segment_pointer")
    bound_ids = []
    if matching["mapping"] is not None:
        for assistant_ordinal, eligible_index in enumerate(matching["mapping"]):
            message_index = canonical_assistants[assistant_ordinal][0]
            event = events[eligible[eligible_index]["event_ordinal"]]
            choice = event["choices"][0]
            choice["canonical_message_index"] = message_index
            event["disposition"] = "canonical_message_matched"
            for segment in choice["segments"]:
                pointer = f"/{message_index}" + segment["message_pointer"]
                canonical = segment_lookup.get((pointer, segment["kind"]))
                agrees = canonical is not None and all(canonical.get(k) == segment[k] for k in ("availability", "value"))
                binding = {"raw_segment_id": segment["segment_id"], "canonical_json_pointer": pointer,
                           "canonical_segment_id": canonical.get("segment_id") if canonical else None,
                           "exact_value_and_availability_match": agrees}
                choice["canonical_segment_bindings"].append(binding)
                if agrees:
                    bound_ids.append(canonical["segment_id"])
                else:
                    issues.append("canonical_segment_binding_disagreement")
        if len(bound_ids) != len(canonical_segments) or set(bound_ids) != {s["segment_id"] for s in canonical_segments}:
            issues.append("incomplete_canonical_segment_binding")
    admitted = []
    if not issues:
        for event in events:
            if event["disposition"] == "canonical_message_matched":
                continue
            event["disposition"] = "supplementary_returned_source"
            for choice in event["choices"]:
                for segment in choice["segments"]:
                    admitted.append(copy.deepcopy(segment))
    else:
        for event in events:
            event["disposition"] = "retained_selection_unknown"
    channels = {}
    for kind in CHANNELS:
        selected = [segment for segment in admitted if segment["kind"] == kind]
        channels[kind] = {"returned_segment_count": len(selected),
                          "availability_counts": {state: sum(segment["availability"] == state for segment in selected)
                                                  for state in ("readable", "empty", "redacted", "unavailable_field", "unresolved_attachment")},
                          "full_channel_capture": "not_established"}
    limits = {key: copy.deepcopy(raw_sample.get(key)) for key in ("limit", "error", "error_retries", "token_limit", "token_limit_type", "token_limit_usage", "message_limit", "time_limit")}
    return {"version": VERSION, "annotation_id": annotation_id, "assignment_id": str(raw_sample.get("id")),
            "sample_uuid": sample_uuid, "sample_index": sample_index, "raw_log_reference": copy.deepcopy(raw_log_reference),
            "selection_issues": sorted(set(issues)), "matching": matching, "events": events,
            "preserved_sample_limits_and_errors": limits,
            "projection": {"annotation_id": annotation_id, "canonical_evidence_status": canonical_item.get("evidence_status"),
                           "selection_status": "unknown" if issues else "unique_verified_mapping",
                           "surface": "supplementary_saved_model_event_outputs_only", "segments": admitted,
                           "channels": channels, "episode_wide_absence": "not_established",
                           "report_completeness": "not_established", "canonical_report_unchanged": True}}
