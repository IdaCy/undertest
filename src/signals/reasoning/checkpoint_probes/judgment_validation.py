"""Reuse V3 evidence validation and check broad-score input/output consistency."""
import hashlib
import json
from pathlib import Path
from signals.reasoning.checkpoint_probes import lean_awareness, awareness_v3

V3_ANCESTOR_SHA = "16b5bd976903374631ef57f4b676326ba53368bcd9fc88f42eb7cb311926810f"


def implementation_hashes():
    return {
        "awareness_v3_sha256": hashlib.sha256(Path(awareness_v3.__file__).read_bytes()).hexdigest(),
        "awareness_v3_ancestor_sha256": V3_ANCESTOR_SHA,
        "lean_awareness_sha256": hashlib.sha256(Path(lean_awareness.__file__).read_bytes()).hexdigest(),
    }


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def verified_v3_packet(record):
    reference = record.get("local_validation") or {}
    raw = Path(reference["packet_path"]).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == reference["packet_sha256"], "local_packet_hash_mismatch")
    require(json.loads(raw) == record["judge_input"]["packet"], "local_packet_content_mismatch")
    require(reference["packet_sha256"] == record["judge_input"]["metadata"]["packet_sha256"], "input_packet_binding_mismatch")
    return raw


def preflight(record):
    if record["task_kind"] == "awareness_v1":
        lean_awareness.preflight(record["judge_input"])
    elif record["task_kind"] == "awareness_v3":
        verified_v3_packet(record)
    elif record["task_kind"] == "broad_probe_score_v1":
        data = record["judge_input"]
        require(set(data) == {"probe", "response", "necessary_context", "requested_dimensions", "rubrics"}, "exact_blinded_scoring_fields_required")
        require(isinstance(data["probe"], str) and bool(data["probe"].strip()), "probe_text_required")
        require(isinstance(data["requested_dimensions"], list) and all(isinstance(x, str) and x for x in data["requested_dimensions"]), "dimension_ids_must_be_strings")
        require(isinstance(data["rubrics"], list), "rubrics_must_be_list")
        for rubric in data["rubrics"]:
            require(isinstance(rubric, dict) and set(rubric) == {"dimension_id", "scale", "anchors", "evidence_rule"}, "exact_blinded_rubric_fields_required")
            require(rubric["scale"] == [0, 10], "scoring_scale_must_be_0_to_10")
            require(isinstance(rubric["anchors"], dict) and set(rubric["anchors"]) == {"0", "5", "10"} and all(isinstance(x, str) and x for x in rubric["anchors"].values()), "three_explicit_anchors_required")
            require(isinstance(rubric["evidence_rule"], str) and bool(rubric["evidence_rule"]), "evidence_rule_required")
        wanted = data["requested_dimensions"]
        require(len(wanted) == len(set(wanted)) and bool(wanted), "requested_dimensions_duplicate_or_empty")
        require(len(wanted) == len(data["rubrics"]) and set(wanted) == {r["dimension_id"] for r in data["rubrics"]}, "dimension_rubric_mismatch")
        require(isinstance(data["response"], str) and isinstance(data["necessary_context"], str), "response_and_context_must_be_exact_text")


def validate(record, judgment, schema):
    if record["task_kind"] == "awareness_v1":
        return lean_awareness.validate(record["judge_input"], judgment)
    if record["task_kind"] == "awareness_v3":
        raw = verified_v3_packet(record)
        for key, value in record["judge_input"]["metadata"].items():
            require(judgment[key] == value, "annotation_metadata_mismatch:" + key)
        awareness_v3.validate_submission(raw, json.dumps(judgment, ensure_ascii=False, allow_nan=False).encode(), schema)
        return "preserved_v3_spans_stances_temporal_coverage_checks; semantic_accuracy_requires_calibration"
    if record["task_kind"] != "broad_probe_score_v1":
        return "json_schema_only"
    data = record["judge_input"]
    requested = data["requested_dimensions"]
    returned = [d["dimension_id"] for d in judgment["dimensions"]]
    require(len(returned) == len(set(returned)) and set(requested) == set(returned), "dimension_census_mismatch")
    for dimension in judgment["dimensions"]:
        if dimension["status"] == "observed":
            require(type(dimension["score"]) in {int, float} and bool(dimension["evidence"]), "observed_requires_score_and_evidence")
        else:
            require(dimension["score"] is None and bool(dimension["reason_code"]), "missingness_requires_null_score_and_reason")
        for evidence in dimension["evidence"]:
            text = data["response"] if evidence["source"] == "response" else data["necessary_context"]
            quote, start, end = evidence["quote"], evidence["start_char"], evidence["end_char"]
            require(bool(quote), "empty_evidence_quote")
            if start is None and end is None:
                require(quote in text, "quote_not_in_source")
            else:
                require(type(start) is int and type(end) is int and 0 <= start < end <= len(text), "invalid_quote_offsets")
                require(text[start:end] == quote, "quote_offset_mismatch")
    return "broad_score_dimension_missingness_and_exact_quote_checks; semantic_accuracy_requires_calibration"
