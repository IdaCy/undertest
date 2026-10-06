import copy
import hashlib
import importlib
import json
from collections import Counter
from pathlib import Path

from characteristics.downstream_effects.recipe_controls_secondary import summarize_control_secondary
from causes.recipe_controls.tasks.banks import BANK_OPERATIONS
from causes.recipe_controls.tasks.banks import structural_last_bank


HISTORY_VERSION = "effects-control-history-v1"
KINDS = ("all", "test", "submit")
FIRST_CATEGORIES = ("A", "B", "none", "unknown")
OBSERVED_FIRST_CATEGORIES = ("A", "B", "not_observed", "unknown")
FINAL_CATEGORIES = ("bank_A", "bank_B", "valid_no_bank", "invalid", "missing", "unknown")
COUNT_FIELDS = (
    "candidates", "test_candidates", "submit_candidates", "valid_candidates", "invalid_candidates",
    "bank_occurrences", "bank_A_occurrences", "bank_B_occurrences", "tested_bank_occurrences", "submitted_bank_occurrences",
)


def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _source_hashes(paths):
    return {path: hashlib.sha256(Path(importlib.import_module(path[4:-3].replace("/", ".")).__file__).read_bytes()).hexdigest()
            for path in paths}


def _tested_only(outcomes):
    tested, submitted = outcomes["tested_shortcut_anytime"], outcomes["submitted_shortcut_anytime"]
    return 0 if tested == 0 or submitted == 1 else 1 if tested == 1 and submitted == 0 else None


def _counts(candidates, occurrences):
    return {
        "candidates": len(candidates),
        "test_candidates": sum(c["action"] == "test" for c in candidates),
        "submit_candidates": sum(c["action"] == "submit" for c in candidates),
        "valid_candidates": sum(c["valid"] for c in candidates),
        "invalid_candidates": sum(not c["valid"] for c in candidates),
        "bank_occurrences": len(occurrences),
        "bank_A_occurrences": sum(o["bank"] == "A" for o in occurrences),
        "bank_B_occurrences": sum(o["bank"] == "B" for o in occurrences),
        "tested_bank_occurrences": sum(o["action"] == "test" for o in occurrences),
        "submitted_bank_occurrences": sum(o["action"] == "submit" for o in occurrences),
    }


def _record(assignment, secondary, attempt, source_references):
    linked = secondary["provenance_linked"]
    complete = linked and attempt["trace_complete"] and attempt["episode_end_observed"]
    record = {
        **{key: assignment[key] for key in ("assignment_id", "family", "instance_id", "cell_id", "rollout", "assignment_index")},
        "attempt_id": secondary["attempt_id"], "provenance_linked": linked,
        "history_status": "complete" if complete else "partial" if linked else "unavailable",
        "trace_complete": attempt["trace_complete"] if linked else None,
        "episode_end_observed": attempt["episode_end_observed"] if linked else None,
        "complete_history_unknown_reasons": [] if complete else ["complete_terminal_history_required"] if linked else
            sorted({reason for reasons in secondary["unknown_reasons"].values() for reason in reasons}),
        "candidates": None, "invalid_candidate_indices": None, "bank_occurrences": None,
        "observed_counts": None, "complete_counts": None,
        "observed_first_banks": dict.fromkeys(KINDS, "unknown"),
        "first_banks": dict.fromkeys(KINDS, "unknown"),
        "final_artifact": {"category": "unknown", "structural_last_bank": None, "candidate_index": None, "raw_recipe_sha256": None},
        "tested_only_shortcut": _tested_only(secondary["outcomes"]),
        "secondary_assignment_audit_sha256": _digest(secondary),
    }
    if not linked:
        return record
    stream = attempt["ledgers"][0]
    request_indices = [index for index, value in enumerate(stream["records"]) if value.get("phase") == "request"]
    candidates, occurrences = [], []
    for event, request_index in zip(attempt["replayed_candidates"], request_indices, strict=True):
        raw = event["raw"].encode("utf-8", errors="surrogatepass")
        candidate = {
            "candidate_index": event["index"], "action": event["kind"], "valid": event["valid"],
            "raw_recipe_sha256": hashlib.sha256(raw).hexdigest(), "raw_recipe_bytes": len(raw),
            "raw_request_reference": {"path": stream["source"], "sha256": source_references[stream["source"]], "record_index": request_index},
            "bank_occurrences": None, "structural_last_bank": None,
            "parse_error": event.get("error") if not event["valid"] else None,
        }
        if event["valid"]:
            candidate["bank_occurrences"] = [
                {"candidate_index": event["index"], "step_index": index, "action": event["kind"], "operation": step["op"], "bank": BANK_OPERATIONS[step["op"]]}
                for index, step in enumerate(event["recipe"]["steps"]) if step["op"] in BANK_OPERATIONS
            ]
            candidate["structural_last_bank"] = structural_last_bank(event["recipe"])
            occurrences.extend(copy.deepcopy(candidate["bank_occurrences"]))
        candidates.append(candidate)
    record.update(candidates=candidates, invalid_candidate_indices=[c["candidate_index"] for c in candidates if not c["valid"]],
                  bank_occurrences=occurrences, observed_counts=_counts(candidates, occurrences))
    for kind in KINDS:
        first = next((o["bank"] for o in occurrences if kind == "all" or o["action"] == kind), None)
        record["observed_first_banks"][kind] = first if first is not None else "not_observed"
        if complete:
            record["first_banks"][kind] = first if first is not None else "none"
    if complete:
        record["complete_counts"] = dict(record["observed_counts"])
        final = next((c for c in reversed(candidates) if c["action"] == "submit"), None)
        record["final_artifact"] = {
            "category": attempt["replay"]["final_category"],
            "structural_last_bank": final["structural_last_bank"] if final is not None else None,
            "candidate_index": final["candidate_index"] if final is not None else None,
            "raw_recipe_sha256": final["raw_recipe_sha256"] if final is not None else None,
        }
    return record


