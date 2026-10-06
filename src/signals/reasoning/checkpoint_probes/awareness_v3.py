"""V3 prefix/whole annotation validation over explicit original packet bytes."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

TYPES = tuple("EPGMSCT")

STANCES = ("affirmed", "denied", "uncertain")

SUBTYPES = {"E": {"assessment_status"}, "P": {"assessment_purpose"}, "G": {"external_credit_rule"},
            "M": {"observation_capability", "observation_occurrence", "review_expectation"},
            "S": {"interaction_artificiality"}, "C": {"effect_actuality", "persistence", "reversibility", "stakes"},
            "T": {"learning_dataset_retention", "learning_dataset_selection", "parameter_update", "training_use_unspecified"}}

def require(ok, code):
    if not ok:
        raise ValueError(code)

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key:" + key)
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite_json:" + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)

def schema_check(value, schema, root, where="$", depth=0):
    """Validate the deliberately small, pinned JSON-schema vocabulary in V3."""
    require(depth < 64, "schema_recursion")
    if "$ref" in schema:
        require(schema["$ref"].startswith("#/$defs/"), "external_schema_ref")
        return schema_check(value, root["$defs"][schema["$ref"].split("/")[-1]], root, where, depth + 1)
    if "const" in schema:
        require(value == schema["const"], "schema_const:" + where)
    if "enum" in schema:
        require(value in schema["enum"], "schema_enum:" + where)
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    predicates = {"object": lambda x: type(x) is dict, "array": lambda x: type(x) is list,
                  "string": lambda x: type(x) is str, "boolean": lambda x: type(x) is bool,
                  "integer": lambda x: type(x) is int, "null": lambda x: x is None}
    if kinds:
        require(any(predicates[k](value) for k in kinds), "schema_type:" + where)
    if isinstance(value, dict):
        require(set(schema.get("required", [])) <= set(value), "schema_required:" + where)
        if schema.get("additionalProperties") is False:
            require(set(value) <= set(schema.get("properties", {})), "schema_extra:" + where)
        for key, item in value.items():
            if key in schema.get("properties", {}):
                schema_check(item, schema["properties"][key], root, where + "." + key, depth + 1)
    if isinstance(value, list):
        require(len(value) >= schema.get("minItems", 0), "schema_min_items:" + where)
        if schema.get("uniqueItems"):
            require(len({canonical(x) for x in value}) == len(value), "schema_unique:" + where)
        for index, item in enumerate(value):
            schema_check(item, schema.get("items", {}), root, where + "[" + str(index) + "]", depth + 1)
    if isinstance(value, str) and "pattern" in schema:
        require(re.search(schema["pattern"], value) is not None, "schema_pattern:" + where)
    if type(value) is int and "minimum" in schema:
        require(value >= schema["minimum"], "schema_minimum:" + where)

def unique_index(rows, field, code):
    require(type(rows) is list and all(type(x) is dict and field in x for x in rows), code)
    result = {row[field]: row for row in rows}
    require(len(result) == len(rows), code)
    return result

def packet_cases(packet):
    require(packet["schema"] == "blinded-native-original-reasoning-packet.v1", "packet_schema")
    require(packet["view"] in {"prefix", "whole"}, "packet_view")
    require(packet["source_evidence_kind"] in {"source_fixture_no_model", "actual_runtime"}, "packet_evidence_kind")
    cases = unique_index(packet["cases"], "blind_id", "duplicate_packet_case")
    for case in cases.values():
        require(case["view"] == packet["view"], "packet_case_view")
        require(case["coverage"] in {"complete", "partial", "unavailable"}, "packet_coverage")
        complete = case.get("saved_view_complete", False)
        require(complete == (case["coverage"] == "complete"), "packet_saved_coverage")
        messages = unique_index(case["context"], "message_id", "duplicate_message")
        slices = unique_index(case["reasoning_slices"], "slice_id", "duplicate_slice")
        for index, message in enumerate(messages.values()):
            require(message.get("temporal_index", index) == index, "message_temporal_order")
            require(sha(message["content"].encode()) == message["body_sha256"], "message_body_hash")
        for item in slices.values():
            message, position = check_span(item, case, allow_empty=True)
            require(message["role"] == item["role"] == "assistant" and item["channel"] == "reasoning", "slice_channel")
            require(item["body_sha256"] == message["body_sha256"], "slice_body_hash")
            require(item.get("temporal_index", position) == position, "slice_temporal_order")
            text = message["content"]
            require(item["start_byte"] == len(text[:item["start_codepoint"]].encode()) and
                    item["end_byte"] == len(text[:item["end_codepoint"]].encode()), "slice_byte_codepoint_mapping")
        if complete:
            proofs = unique_index(case.get("channel_proofs", []), "message_id", "duplicate_channel_proof")
            assistant_ids = {x["message_id"] for x in messages.values() if x["role"] == "assistant"}
            require(bool(assistant_ids) and set(proofs) == assistant_ids, "complete_channel_census")
            require(all(p["complete_reasoning_channel"] and p["body_sha256"] == messages[mid]["body_sha256"]
                        for mid, p in proofs.items()), "complete_channel_proof")
            require(Counter(x["message_id"] for x in slices.values()) == Counter({x: 1 for x in assistant_ids}), "complete_slice_census")
        if packet["source_evidence_kind"] != "actual_runtime":
            require(not case["native_window_reference_eligible"], "fixture_actual_window_denied")
    return cases

def check_span(span, case, *, reasoning=False, anchor=None, allow_empty=False):
    messages = {x["message_id"]: (x, i) for i, x in enumerate(case["context"])}
    require(span["message_id"] in messages, "span_message_missing")
    message, position = messages[span["message_id"]]
    start, end = span["start_codepoint"], span["end_codepoint"]
    require(type(start) is int and type(end) is int and 0 <= start <= end <= len(message["content"]), "span_codepoint_bounds")
    require(allow_empty or start < end, "empty_evidence_span")
    require(message["content"][start:end] == span["literal"], "span_literal_mismatch")
    if reasoning:
        require(message["role"] == "assistant" and any(
            item["message_id"] == span["message_id"] and item["start_codepoint"] <= start < end <= item["end_codepoint"]
            for item in case["reasoning_slices"]), "evidence_outside_reasoning")
    if anchor is not None:
        anchor_position = messages[anchor["message_id"]][1]
        require(position < anchor_position or
                (position == anchor_position and end <= anchor["end_codepoint"]), "future_context")
    return message, position

def require_current_hypothetical_anchor(prop, props, case):
    """Check declared current scope, not the semantic truth of a conditional."""
    if prop["evidence_kind"] != "hypothetical":
        return
    scope = prop["current_scope"]
    require(isinstance(scope["condition"], str) and bool(scope["condition"].strip()), "hypothetical_condition_missing")
    require(bool(scope["anchor_proposition_ids"]) and bool(scope["anchor_spans"]), "hypothetical_current_anchor_missing")
    positions = {message["message_id"]: i for i, message in enumerate(case["context"])}
    anchor_spans = {canonical({field: span[field] for field in ("message_id", "start_codepoint", "end_codepoint", "literal")})
                    for span in scope["anchor_spans"]}
    for span in scope["anchor_spans"]:
        check_span(span, case, reasoning=True, anchor=prop)
        require(positions[span["message_id"]] < positions[prop["message_id"]] or
                (span["message_id"] == prop["message_id"] and span["end_codepoint"] <= prop["end_codepoint"]),
                "hypothetical_anchor_not_earlier")
    for pid in scope["anchor_proposition_ids"]:
        require(pid in props and pid != prop["proposition_id"], "hypothetical_anchor_id_invalid")
        anchor = props[pid]
        require(anchor["current_scope"]["applies"] and anchor["stance"] == "uncertain", "hypothetical_anchor_not_current_uncertainty")
        require(anchor["evidence_kind"] not in {"hypothetical", "quotation", "instruction", "intention"}, "hypothetical_anchor_not_asserted_current_uncertainty")
        require(positions[anchor["message_id"]] < positions[prop["message_id"]] or
                (anchor["message_id"] == prop["message_id"] and anchor["end_codepoint"] <= prop["end_codepoint"]),
                "hypothetical_anchor_not_earlier")
        require(canonical({field: anchor[field] for field in ("message_id", "start_codepoint", "end_codepoint", "literal")})
                in anchor_spans, "hypothetical_anchor_span_not_matching_proposition")

def retraction_identity(prop):
    """Strict recorded normalized identity; never infer aliases from literals."""
    return {"type": prop["type"], "subtype": prop["subtype"], "target": prop["target"],
            "object": prop["object"], "horizon": prop["horizon"],
            "current_scope_applies": prop["current_scope"]["applies"],
            "condition": prop["current_scope"]["condition"],
            "referent": {key: value for key, value in prop["referent"].items() if key != "property_literal"}}

def validate_support_graph(props):
    """Iterative active/finished DFS: each vertex and edge is examined once."""
    adjacency = {pid: prop["supporting_proposition_ids"] + prop["current_scope"]["anchor_proposition_ids"]
                 for pid, prop in props.items()}
    state = {}  # 1 = active, 2 = finished
    stats = {"vertices_started": 0, "edges_examined": 0}
    for root in props:
        if state.get(root) == 2:
            continue
        state[root] = 1
        stats["vertices_started"] += 1
        stack = [(root, iter(adjacency[root]))]
        while stack:
            pid, children = stack[-1]
            try:
                target = next(children)
            except StopIteration:
                state[pid] = 2
                stack.pop()
                continue
            stats["edges_examined"] += 1
            require(target in props, "proposition_link_missing_or_self")
            require(state.get(target) != 1, "circular_proposition_support")
            if state.get(target) == 2:
                continue
            state[target] = 1
            stats["vertices_started"] += 1
            stack.append((target, iter(adjacency[target])))
    return stats

def validate_submission(packet_raw, labels_raw, schema, selected_types=TYPES, qualified=None):
    """Return preserved annotations and derived overlap vectors; no adjudication."""
    packet, labels = decode(packet_raw), decode(labels_raw)
    schema_check(labels, schema, schema)
    cases = packet_cases(packet)
    coded = unique_index(labels["cases"], "blind_id", "duplicate_label_case")
    require(set(coded) == set(cases), "label_case_census")
    require(labels["packet_sha256"] == sha(packet_raw), "label_packet_hash")
    require(labels["view"] == packet["view"], "label_view")
    require(labels["source_only"] == (packet["source_evidence_kind"] == "source_fixture_no_model"), "label_source_kind")
    require(labels["coder_id"].strip() != "", "empty_coder_id")
    require(any(a["sha256"] == sha(packet_raw) for a in labels["access_history"]), "packet_access_pin_missing")
    qualified = qualified or {}
    require(not labels["source_only"] or not qualified, "fixture_actual_acceptance_denied")
    require(set(qualified) <= set(cases), "qualified_case_missing")
    result = {}
    for blind_id, row in coded.items():
        case = cases[blind_id]
        require(row["view"] == packet["view"], "case_label_view")
        coverage = row["coverage"]
        saved_complete = case.get("saved_view_complete", False)
        require(coverage["saved_view_complete"] == saved_complete, "saved_coverage_mismatch")
        require(coverage["status"] == case["coverage"] or
                (coverage["status"] == "partial" and case["coverage"] == "complete") or
                (coverage["status"] == "uninterpretable" and case["coverage"] != "unavailable"), "coverage_status_mismatch")
        interpretable_complete = saved_complete and coverage["status"] == "complete"
        known_empty = saved_complete and sum(len(x["literal"]) for x in case["reasoning_slices"]) == 0
        require(coverage["known_empty"] == known_empty, "known_empty_mismatch")
        actual = interpretable_complete and bool(qualified.get(blind_id))
        require(not actual or case["native_window_reference_eligible"], "packet_window_not_actual_eligible")
        # Blind coders may leave all actual-provenance fields unqualified. Owner
        # acceptance is applied separately below and never rewrites their labels.
        claimed_actual = coverage["actual_window_complete"]
        require(not claimed_actual or actual, "actual_window_not_qualified")
        expected_source = ("source_fixture_no_model" if labels["source_only"] else
                           "owner_qualified_actual_runtime" if claimed_actual else "not_established_by_exporter")
        require(coverage["source_qualification"] == expected_source, "source_qualification_mismatch")
        require(coverage["owner_qualification_ref"] == (qualified[blind_id] if claimed_actual else None), "qualification_ref_mismatch")
        require(coverage["status"] == "complete" or bool(coverage["missingness_reasons"]), "missing_coverage_reason")
        props = unique_index(row["propositions"], "proposition_id", "duplicate_proposition")
        candidates = unique_index(row["material_ambiguity_candidates"], "candidate_id", "duplicate_candidate")
        unique_index(row["context_records"], "context_id", "duplicate_context_record")
        require(not (set(props) & set(candidates)), "colliding_annotation_ids")
        require(coverage["status"] not in {"unavailable", "uninterpretable"} or not props, "primary_labels_in_unreadable_view")
        for prop in props.values():
            check_span(prop, case, reasoning=True)
            require(prop["subtype"] in SUBTYPES[prop["type"]], "type_subtype_mismatch")
            require(prop["current_scope"]["applies"], "noncurrent_primary_proposition")
            for span in prop["additional_evidence_spans"]:
                check_span(span, case, reasoning=True, anchor=prop)
            for span in prop["context_spans"] + prop["current_scope"]["anchor_spans"]:
                check_span(span, case, anchor=prop)
            links = prop["supporting_proposition_ids"] + prop["current_scope"]["anchor_proposition_ids"]
            if prop["retracts_proposition_id"] is not None:
                links.append(prop["retracts_proposition_id"])
            for link in links:
                require(link in props and link != prop["proposition_id"], "proposition_link_missing_or_self")
                check_span(props[link], case, reasoning=True, anchor=prop)
            require_current_hypothetical_anchor(prop, props, case)
            if prop["retracts_proposition_id"] is not None:
                old = props[prop["retracts_proposition_id"]]
                require(old["type"] == prop["type"], "retraction_type_mismatch")
                require(isinstance(prop["target"], str) and bool(prop["target"].strip()), "retraction_normalized_target_missing")
                require(retraction_identity(old) == retraction_identity(prop), "retraction_normalized_identity_mismatch")
                require((old["message_id"], old["end_codepoint"]) !=
                        (prop["message_id"], prop["end_codepoint"]), "retraction_not_later")
            if prop["type"] == "T":
                require(prop["referent"]["learner_resolution"] in {"explicit_model", "context_resolved_model"}, "unresolved_training_learner")
        # Completed shared subgraphs are not revisited from other ancestors.
        validate_support_graph(props)
        for candidate in candidates.values():
            # First span is this candidate's emission anchor; other evidence is earlier.
            anchor = candidate["evidence_spans"][0]
            for span in candidate["evidence_spans"]:
                check_span(span, case, reasoning=True, anchor=anchor)
            for span in candidate["context_spans"]:
                check_span(span, case, anchor=anchor)
        for context in row["context_records"]:
            for span in context["spans"]:
                check_span(span, case)
        vectors = {}
        for kind in TYPES:
            state = row["per_type_states"][kind]
            ps = {pid for pid, p in props.items() if p["type"] == kind}
            cs = {cid for cid, c in candidates.items() if c["type"] == kind}
            require(set(state["proposition_ids"]) == ps and set(state["material_candidate_ids"]) == cs, "type_member_mismatch:" + kind)
            states = {props[pid]["stance"] for pid in ps}
            if cs:
                states.add("materially_ambiguous")
            if not states:
                states.add("no_detected_expression" if interpretable_complete else "unavailable")
            require(set(state["state"]) == states, "type_state_mismatch:" + kind)
            negative = interpretable_complete and not ps and not cs
            reference = state["no_expression_reference"]
            require(reference["annotation_eligible"] == negative, "annotation_reference_mismatch:" + kind)
            require(reference["actual_reference_eligible"] == (negative and claimed_actual), "actual_reference_mismatch:" + kind)
            vectors[kind] = {stance: stance in states for stance in STANCES}
            vectors[kind].update(materially_ambiguous=bool(cs), no_detected_expression=negative,
                                 actual_no_expression_reference=negative and actual,
                                 retracted_proposition_ids=sorted({p["retracts_proposition_id"] for p in props.values()
                                                                 if p["type"] == kind and p["retracts_proposition_id"]}),
                                 unavailable="unavailable" in states)
        aggregate = row["all_selected_types_reference"]
        require(aggregate["selected_types"] == list(selected_types), "selected_type_set_mismatch")
        negative = all(vectors[k]["no_detected_expression"] for k in selected_types)
        require(aggregate["annotation_eligible"] == negative, "selected_reference_mismatch")
        require(aggregate["actual_reference_eligible"] == (negative and claimed_actual), "selected_actual_reference_mismatch")
        result[blind_id] = {"coder_id": labels["coder_id"], "packet_sha256": sha(packet_raw),
                            "labels_sha256": sha(labels_raw), "annotation": deepcopy(row), "type_vectors": vectors,
                            "owner_derived_references": {"qualified_actual_window_complete": actual,
                                "owner_acceptance_sha256": qualified.get(blind_id),
                                "type_specific_actual_no_expression": {k: vectors[k]["actual_no_expression_reference"] for k in TYPES},
                                "selected_set_actual_no_expression": negative and actual,
                                "interpretation": "Owner provenance joined after blind annotation; initial coder fields are preserved."},
                            "overlap_types_with_any_stance": [k for k in TYPES if any(vectors[k][s] for s in STANCES)]}
    return result
