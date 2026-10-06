from collections import Counter

from signals.reasoning.context_annotations.combined import COORDINATE_NAMESPACE
from signals.reasoning.context_annotations.combined import PAYLOAD_VERSION
from signals.reasoning.context_annotations.combined import build_combined_construct_payload
from signals.reasoning.context_annotations.combined import render_combined_construct_request
from signals.reasoning.context_annotations.payload import _identity
from signals.reasoning.context_annotations.payload import _integer
from signals.reasoning.context_annotations.payload import _require
from causes.recipe_controls.supplement import VERSION as SUPPLEMENT_VERSION
from causes.recipe_controls.supplement import digest
from causes.recipe_controls.supplement import message_segments


def _rows(document, key):
    _require(type(document) is dict and type(document.get(key)) is list, "Missing source rows")
    rows = document[key]
    _require(all(type(row) is dict and _identity(row.get("annotation_id")) for row in rows),
             "Malformed source episode identity")
    identities = [row["annotation_id"] for row in rows]
    _require(len(set(identities)) == len(identities), "Duplicate source episode")
    return rows, identities


def _reference(reference, expected_source, pointer, value):
    _require(type(reference) is dict and reference.get("source") == expected_source
             and reference.get("json_pointer") == pointer
             and reference.get("canonical_sha256") == digest(value), "Owner row source binding differs")


