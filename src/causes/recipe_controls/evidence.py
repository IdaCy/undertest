import copy
import csv
import hashlib
import io
import json
from pathlib import Path


EVIDENCE_VERSION = "effects-evidence-v2"
MAX_SOURCE_JSON_DEPTH = 128
ARTIFACTS = ("messages.json", "metadata.json", "scores.json", "export_errors.json",
             "effects/ledger.json", "effects/tool_requests.json", "effects/assessment.json",
             "effects/model_output.json", "effects/identity.json")
CONDITION_FIELDS = ("assignment_id", "instance_id", "family", "framing", "grading", "rollout", "prompt_variant", "condition")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _source_depth_supported(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_SOURCE_JSON_DEPTH:
            return False
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return True


def _reject_nonfinite(value):
    raise ValueError(f"Nonfinite JSON constant: {value}")


def _read(path, root, *, csv_file=False, raw_only=False):
    reference = {"path": str(path), "status": "missing", "sha256": None, "bytes": None}
    try:
        if not path.resolve().is_relative_to(root):
            reference["status"] = "outside_output_root"
            return reference, None
        raw = path.read_bytes()
        reference.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        text = raw.decode("utf-8")
        value = None if raw_only else list(csv.DictReader(io.StringIO(text))) if csv_file else json.loads(text, parse_constant=_reject_nonfinite)
        if not _source_depth_supported(value):
            reference.update(status="unsupported_nesting_depth", maximum_json_depth=MAX_SOURCE_JSON_DEPTH)
            return reference, None
        reference["status"] = "readable"
        return reference, value
    except FileNotFoundError:
        return reference, None
    except (OSError, UnicodeError, ValueError, csv.Error, RuntimeError) as exc:
        reference.update(status="unreadable", error=f"{type(exc).__name__}: {exc}")
        return reference, None


def _availability(text, redacted=False):
    if redacted:
        return "redacted"
    if not isinstance(text, str):
        return "unavailable_field"
    if text.startswith("attachment://"):
        return "unresolved_attachment"
    return "readable" if text else "empty"


def _transcript(messages, reference, capped):
    result = {"status": "unavailable", "segments": [], "outer_tool_calls": [], "tool_errors": [],
              "unsupported_content_blocks": [], "schema_issues": [], "reasoning_availability": "unavailable_transcript",
              "outer_call_parse_error_events": None, "tool_parsing_error_events": None,
              "report": {"status": "unavailable_transcript", "segment_ids": [], "ambiguous": True}}
    if not isinstance(messages, list) or not messages or any(not isinstance(message, dict) for message in messages):
        return result
    result["status"] = "available"
    reasoning_blocks = []
    tool_positions = []
    assistant_positions = []
    text_positions = {}

    def invalid(pointer, reason, value):
        result["schema_issues"].append({"json_pointer": pointer, "reason": reason, "value": copy.deepcopy(value)})

    def add(message_index, content_index, kind, field, value, redacted=False):
        pointer = f"/{message_index}/content" + (f"/{content_index}/{field}" if content_index is not None else "")
        segment = {
            "segment_id": _hash([reference["sha256"], pointer, kind]), "kind": kind,
            "message_index": message_index, "content_index": content_index, "field": field,
            "json_pointer": pointer, "source": copy.deepcopy(reference),
            "value": value, "availability": _availability(value, redacted),
        }
        result["segments"].append(segment)
        if kind == "text" and isinstance(value, str) and value.strip() and segment["availability"] == "readable":
            text_positions.setdefault(message_index, []).append(segment["segment_id"])

    for message_index, message in enumerate(messages):
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            invalid(f"/{message_index}", "unknown_or_missing_message_role", message)
            continue
        content = message.get("content")
        if not isinstance(content, (str, list)):
            invalid(f"/{message_index}/content", "invalid_or_missing_message_content", content)
        if message.get("role") == "tool":
            tool_positions.append(message_index)
            if message.get("error") is not None:
                error = message["error"]
                if not isinstance(error, dict) or error.get("type") not in ("parsing", "timeout", "unicode_decode", "permission", "file_not_found", "is_a_directory", "limit", "approval", "cancelled", "sandbox_unavailable", "unknown", "output_limit") or not isinstance(error.get("message"), str):
                    invalid(f"/{message_index}/error", "invalid_tool_error_record", error)
                result["tool_errors"].append({"message_index": message_index, "json_pointer": f"/{message_index}/error",
                                              "error": copy.deepcopy(message["error"]), "tool_call_id": message.get("tool_call_id"),
                                              "content": copy.deepcopy(message.get("content"))})
        if message.get("role") != "assistant":
            continue
        assistant_positions.append(message_index)
        calls = message.get("tool_calls")
        if calls is None:
            calls = []
        elif not isinstance(calls, list):
            invalid(f"/{message_index}/tool_calls", "invalid_tool_calls_container", calls)
            tool_positions.append(message_index)
            calls = []
        if calls:
            tool_positions.append(message_index)
        for call_index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(call.get("function"), str) or not isinstance(call.get("arguments"), dict):
                invalid(f"/{message_index}/tool_calls/{call_index}", "invalid_tool_call_record", call)
            elif call.get("parse_error") is not None and not isinstance(call["parse_error"], str):
                invalid(f"/{message_index}/tool_calls/{call_index}/parse_error", "invalid_outer_parse_error", call["parse_error"])
            result["outer_tool_calls"].append({"message_index": message_index, "call_index": call_index,
                                               "json_pointer": f"/{message_index}/tool_calls/{call_index}", "value": copy.deepcopy(call)})
        content = message.get("content")
        if isinstance(content, str):
            add(message_index, None, "text", "content", content)
        elif isinstance(content, list):
            for content_index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    add(message_index, content_index, "text", "text", block.get("text"))
                    if not isinstance(block.get("text"), str):
                        invalid(f"/{message_index}/content/{content_index}", "invalid_text_block", block)
                elif isinstance(block, dict) and block.get("type") == "reasoning":
                    availability = _availability(block.get("reasoning"), block.get("redacted") is True)
                    reasoning_blocks.append(availability)
                    add(message_index, content_index, "reasoning", "reasoning", block.get("reasoning"), block.get("redacted") is True)
                    if "summary" in block and block["summary"] is not None:
                        add(message_index, content_index, "reasoning_summary", "summary", block["summary"])
                else:
                    result["unsupported_content_blocks"].append({"message_index": message_index, "content_index": content_index,
                                                                 "json_pointer": f"/{message_index}/content/{content_index}", "value": copy.deepcopy(block)})
                    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                        invalid(f"/{message_index}/content/{content_index}", "invalid_content_block", block)
    if result["schema_issues"]:
        result["status"] = "partially_available"
    if not reasoning_blocks:
        result["reasoning_availability"] = "unavailable_incomplete_transcript" if result["schema_issues"] else "not_returned_or_not_recorded"
    elif all(value == "readable" for value in reasoning_blocks):
        result["reasoning_availability"] = "readable_returned_blocks"
    elif "readable" in reasoning_blocks:
        result["reasoning_availability"] = "partially_readable_returned_blocks"
    elif all(value == "redacted" for value in reasoning_blocks):
        result["reasoning_availability"] = "redacted_returned_blocks"
    else:
        result["reasoning_availability"] = "returned_blocks_without_readable_reasoning"
    last_tool = max(tool_positions, default=-1)
    candidates = [position for position in text_positions if position > last_tool]
    ambiguity = []
    if result["schema_issues"]:
        ambiguity.append("malformed_transcript_structure")
    if not tool_positions:
        ambiguity.append("no_tool_boundary")
    if len(candidates) > 1:
        ambiguity.append("multiple_post_tool_text_messages")
    if capped:
        ambiguity.append("recorded_generation_or_sample_limit")
    selected = max(candidates) if candidates else None
    if selected is not None and assistant_positions and max(assistant_positions) > selected:
        ambiguity.append("later_assistant_message_without_report_text")
    result["report"] = {
        "status": "candidate_after_tools" if selected is not None and tool_positions else "candidate_without_tools" if selected is not None else "no_text_after_last_tool",
        "message_index": selected, "last_tool_message_index": last_tool if tool_positions else None,
        "segment_ids": text_positions.get(selected, []), "ambiguous": bool(ambiguity) or selected is None,
        "ambiguity_reasons": ambiguity, "selection_is_structural_not_semantic": True,
    }
    if not result["schema_issues"]:
        result["outer_call_parse_error_events"] = sum(isinstance(call["value"], dict) and call["value"].get("parse_error") is not None for call in result["outer_tool_calls"])
        result["tool_parsing_error_events"] = sum(isinstance(event["error"], dict) and event["error"].get("type") == "parsing" for event in result["tool_errors"])
    return result


def _provenance_flags(value, scope):
    reasons = []
    for field in ("metadata_integrity", "metadata_complete"):
        if value.get(field) is not True:
            reasons.append(f"{scope}_{field}_" + ("false" if value.get(field) is False else "unverified"))
    if value.get("metadata_conflicts") or value.get("status") == "metadata_conflict" or value.get("termination") == "metadata_conflict":
        reasons.append(f"{scope}_metadata_conflict")
    if value.get("metadata_absent"):
        reasons.append(f"{scope}_metadata_absent")
    return reasons


def _attempt_evidence(root, row, attempt, pointer, input_reference, csv_reference, csv_rows, primary):
    reasons = _provenance_flags(row, "assignment") + _provenance_flags(attempt, "attempt")
    if not primary or row.get("primary_order_verified") is not True:
        reasons.append("primary_attribution_unverified")
    if attempt.get("ledger_integrity") is False:
        reasons.append("ledger_integrity_conflict")
    snapshots = []
    seen = set()
    usage_rows = []
    for sample_index, sample in enumerate(attempt.get("samples", [])):
        sample_key = sample.get("sample_key")
        if isinstance(sample_key, str) and sample_key and all(char.isalnum() or char in "-_" for char in sample_key):
            directory = root / "artifacts" / sample_key
        elif isinstance(sample.get("artifacts"), str):
            directory = Path(sample["artifacts"])
            if not directory.is_absolute():
                directory = root / directory
        else:
            reasons.append("unresolved_sample_artifact_path")
            continue
        if str(directory) in seen:
            continue
        seen.add(str(directory))
        sources = {}
        values = {}
        for name in ARTIFACTS:
            sources[name], values[name] = _read(directory / name, root)
        identity = values["effects/identity.json"]
        if not isinstance(identity, dict) or identity.get("assignment_id") != row["assignment_id"] or identity.get("attempt_id") != attempt["attempt_id"]:
            reasons.append("artifact_identity_unverified_or_conflicting")
        metadata = values["metadata.json"]
        sample_metadata = metadata.get("sample") if isinstance(metadata, dict) else None
        if not isinstance(sample_metadata, dict):
            reasons.append("artifact_sample_metadata_unavailable")
        else:
            for field in CONDITION_FIELDS:
                if field in row and sample_metadata.get(field) != row[field]:
                    reasons.append(f"artifact_metadata_mismatch_{field}")
        transcript = _transcript(values["messages.json"], sources["messages.json"], bool(attempt.get("model_cap") or attempt.get("sample_limit")))
        if transcript["status"] != "available":
            reasons.append("unavailable_or_invalid_transcript")
        for csv_index, csv_row in enumerate(csv_rows or []):
            if not sample_key or csv_row.get("sample_key") != sample_key:
                continue
            if csv_row.get("sample_id") != row["assignment_id"]:
                reasons.append("csv_sample_identity_mismatch")
            try:
                csv_metadata = json.loads(csv_row.get("sample_metadata", ""))
            except (ValueError, TypeError):
                csv_metadata = None
            if not isinstance(csv_metadata, dict):
                reasons.append("csv_sample_metadata_unavailable")
            elif any(field in row and csv_metadata.get(field) != row[field] for field in CONDITION_FIELDS):
                reasons.append("csv_sample_metadata_conflict")
            usage_rows.append({"source": copy.deepcopy(csv_reference), "row_index": csv_index,
                               "scope": "exported_sample_totals", "input_tokens": csv_row.get("input_tokens"),
                               "output_tokens": csv_row.get("output_tokens"), "values_are_reported_strings": True,
                               "availability": "unverified_zero_may_encode_missing_model_usage"})
        model_output = values["effects/model_output.json"]
        snapshots.append({
            "sample_index": sample_index, "sample_key": sample_key, "recorded_artifact_path": sample.get("artifacts"),
            "sources": sources, "transcript": transcript,
            "retained_tool_requests": copy.deepcopy(values["effects/tool_requests.json"]),
            "stored_model_output_usage": {"scope": "stored_model_output_only_not_episode_total", "available": isinstance(model_output, dict) and model_output.get("usage") is not None,
                                           "value": copy.deepcopy(model_output.get("usage")) if isinstance(model_output, dict) else None},
            "model_output_reference": {"source": sources["effects/model_output.json"], "json_pointer": "/usage"},
        })
    message_hashes = {snapshot["sources"]["messages.json"]["sha256"] for snapshot in snapshots if snapshot["sources"]["messages.json"]["status"] == "readable"}
    if len(message_hashes) > 1:
        reasons.append("conflicting_transcript_snapshots")
    if not message_hashes:
        reasons.append("no_readable_transcript")
    candidates = attempt.get("candidates")
    ledger_available = attempt.get("ledger_available") is True and isinstance(candidates, list)
    counts = {kind: sum(isinstance(candidate, dict) and candidate.get("kind") == kind for candidate in candidates) if ledger_available else None for kind in ("submit", "test")}
    counts.update(coverage="complete" if ledger_available and attempt.get("trace_complete") and attempt.get("ledger_integrity") is True else "observed_partial" if ledger_available else "unavailable",
                  outer_call_to_candidate_linkage="not_established_no_invocation_ids")
    preserved_fields = ("trace_complete", "termination", "last_stop_reason", "finish_reason", "model_cap", "sample_limit", "limits", "errors", "outcomes", "samples", "metadata_integrity", "metadata_complete", "metadata_conflicts", "metadata_absent")
    return {
        "attempt_id": attempt["attempt_id"], "verified_first_attempt": primary,
        "condition_attribution_trusted": not reasons, "attribution_issues": sorted(set(reasons)),
        "reconciled_reference": {"source": input_reference, "json_pointer": pointer},
        "preserved_attempt_fields": {field: copy.deepcopy(attempt[field]) for field in preserved_fields if field in attempt},
        "candidate_counts": counts, "candidates": copy.deepcopy(candidates),
        "retained_outer_tool_requests": copy.deepcopy(attempt.get("tool_requests")), "retained_tool_errors": copy.deepcopy(attempt.get("tool_errors")),
        "ledger_sources": [_read(Path(path) if Path(path).is_absolute() else root / path, root, raw_only=True)[0] for path in attempt.get("ledger_paths", [])],
        "snapshots": snapshots, "reported_sample_usage": usage_rows,
        "usage_completeness": "not_established_do_not_sum_duplicate_exports_or_last_call_usage",
    }


def index_evidence(output: Path, assignments: list[dict]) -> dict:
    root = Path(output).resolve()
    if not isinstance(assignments, list) or any(not isinstance(row, dict) for row in assignments):
        raise ValueError("Expected a list of reconciled assignments")
    identities = [row.get("assignment_id") for row in assignments]
    if any(not isinstance(identity, str) or not identity for identity in identities) or len(set(identities)) != len(identities):
        raise ValueError("Assignment IDs must be nonempty and unique")
    input_reference = {"kind": "provided_reconciled_assignments", "sha256": _hash(assignments)}
    csv_reference, csv_rows = _read(root / "samples.csv", root, csv_file=True)
    records = []
    for row_index, row in enumerate(assignments):
        attempts = row.get("attempts", [])
        if not isinstance(attempts, list) or any(not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str) for attempt in attempts):
            raise ValueError("Malformed reconciled attempts")
        if len({attempt["attempt_id"] for attempt in attempts}) != len(attempts):
            raise ValueError("Duplicate attempt IDs")
        primary_id = row.get("primary_attempt_id")
        first = next((attempt["attempt_id"] for attempt in attempts if attempt.get("started")), attempts[0]["attempt_id"] if attempts else None)
        primary_ok = primary_id is not None and primary_id == first and row.get("primary_order_verified") is True
        primary_evidence = None
        unattributed = []
        for attempt_index, attempt in enumerate(attempts):
            designated = primary_id is not None and attempt["attempt_id"] == primary_id
            evidence = _attempt_evidence(root, row, attempt, f"/{row_index}/attempts/{attempt_index}", input_reference, csv_reference, csv_rows, designated and primary_ok)
            if designated:
                primary_evidence = evidence
            else:
                unattributed.append(evidence)
        records.append({
            "assignment_id": row["assignment_id"], "assignment_status": row.get("status"), "primary_attempt_id": primary_id,
            "condition_metadata": {field: copy.deepcopy(row[field]) for field in CONDITION_FIELDS if field in row},
            "assignment_outcomes": copy.deepcopy(row.get("outcomes")),
            "assignment_provenance": {key: copy.deepcopy(value) for key, value in row.items() if key.startswith("metadata_") or key == "primary_order_verified"},
            "reconciled_reference": {"source": input_reference, "json_pointer": f"/{row_index}"},
            "condition_attribution_trusted": bool(primary_evidence and primary_evidence["condition_attribution_trusted"]),
            "evidence_status": "primary_evidence" if primary_evidence else "unattributed_attempts_only" if attempts else "no_attempt_evidence",
            "primary_evidence": primary_evidence, "unattributed_attempts": unattributed,
        })
    return {"version": EVIDENCE_VERSION, "semantics": "structural_evidence_only_no_awareness_truth_or_intent_labels",
            "input_reference": input_reference, "csv_reference": csv_reference, "assignments": records}


