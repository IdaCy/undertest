"""Pure key-free field views of pinned anchor evidence; no reading or dispatch.

The enclosing return includes private provenance. Distribute only reader_packet
with its pinned schema/rubric and the named instruction successor. The caller
supplies independently opaque item IDs per channel; no cross-call registry or
blinding guarantee is implemented here.
"""

from copy import deepcopy
import re

import signals.behavioral.outcomes.anchor_outcomes as outcomes


VERSION = "sigres-anchor-reader-field-views-v1"
INSTRUCTION_VERSION = "sigres-anchor-saved-reader-instructions-v1"
CHANNELS = ("public_text", "raw_reasoning", "reasoning_summary")
READER_CONTRACT = {
    "instruction_version": INSTRUCTION_VERSION,
    "annotation_schema": {"bytes": 26579, "sha256": "ea1488166f04f4962fa9523baf487c155221a9663cdff9fdcd8abc2292d2f4b2"},
    "operational_rubric": {"bytes": 9933, "sha256": "87b8a456f3dfac4d1f882ea2fc40ed6791674631e6591061e0c5d9e0bb68d2d6"},
}
_TEXT_FIELDS = ("content", "reasoning", "reasoning_content", "refusal")


class AnchorReaderViewError(ValueError):
    """A supplied join or complete opaque identity map cannot be authenticated."""


def _require(condition, reason):
    if not condition:
        raise AnchorReaderViewError(reason)


def _load(read_exact, pin):
    # Deliberate reuse of the exact adopted V2 byte/JSON boundary; its source pin
    # is a dependency of this module, rather than a second general join engine.
    return outcomes._json(outcomes._read(read_exact, pin))


def _verified_join(pin, read_exact):
    supplied = _load(read_exact, pin)
    _require(type(supplied) is dict and supplied.get("schema") == outcomes.SCHEMA,
             "unsupported_join_result")
    deps = supplied.get("dependencies")
    _require(type(deps) is dict and all(k in deps for k in ("frame", "terminal", "packet_manifest", "sources")),
             "missing_join_dependencies")
    actual = outcomes.join_anchor_saved_outcomes(
        frame_pin=deps["frame"], terminal_pin=deps["terminal"],
        packet_manifest_pin=deps["packet_manifest"], source_pins=deps["sources"], read_exact=read_exact)
    _require(outcomes._wire(supplied) == outcomes._wire(actual), "saved_join_does_not_match_recomputed_evidence")
    return actual


def _state(message, field):
    if type(message) is not dict or field not in message:
        return "absent"
    value = message[field]
    if value is None:
        return "null"
    if type(value) is not str:
        return "wrong_type"
    return "empty" if value == "" else "text"


def _context(row, read_exact):
    if row["client_request_join"] != "exact_saved_client_request":
        return None, "missing_or_unjoined_request_context"
    if row["kind"] == "ask" and row["original_prefix_join"] != "exact_saved_original_prefix":
        return None, "missing_or_unjoined_original_prefix"
    request = _load(read_exact, row["evidence"]["request"]["pin"])
    messages = request["messages"]
    roles = ["system", "user"] if row["kind"] == "original" else ["system", "user", "assistant", "user"]
    if len(messages) != len(roles):
        return None, "unsupported_request_context"
    projected = []
    for message, role in zip(messages, roles):
        if type(message) is not dict or message.get("role") != role:
            return None, "unsupported_request_context"
        if any(field in message and message[field] is not None and type(message[field]) is not str
               for field in _TEXT_FIELDS):
            return None, "unsupported_context_text_representation"
        # Non-text native payloads are retained privately, never silently rendered.
        if any(message.get(field) not in (None, []) for field in ("tool_calls", "annotations")):
            return None, "unsupported_nontext_context"
        if any(message.get(field) is not None for field in ("audio", "function_call")):
            return None, "unsupported_nontext_context"
        if set(message) - {"role", *_TEXT_FIELDS, "tool_calls", "annotations", "audio", "function_call"}:
            return None, "unsupported_context_fields"
        projected.append({key: deepcopy(message[key]) for key in ("role", *_TEXT_FIELDS) if key in message})
    return projected, "exact_saved_request_context"


def _target(row, channel):
    message = row["observed_native_message"]
    if channel == "reasoning_summary":
        return None, [], "unavailable", "no_authenticated_provider_summary"
    if row["observed_message_join"] != "exact_saved_observed_message":
        return None, [], "unavailable", "raw_observation_not_joined"
    if type(message) is not dict or message.get("role") != "assistant":
        return None, [], "unavailable", "observed_speaker_is_not_assistant"
    if channel == "public_text":
        state = _state(message, "content")
        if state not in {"text", "empty"}:
            return None, [], "unavailable", "content_" + state
        fields = ["content"]
    else:
        fields = [field for field in ("reasoning", "reasoning_content")
                  if _state(message, field) in {"text", "empty"}]
        if any(_state(message, field) == "wrong_type" for field in ("reasoning", "reasoning_content")):
            return None, [], "unresolved", "reasoning_alias_representation_conflict"
        if not fields:
            return None, [], "unavailable", "reasoning_fields_absent_or_null"
        if len(fields) == 2 and message[fields[0]] != message[fields[1]]:
            return None, [], "unresolved", "reasoning_alias_text_conflict"
    if any(row["observed_channels"].get(field, {}).get("join") != "exact_saved_observed_channel"
           for field in fields):
        return None, fields, "unavailable", "selected_channel_record_not_joined"
    return message[fields[0]], fields, "available", "exact_saved_assistant_field"