def _describe(records):
    linked = sum(r["provenance_linked"] for r in records)
    complete = sum(r["history_status"] == "complete" for r in records)
    return {
        "assigned": len(records), "linked": linked, "unlinked": len(records) - linked,
        "complete": complete, "partial": linked - complete, "unknown_complete_history": len(records) - complete,
        "observed_count_sums": {name: sum(r["observed_counts"][name] for r in records if r["observed_counts"] is not None) for name in COUNT_FIELDS},
        "complete_history_count_sums": {name: sum(r["complete_counts"][name] for r in records if r["complete_counts"] is not None) for name in COUNT_FIELDS},
        "observed_first_bank_counts": {kind: {value: sum(r["observed_first_banks"][kind] == value for r in records) for value in OBSERVED_FIRST_CATEGORIES} for kind in KINDS},
        "first_bank_counts": {kind: {value: sum(r["first_banks"][kind] == value for r in records) for value in FIRST_CATEGORIES} for kind in KINDS},
        "final_category_counts": {value: sum(r["final_artifact"]["category"] == value for r in records) for value in FINAL_CATEGORIES},
        "tested_only_shortcut_counts": {str(value) if value is not None else "unknown": sum(r["tested_only_shortcut"] == value for r in records) for value in (0, 1, None)},
        "complete_history_unknown_reason_counts": dict(sorted(Counter(reason for r in records for reason in r["complete_history_unknown_reasons"]).items())),
    }


def summarize_control_history(design, reconciled):
    secondary = summarize_control_secondary(design, reconciled)
    if design["manifest"]["study_id"] != "C2":
        raise ValueError("Bank/action history reporting requires a C2 design")
    references = copy.deepcopy(secondary["input_references"])
    expected_sources = references["implementation_source_sha256"]
    sources = _source_hashes([*expected_sources, "src/causes/recipe_controls/controls/history.py"])
    if any(sources[name] != digest for name, digest in expected_sources.items()):
        raise ValueError("Implementation sources changed after secondary validation")
    audits = {r["assignment_id"]: r for r in reconciled["audit"]["assignments"]}
    secondary_records = {r["assignment_id"]: r for r in secondary["assignment_audit"]}
    source_references = {r["path"]: r["sha256"] for r in reconciled["source_references"]}
    records = []
    for assignment in design["assignments"]:
        aid = assignment["assignment_id"]
        selected = secondary_records[aid]
        attempt = next((r for r in audits[aid]["attempts"] if r["attempt_id"] == selected["attempt_id"]), None) if selected["provenance_linked"] else None
        records.append(_record(assignment, selected, attempt, source_references))
    cells = tuple(design["manifest"]["contrast_specification"]["contrasts"][0]["coefficients"])
    references.update(secondary_summary_sha256=secondary["summary_sha256"], implementation_source_sha256=sources)
    result = {
        "history_version": HISTORY_VERSION, "claim_status": "finite_panel_history_descriptive_only",
        **{key: design["manifest"][key] for key in ("study_id", "stage", "schedule_id")},
        "assignment_audit": records,
        "descriptive_counts": {
            "all_assigned": _describe(records),
            "cells": {cell: _describe([r for r in records if r["cell_id"] == cell]) for cell in cells},
            "tasks": {instance: {"cells": {cell: _describe([r for r in records if r["instance_id"] == instance and r["cell_id"] == cell]) for cell in cells}}
                      for instance in sorted({r["instance_id"] for r in records})},
        },
        "input_references": references,
        "definitions": {
            "bank_occurrences": "valid_recipe_syntax_in_candidate_then_step_order_not_executed_lookups",
            "raw_recipe_digest": "sha256_of_request_recipe_text_encoded_utf8_with_surrogatepass_not_raw_archive_bytes",
            "indices": "zero_based_original_candidate_recipe_step_and_durable_record_indices",
            "observed_first_banks": "first_bank_in_linked_observed_history_not_a_complete_episode_first_label",
            "first_banks_and_complete_counts": "require_verified_complete_action_capture_and_observed_episode_end",
            "none": "complete_history_without_a_valid_bank_occurrence_for_the_action_kind",
            "not_observed": "no_valid_bank_occurrence_in_linked_observed_history_for_the_action_kind",
            "invalid_candidate_inventory": "null_not_an_interpretation_of_bank_names_in_invalid_text",
            "tested_only_shortcut": "partial_information_conjunction_of_secondary_tested_shortcut_anytime_and_not_submitted_shortcut_anytime",
            "count_sums": "observed_sums_cover_linked_records_complete_sums_cover_complete_records_neither_imputes_unavailable_actions",
            "other_outcomes": "repair_success_exploitation_and_compliance_remain_in_the_digest_bound_secondary_summary",
        },
        "limitations": ["no_retry_substitution", "source_references_structurally_validated_not_raw_files_authenticated",
                        "partial_observed_history_is_not_complete_episode_history", "structural_bank_choice_is_not_execution_success",
                        "no_new_contrasts_intervals_tests_or_population_inference", "no_atomic_live_snapshot"],
    }
    if sources != _source_hashes(sources):
        raise ValueError("Implementation sources changed during history analysis")
    result["summary_sha256"] = _digest(result)
    return result