def _bound_order(canonical, supplement, episode):
    _require(episode.get("version") == SUPPLEMENT_VERSION and episode.get("selection_issues") == [],
             "Supplementary source is not admitted")
    matching = episode.get("matching")
    _require(type(matching) is dict and matching.get("status") == "unique"
             and matching.get("mapping_count_capped_at_two") == 1,
             "Supplementary event matching is not unique")
    _require(supplement.get("selection_status") == "unique_verified_mapping", "Supplementary projection is not admitted")
    sample = episode.get("sample_index")
    _require(_integer(sample), "Invalid owner sample coordinate")
    events = episode.get("events")
    _require(type(events) is list, "Missing owner model-event inventory")
    canonical_segments = {s["segment_id"]: s for s in canonical["segments"]}
    projected = {s["segment_id"]: s for s in supplement["segments"]}
    _require(len(canonical_segments) == len(canonical["segments"])
             and len(projected) == len(supplement["segments"]), "Duplicate source segment")
    ordered, canonical_ids, supplementary_ids, matched_messages, matched_ordinals = [], [], [], [], []
    previous_event = -1
    for ordinal, event in enumerate(events):
        index = event.get("event_index")
        _require(_integer(index) and index > previous_event
                 and type(event.get("model_event_ordinal")) is int and event["model_event_ordinal"] == ordinal,
                 "Owner event order differs")
        previous_event = index
        prefix = f"/samples/{sample}/events/{index}"
        _require(event.get("inspect_view_pointer") == prefix, "Owner event pointer differs")
        output = event.get("output")
        _require(type(output) is dict and event.get("output_canonical_sha256") == digest(output),
                 "Owner model output digest differs")
        _require(event.get("event_error") is None and event.get("pending") is not True
                 and output.get("error") is None, "Unresolved model output")
        choices, raw_choices = event.get("choices"), output.get("choices")
        _require(type(choices) is list and len(choices) == 1
                 and type(raw_choices) is list and len(raw_choices) == 1, "Nonunique model choice")
        choice = choices[0]
        message = raw_choices[0].get("message")
        _require(type(choice.get("choice_index")) is int and choice["choice_index"] == 0
                 and choice.get("message_canonical_sha256") == digest(message), "Owner choice binding differs")
        extracted, unsupported = message_segments(message)
        _require(not unsupported and choice.get("unsupported") == unsupported, "Unsupported actor message")
        for segment in extracted:
            segment["output_pointer"] = "/choices/0/message" + segment["message_pointer"]
            segment["inspect_view_pointer"] = prefix + "/output" + segment["output_pointer"]
            segment["segment_id"] = digest([
                SUPPLEMENT_VERSION, canonical["annotation_id"], episode["raw_log_reference"]["sha256"],
                episode["sample_uuid"], event["event_uuid"], ordinal, 0, segment["message_pointer"], segment["kind"],
            ])
        _require(extracted == choice.get("segments"), "Owner segments differ from saved output")
        disposition = event.get("disposition")
        matched = disposition == "canonical_message_matched"
        _require(matched or disposition == "supplementary_returned_source", "Unknown owner event disposition")
        bindings = choice.get("canonical_segment_bindings")
        _require(type(bindings) is list, "Missing canonical segment bindings")
        if matched:
            message_index = choice.get("canonical_message_index")
            _require(_integer(message_index) and (not matched_messages or matched_messages[-1] < message_index),
                     "Nonmonotone canonical message binding")
            matched_messages.append(message_index)
            matched_ordinals.append(ordinal)
            _require(len(bindings) == len(extracted), "Incomplete canonical segment bindings")
        else:
            _require(bindings == [] and choice.get("canonical_message_index") is None,
                     "Supplementary event masquerades as canonical")
        for position, segment in enumerate(extracted):
            if matched:
                binding = bindings[position]
                identity = binding.get("canonical_segment_id")
                _require(identity in canonical_segments, "Canonical segment is absent from projection")
                canonical_segment = canonical_segments[identity]
                pointer = f"/{message_index}" + segment["message_pointer"]
                _require(binding == {"raw_segment_id": segment["segment_id"], "canonical_json_pointer": pointer,
                                     "canonical_segment_id": identity, "exact_value_and_availability_match": True},
                         "Canonical segment binding differs")
                _require(canonical_segment["message_index"] == message_index
                         and canonical_segment["json_pointer"] == pointer
                         and all(canonical_segment[key] == segment[key] for key in ("kind", "availability", "value")),
                         "Canonical segment content differs from saved model event")
                canonical_ids.append(identity)
            else:
                identity, pointer = segment["segment_id"], segment["inspect_view_pointer"]
                _require(projected.get(identity) == segment, "Supplement differs from saved model event")
                supplementary_ids.append(identity)
            ordered.append({"segment_id": identity,
                            "source_surface": "canonical" if matched else "supplementary_model_event",
                            "order": [ordinal, 0, position], "kind": segment["kind"], "native_pointer": pointer,
                            "canonical_message_index": choice["canonical_message_index"], "raw_event_index": index})
    _require(canonical_ids == [s["segment_id"] for s in canonical["segments"]], "Canonical source inventory/order differs")
    _require(supplementary_ids == [s["segment_id"] for s in supplement["segments"]], "Supplement inventory/order differs")
    _require(matching.get("canonical_message_indices") == matched_messages
             and matching.get("eligible_event_ordinals") == list(range(len(events)))
             and matching.get("mapping") == matched_ordinals, "Owner message/event mapping differs")
    _require(len(set(canonical_ids + supplementary_ids)) == len(ordered), "Duplicate bound segment")
    return {"annotation_id": canonical["annotation_id"], "canonical_evidence_status": canonical["evidence_status"],
            "canonical_segment_count": len(canonical_ids), "supplementary_segment_count": len(supplementary_ids),
            "ordered_sources": ordered, "episode_wide_absence": "not_established", "report_completeness": "not_established"}