def annotation_view(index: dict, *, include_unattributed: bool = False) -> dict:
    items = []
    for row in index["assignments"]:
        attempts = ([row["primary_evidence"]] if row["primary_evidence"] else []) + (row["unattributed_attempts"] if include_unattributed else [])
        if not attempts:
            items.append({"annotation_id": _hash([row["assignment_id"], None]), "evidence_status": "no_designated_primary_evidence", "segments": []})
        for attempt in attempts:
            if not attempt["snapshots"]:
                items.append({"annotation_id": _hash([row["assignment_id"], attempt["attempt_id"], None]), "evidence_status": "no_transcript_snapshot", "segments": []})
            for snapshot_index, snapshot in enumerate(attempt["snapshots"]):
                transcript = snapshot["transcript"]
                segments = [{key: copy.deepcopy(value) for key, value in segment.items() if key not in {"source"}} for segment in transcript["segments"]]
                items.append({"annotation_id": _hash([row["assignment_id"], attempt["attempt_id"], snapshot_index]),
                              "verified_first_attempt": attempt["verified_first_attempt"], "source_attribution_verified": attempt["condition_attribution_trusted"],
                              "evidence_status": transcript["status"], "reasoning_availability": transcript["reasoning_availability"],
                              "segments": segments, "report": copy.deepcopy(transcript["report"])})
    return {"version": EVIDENCE_VERSION, "concealment": "best_effort_actor_mentions_may_reveal_condition",
            "excluded": ["prompts", "factor_metadata", "assignment_ids", "attempt_ids", "source_paths", "outcomes", "model_identity"],
            "items": items}