def _completeness(row, response):
    try:
        if response["choices"][0].get("finish_reason") == "length":
            return "partial_return"
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    if row["source_status"] == "native_completed" and row["native_message_join"] == "exact_saved_native_message":
        return "recorded_complete_return"
    return "return_completeness_unknown"


def project_anchor_reader_views(*, joined_result_pin, target_channel, item_ids, read_exact):
    """Export all 108 slots for one designated channel after recomputing V2 joins.

    item_ids maps every assignment to a distinct reader-<32 lowercase hex> ID.
    IDs must be independently assigned for each channel by the caller; this
    pure function cannot detect reuse across separate calls. Bad root evidence
    fails, as in the adopted outcome join. Missing per-row evidence produces
    explicit placeholders and never fabricated messages. Neither returned
    reader material nor private provenance is a semantic annotation.
    """
    _require(type(target_channel) is str and target_channel in CHANNELS, "unsupported_target_channel")
    try:
        joined = _verified_join(joined_result_pin, read_exact)
        rows = joined["rows"]
        expected = {row["assignment_id"] for row in rows}
        _require(type(item_ids) is dict and set(item_ids) == expected and len(item_ids) == 108,
                 "complete_108_assignment_identity_map_required")
        _require(all(type(value) is str and re.fullmatch(r"reader-[0-9a-f]{32}", value) for value in item_ids.values())
                 and len(set(item_ids.values())) == 108, "unique_opaque_reader_ids_required")
        items, index = [], []
        for row in rows:
            ident = row["assignment_id"]
            context, context_status = _context(row, read_exact)
            text, fields, availability, reason = _target(row, target_channel)
            raw_response = None
            if row["evidence"]["raw_response"]["status"] == "json_verified":
                raw_response = _load(read_exact, row["evidence"]["raw_response"]["pin"])
            completeness = _completeness(row, raw_response)
            if context is None:
                availability, reason = "unavailable", context_status
            messages = [] if context is None else deepcopy(context)
            target_index = None
            if availability == "available":
                target_index = len(messages)
                messages.append({"role": "assistant", "content": text})
            item = {
                "item_id": item_ids[ident],
                "phase": "original_pre_question" if row["kind"] == "original" else "elicited_ASK",
                "target_channel": target_channel, "target_message_index": target_index,
                "channel_availability": availability, "availability_reason": reason,
                "context_status": context_status, "text_completeness": completeness,
                "target_text_state": "not_supplied" if target_index is None else ("empty" if text == "" else "text"),
                "target_text_utf8_sha256": outcomes._sha(text.encode("utf-8")) if target_index is not None else None,
                "messages": messages,
            }
            items.append(item)
            message = row["observed_native_message"]
            index.append({
                "item_id": item_ids[ident], "assignment_id": ident,
                "parent_assignment_id": row["parent_assignment_id"] if row["kind"] == "ask" else None,
                "case_id": row["case_id"], "kind": row["kind"], "target_channel": target_channel,
                "source_outcome": deepcopy(row["source_outcome"]), "source_status": row["source_status"],
                "outcome_presence": row["outcome_presence"], "evidence": deepcopy(row["evidence"]),
                "diagnostics": deepcopy(row["diagnostics"]),
                "recorded_native_stage_verified_here": False,
                "native_message_join": row["native_message_join"],
                "observed_message_join": row["observed_message_join"],
                "original_prefix_join": row["original_prefix_join"],
                "observed_native_message": deepcopy(message),
                "observed_channels": deepcopy(row["observed_channels"]),
                "field_states": {field: _state(message, field) for field in _TEXT_FIELDS},
                "target_native_fields": fields,
                "raw_response_field_selectors": ["choices[0].message." + field for field in fields],
                "target_text_utf8_sha256": outcomes._sha(text.encode("utf-8")) if text is not None else None,
                "context_message_indices": list(range(len(context))) if context is not None else [],
                "target_message_index": target_index,
                "reader_item_sha256": outcomes._sha(outcomes._wire(item)),
            })
        return {
            "version": VERSION, "distribute_only_reader_packet": True,
            "reader_packet": {"contract": deepcopy(READER_CONTRACT), "target_channel": target_channel,
                              "items": sorted(items, key=lambda item: item["item_id"])},
            "private_provenance": {"joined_result_pin": deepcopy(joined_result_pin),
                                   "verified_dependencies": deepcopy(joined["dependencies"]),
                                   "assigned_total": 108, "assigned_originals": 12, "assigned_ASK": 96,
                                   "source_counts": deepcopy(joined["counts"]), "index": index,
                                   "reader_dispatches": 0, "semantic_annotations": 0,
                                   "native_qualification_verified_here": False},
        }
    except (outcomes.AnchorOutcomeError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise AnchorReaderViewError("invalid_saved_reader_input:" + str(exc)) from exc