def materialize_combined_population(canonical, canonical_owner, supplementary, supplementary_owner,
                                    crosswalk, *, source_references, instructions):
    canonical_rows, identities = _rows(canonical, "items")
    sources = ((canonical_owner, "episodes"), (supplementary, "items"),
               (supplementary_owner, "episodes"), (crosswalk, "episodes"))
    inventories = []
    for document, key in sources:
        rows, selected = _rows(document, key)
        _require(selected == identities, "Source episode population/order differs")
        inventories.append(rows)
    _require(type(canonical.get("assigned_count")) is int and canonical["assigned_count"] == len(identities)
             and type(canonical_owner.get("assigned_count")) is int
             and canonical_owner["assigned_count"] == len(identities), "Assigned population count differs")
    _require(canonical_owner.get("schedule_identity") == supplementary_owner.get("schedule_identity")
             and type(canonical_owner.get("schedule_identity")) is dict, "Owner schedule binding differs")
    _require(supplementary.get("canonical_projection_reference") == source_references["canonical_projection"]
             and crosswalk.get("source") == source_references["supplementary_inventory"]
             and crosswalk.get("projection_source") == source_references["supplementary_projection"],
             "Cross-document source binding differs")
    bindings, supplements, owners, order_rows = inventories
    _require(canonical_owner["schedule_identity"].get("assignment_ids") == [r["assignment_id"] for r in bindings],
             "Assigned schedule order differs")
    items, requests, audit = [], [], []
    for index, (item, binding, supplement, owner, saved_order) in enumerate(
            zip(canonical_rows, bindings, supplements, owners, order_rows)):
        _require(owner.get("assignment_ordinal") == index and type(owner.get("assignment_ordinal")) is int
                 and owner.get("assignment_id") == binding["assignment_id"]
                 and owner.get("verified_attempt_id") == binding["verified_attempt_id"], "Owner attempt binding differs")
        _reference(owner.get("canonical_item_reference"), source_references["canonical_projection"], f"/items/{index}", item)
        _reference(owner.get("reviewed_owner_reference"), source_references["canonical_owner"], f"/episodes/{index}", binding)
        _require(owner.get("preserved_canonical_status") == item["evidence_status"]
                 and owner.get("preserved_canonical_report") == item.get("report"), "Canonical status/report changed")
        order = _bound_order(item, supplement, owner)
        _require(order == saved_order, "Saved order crosswalk differs from independent owner reconstruction")
        combined = build_combined_construct_payload(item, supplement, order)
        request = render_combined_construct_request(combined, instructions)
        items.append(combined)
        requests.append(request)
        audit.append({"annotation_id": item["annotation_id"], "assignment_id": binding["assignment_id"],
                      "verified_attempt_id": binding["verified_attempt_id"], "canonical_source": binding["canonical_source"],
                      "native_linkage_eligible": binding["native_linkage_eligible"],
                      "raw_log_reference": owner["raw_log_reference"], "owner_row_index": index,
                      "canonical_segments": len(item["segments"]), "supplementary_segments": len(supplement["segments"]),
                      "evidence_status": combined["evidence_status"], "payload_canonical_sha256": digest(combined),
                      "request_messages_utf8_sha256": request["messages_utf8_sha256"],
                      "eligibility_resolution_application": "separate_future_join_not_encoded_in_coder_payload"})
    status_counts = dict(Counter(item["evidence_status"] for item in items))
    population = {"version": PAYLOAD_VERSION, "coordinate_namespace": COORDINATE_NAMESPACE,
                  "unit": "assigned_episode", "assigned_count": len(items), "items": items,
                  "concealment": "field_allowlist_only_actor_text_may_reveal_condition"}
    owner_result = {"owner_only_never_send_to_coder": True, "source_references": source_references,
                    "items": audit, "evidence_status_counts": status_counts,
                    "canonical_segments": sum(r["canonical_segments"] for r in audit),
                    "supplementary_segments": sum(r["supplementary_segments"] for r in audit),
                    "supplementary_episodes": sum(bool(r["supplementary_segments"]) for r in audit),
                    "actual_model_calls": 0, "actual_tokenizer_calls": 0, "coding_panel_selected": False,
                    "source_identity_verified_by": "caller_must_verify_exact_input_bytes_before_this_function",
                    "raw_logs_reopened": False, "prior_owner_source_acceptance_required": True}
    return population, requests, owner_result
