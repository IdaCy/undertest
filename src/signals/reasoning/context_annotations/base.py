import copy
import hashlib
import json
import re


PROJECTOR_VERSION = "effects-annotation-episodes-v1"
EVIDENCE_VERSION = "effects-evidence-v2"
EVIDENCE_V5_SOURCE_SHA256 = "62b507db6c18f36cea10c5b9427fd9a8f0b6f4d163093117764adbc67dc39a5c"
CHANNELS = ("text", "reasoning", "reasoning_summary")
AVAILABILITIES = ("readable", "empty", "redacted", "unavailable_field", "unresolved_attachment")
REASONING_AVAILABILITIES = (
    "unavailable_transcript", "unavailable_incomplete_transcript", "not_returned_or_not_recorded",
    "readable_returned_blocks", "partially_readable_returned_blocks", "redacted_returned_blocks",
    "returned_blocks_without_readable_reasoning",
)
NONCONFLICT_ISSUES = {
    "assignment_metadata_integrity_unverified", "assignment_metadata_complete_unverified",
    "assignment_metadata_complete_false", "assignment_metadata_absent",
    "attempt_metadata_integrity_unverified", "attempt_metadata_complete_unverified",
    "attempt_metadata_complete_false", "attempt_metadata_absent",
    "unavailable_or_invalid_transcript", "no_readable_transcript", "unresolved_sample_artifact_path",
    "artifact_sample_metadata_unavailable", "csv_sample_metadata_unavailable",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _identity(value):
    return type(value) is str and bool(value)


def _nonnegative(value):
    return type(value) is int and value >= 0


def _segment(segment, reference, annotation_id):
    if type(segment) is not dict or segment.get("kind") not in CHANNELS:
        raise ValueError("Invalid indexed actor segment")
    kind, message, content = segment["kind"], segment.get("message_index"), segment.get("content_index")
    if not _nonnegative(message) or (content is not None and not _nonnegative(content)):
        raise ValueError("Invalid indexed segment coordinates")
    field = "content" if content is None else {"text": "text", "reasoning": "reasoning", "reasoning_summary": "summary"}[kind]
    pointer = f"/{message}/content" + (f"/{content}/{field}" if content is not None else "")
    if content is None and kind != "text":
        raise ValueError("Typed reasoning requires an indexed content block")
    native_id = hashlib.sha256(json.dumps([reference["sha256"], pointer, kind], sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    if (segment.get("json_pointer") != pointer or segment.get("field") != field
            or segment.get("segment_id") != native_id or segment.get("source") != reference
            or segment.get("availability") not in AVAILABILITIES):
        raise ValueError("Indexed segment provenance or channel mismatch")
    availability = segment["availability"]
    value = segment.get("value")
    if ((availability == "readable" and (type(value) is not str or not value or value.startswith("attachment://")))
            or (availability == "empty" and value != "")):
        raise ValueError("Indexed readable segment has no exact source text")
    return {"segment_id": _hash([PROJECTOR_VERSION, annotation_id, reference["sha256"], pointer, kind]),
            "kind": kind, "message_index": message, "content_index": content, "field": field,
            "json_pointer": pointer, "availability": availability,
            "value": value if availability in ("readable", "empty") else None}


def _channels(segments, transcript=None):
    channels = {}
    for kind in CHANNELS:
        selected = [segment for segment in segments if segment["kind"] == kind]
        counts = {state: sum(segment["availability"] == state for segment in selected) for state in AVAILABILITIES}
        channels[kind] = {
            "returned_segment_count": len(selected), "availability_counts": counts,
            "status": "returned_segments" if selected else "not_returned_or_not_recorded" if transcript and transcript["status"] == "available" else "unavailable_or_incomplete_transcript",
            "full_channel_capture": "not_established",
        }
    channels["reasoning"]["index_availability"] = transcript["reasoning_availability"] if transcript else "unavailable_transcript"
    return channels


def _report_candidates(transcript, segment_map):
    report = transcript["report"]
    boundary = report.get("last_tool_message_index")
    if boundary is not None and not _nonnegative(boundary):
        raise ValueError("Invalid structural tool boundary")
    grouped = {}
    for segment in transcript["segments"]:
        if (segment["kind"] == "text" and segment["availability"] == "readable" and segment["value"].strip()
                and (boundary is None or segment["message_index"] > boundary)):
            grouped.setdefault(segment["message_index"], []).append(segment_map[segment["segment_id"]])
    return [{"message_index": message, "segment_ids": ids} for message, ids in sorted(grouped.items())]


def _snapshot(snapshot, annotation_id):
    sources = snapshot.get("sources")
    if type(sources) is not dict or type(sources.get("messages.json")) is not dict:
        raise ValueError("Missing transcript source reference")
    reference = sources["messages.json"]
    transcript = snapshot.get("transcript")
    if not _identity(reference.get("path")) or not _identity(reference.get("status")) or type(transcript) is not dict:
        raise ValueError("Invalid transcript inventory")
    result = {"source": copy.deepcopy(reference), "readable_bytes": reference["status"] == "readable",
              "transcript_status": transcript.get("status"), "segments": [], "segment_map": {}, "report_candidates": []}
    if not result["readable_bytes"]:
        if transcript.get("segments") or transcript.get("status") != "unavailable":
            raise ValueError("Unreadable transcript unexpectedly has extracted text")
        return result
    if not _sha(reference.get("sha256")) or not _nonnegative(reference.get("bytes")):
        raise ValueError("Readable transcript lacks exact byte identity")
    if (transcript.get("status") not in ("available", "partially_available", "unavailable")
            or type(transcript.get("segments")) is not list or type(transcript.get("report")) is not dict
            or transcript.get("reasoning_availability") not in REASONING_AVAILABILITIES):
        raise ValueError("Invalid indexed transcript schema")
    for field in ("schema_issues", "unsupported_content_blocks", "outer_tool_calls", "tool_errors"):
        if type(transcript.get(field)) is not list:
            raise ValueError("Invalid indexed transcript collections")
    if (transcript["status"] == "available" and transcript["schema_issues"]
            or transcript["status"] == "unavailable" and transcript["segments"]):
        raise ValueError("Transcript status disagrees with extracted surface")
    for segment in transcript["segments"]:
        projected = _segment(segment, reference, annotation_id)
        if segment["segment_id"] in result["segment_map"]:
            raise ValueError("Duplicate source segment identity")
        result["segments"].append(projected)
        result["segment_map"][segment["segment_id"]] = projected["segment_id"]
    report = transcript["report"]
    if type(report.get("segment_ids")) is not list or type(report.get("ambiguous")) is not bool:
        raise ValueError("Invalid indexed structural report")
    if any(identity not in result["segment_map"] for identity in report["segment_ids"]):
        raise ValueError("Unknown structural report segment")
    result["report_candidates"] = _report_candidates(transcript, result["segment_map"])
    normalized = copy.deepcopy(transcript)
    for segment in normalized["segments"]:
        del segment["source"]
    result["projection_sha256"] = _hash(normalized)
    return result


def _provenance_issues(row, attempt):
    reasons = []
    flags = (row.get("assignment_provenance", {}), attempt.get("preserved_attempt_fields", {}))
    for scope, values in zip(("assignment", "attempt"), flags):
        if type(values) is not dict:
            reasons.append(scope + "_provenance_unavailable")
        elif values.get("metadata_integrity") is False or values.get("metadata_conflicts"):
            reasons.append(scope + "_identity_conflict")
    if row.get("assignment_status") == "metadata_conflict" or (type(flags[1]) is dict and flags[1].get("termination") == "metadata_conflict"):
        reasons.append("recorded_metadata_conflict")
    issues = attempt.get("attribution_issues")
    if type(issues) is not list or any(type(issue) is not str for issue in issues):
        reasons.append("invalid_native_attribution_issues")
    else:
        reasons.extend(issue for issue in issues if issue not in NONCONFLICT_ISSUES)
    if row.get("condition_attribution_trusted") is True or attempt.get("condition_attribution_trusted") is True:
        if (row.get("condition_attribution_trusted") is not True or attempt.get("condition_attribution_trusted") is not True
                or issues or any(type(values) is not dict or values.get("metadata_integrity") is not True
                                or values.get("metadata_complete") is not True or values.get("metadata_absent") for values in flags)):
            reasons.append("native_attribution_flags_inconsistent")
    if type(row.get("condition_metadata")) is not dict or row["condition_metadata"].get("assignment_id", row["assignment_id"]) != row["assignment_id"]:
        reasons.append("assignment_identity_mismatch")
    return sorted(set(reasons))


def _natural_end(attempt):
    fields = attempt.get("preserved_attempt_fields", {})
    return (fields.get("trace_complete") is True and fields.get("termination") == "natural"
            and fields.get("model_cap") is False and fields.get("sample_limit") is False and fields.get("errors") == [])


def _project_row(row, assignment_id, schedule_sha256):
    attempt = row.get("primary_evidence") if row else None
    retries = row.get("unattributed_attempts", []) if row else []
    primary_ok = bool(type(attempt) is dict and _identity(attempt.get("attempt_id"))
                      and row.get("primary_attempt_id") == attempt["attempt_id"]
                      and attempt.get("verified_first_attempt") is True
                      and row.get("assignment_provenance", {}).get("primary_order_verified") is True)
    verified_id = attempt["attempt_id"] if primary_ok else None
    annotation_id = _hash([PROJECTOR_VERSION, schedule_sha256, assignment_id, verified_id])
    item = {"annotation_id": annotation_id, "evidence_status": "unavailable", "segments": [],
            "channels": _channels([]), "provided_surface_coverage": "unavailable",
            "episode_wide_absence": "not_established",
            "report": {"status": "unavailable", "segment_ids": [], "structural_only": True}}
    audit = {"annotation_id": annotation_id, "assignment_id": assignment_id, "verified_attempt_id": verified_id,
             "original_index_row": copy.deepcopy(row), "selection_issues": [], "snapshot_dispositions": [],
             "canonical_source": None, "native_index_attribution_trusted": row.get("condition_attribution_trusted") if row else None,
             "native_attempt_attribution_trusted": attempt.get("condition_attribution_trusted") if type(attempt) is dict else None,
             "native_linkage_eligible": False,
             "separate_receipt_gate": {"status": "missing_not_implemented", "verified": False},
             "family_eligibility_override_applied": False,
             "retry_disposition": "retained_in_owner_audit_never_selected", "unattributed_attempt_count": len(retries)}
    if not primary_ok:
        audit["selection_issues"] = ["missing_index_record" if row is None else "no_attempt_evidence" if not attempt and not retries else "unverified_or_unknown_primary"]
        return item, audit
    reasons = _provenance_issues(row, attempt)
    attempt_ids = [attempt["attempt_id"]] + [value.get("attempt_id") for value in retries if type(value) is dict]
    if len(set(attempt_ids)) != len(attempt_ids) or any(value.get("verified_first_attempt") is True for value in retries if type(value) is dict):
        reasons.append("conflicting_first_attempt_identity")
    snapshots = attempt.get("snapshots")
    if type(snapshots) is not list:
        reasons.append("invalid_snapshot_inventory")
        snapshots = []
    parsed = []
    for index, snapshot in enumerate(snapshots):
        disposition = {"snapshot_index": index, "sources": copy.deepcopy(snapshot.get("sources")) if type(snapshot) is dict else None,
                       "disposition": "retained_not_selected"}
        audit["snapshot_dispositions"].append(disposition)
        try:
            value = _snapshot(snapshot, annotation_id)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            reasons.append("invalid_indexed_snapshot")
            disposition.update(disposition="invalid_indexed_snapshot", error=str(exc))
            continue
        value["snapshot_index"] = index
        parsed.append(value)
        disposition["structural_report_candidates"] = value["report_candidates"]
        disposition["index_to_projected_segment_ids"] = value["segment_map"]
        disposition["disposition"] = "readable_candidate" if value["readable_bytes"] else "missing_or_unreadable_optional_snapshot"
    readable = [value for value in parsed if value["readable_bytes"]]
    if len(readable) > 1 and any(issue in attempt.get("attribution_issues", []) for issue in (
            "artifact_sample_metadata_unavailable", "csv_sample_metadata_unavailable", "unresolved_sample_artifact_path")):
        reasons.append("duplicate_provenance_compatibility_unverified")
    hashes = sorted({value["source"]["sha256"] for value in readable})
    audit["readable_transcript_sha256s"] = hashes
    if len(hashes) > 1:
        reasons.append("differing_readable_transcript_bytes")
    if readable and (len({value["source"]["bytes"] for value in readable}) > 1
                     or len({value["projection_sha256"] for value in readable}) > 1) and len(hashes) == 1:
        reasons.append("same_hash_inconsistent_index_projection")
    if not readable:
        reasons.append("no_readable_transcript")
    if reasons:
        audit["selection_issues"] = sorted(set(reasons))
        for value in readable:
            audit["snapshot_dispositions"][value["snapshot_index"]]["disposition"] = "rejected_canonical_selection"
        return item, audit
    representative = min(readable, key=lambda value: (value["source"]["path"], value["snapshot_index"]))
    selected_index = representative["snapshot_index"]
    transcript = snapshots[selected_index]["transcript"]
    if transcript["status"] == "unavailable":
        audit["selection_issues"] = ["unavailable_required_transcript"]
        return item, audit
    for value in readable:
        audit["snapshot_dispositions"][value["snapshot_index"]]["disposition"] = "canonical_representative" if value is representative else "identical_compatible_duplicate"
    audit["canonical_source"] = copy.deepcopy(representative["source"])
    audit["native_linkage_eligible"] = row.get("condition_attribution_trusted") is True and attempt.get("condition_attribution_trusted") is True
    segments = representative["segments"]
    snapshot_coverage = len(readable) == len(snapshots) and not any(issue in attempt.get("attribution_issues", []) for issue in (
        "unresolved_sample_artifact_path", "unavailable_or_invalid_transcript", "no_readable_transcript"))
    complete_surface = (transcript["status"] == "available" and not transcript["unsupported_content_blocks"]
                        and all(segment["availability"] in ("readable", "empty") for segment in segments)
                        and snapshot_coverage and _natural_end(attempt))
    item.update(evidence_status="available" if complete_surface else "partial", segments=segments,
                channels=_channels(segments, transcript), provided_surface_coverage="complete_recorded_surface" if complete_surface else "partial_or_unestablished")
    report = transcript["report"]
    report_complete = (transcript["status"] == "available" and not transcript["unsupported_content_blocks"]
                       and all(segment["availability"] in ("readable", "empty") for segment in segments if segment["kind"] == "text")
                       and snapshot_coverage and _natural_end(attempt))
    candidates = representative["report_candidates"]
    report_ids = [representative["segment_map"][value] for value in report["segment_ids"]]
    if report_complete and not report["ambiguous"] and len(candidates) == 1 and candidates[0]["segment_ids"] == report_ids:
        item["report"].update(status="structural_candidate", segment_ids=report_ids)
    elif report_complete and not candidates and report.get("status") == "no_text_after_last_tool" and not report.get("ambiguity_reasons"):
        item["report"].update(status="no_report")
    else:
        item["report"].update(status="unavailable_ambiguous_or_incomplete")
    audit["projected_segment_sources"] = [{"segment_id": projected["segment_id"], "index_segment_id": source["segment_id"],
                                            "source": copy.deepcopy(source["source"]), "json_pointer": source["json_pointer"]}
                                           for projected, source in zip(segments, transcript["segments"])]
    return item, audit


def project_annotation_episodes(index, schedule_identity):
    if (type(schedule_identity) is not dict or set(schedule_identity) != {"schedule_sha256", "assignment_ids"}
            or not _sha(schedule_identity["schedule_sha256"]) or type(schedule_identity["assignment_ids"]) is not list
            or any(not _identity(value) for value in schedule_identity["assignment_ids"])
            or len(set(schedule_identity["assignment_ids"])) != len(schedule_identity["assignment_ids"])):
        raise ValueError("Expected an exact schedule hash and unique ordered assigned IDs")
    if (type(index) is not dict or index.get("version") != EVIDENCE_VERSION
            or index.get("semantics") != "structural_evidence_only_no_awareness_truth_or_intent_labels"
            or type(index.get("assignments")) is not list):
        raise ValueError("Expected the reviewed v5 effects-evidence inventory schema")
    try:
        index_sha256 = _hash(index)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Index must be finite JSON") from exc
    rows = {}
    for row in index["assignments"]:
        if type(row) is not dict or not _identity(row.get("assignment_id")) or row["assignment_id"] in rows:
            raise ValueError("Index assignment identities must be nonempty and unique")
        if type(row.get("unattributed_attempts")) is not list or type(row.get("assignment_provenance")) is not dict:
            raise ValueError("Malformed indexed assignment provenance")
        rows[row["assignment_id"]] = row
    if set(rows) - set(schedule_identity["assignment_ids"]):
        raise ValueError("Index has assignments outside the declared schedule")
    items, audits = [], []
    for identity in schedule_identity["assignment_ids"]:
        item, audit = _project_row(rows.get(identity), identity, schedule_identity["schedule_sha256"])
        items.append(item)
        audits.append(audit)
    construct = {"version": PROJECTOR_VERSION, "unit": "assigned_episode", "assigned_count": len(items),
                 "concealment": "best_effort_actor_mentions_may_reveal_condition",
                 "text_is_untrusted_quoted_data": True, "items": items}
    owner = {"version": PROJECTOR_VERSION, "schedule_identity": copy.deepcopy(schedule_identity),
             "input_index_canonical_sha256": index_sha256, "input_reference": copy.deepcopy(index.get("input_reference")),
             "csv_reference": copy.deepcopy(index.get("csv_reference")),
             "required_evidence_release": "evidence_index_v5", "required_evidence_source_sha256": EVIDENCE_V5_SOURCE_SHA256,
             "producer_source_attestation": "external_owner_gate_not_verified_from_index_version_string",
             "assigned_count": len(items), "episodes": audits}
    return {"version": PROJECTOR_VERSION, "construct_view": construct, "owner_audit": owner}
