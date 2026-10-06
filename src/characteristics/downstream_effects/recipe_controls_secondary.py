import copy
import hashlib
import importlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from causes.recipe_controls.controls.analysis import summarize_control_design
from causes.recipe_controls.controls.design import verify_control_design
from causes.recipe_controls.controls.reconcile import RECONCILIATION_VERSION
from causes.recipe_controls.tasks.control_task import assess_control_ledger
from causes.recipe_controls.tasks.control_task import fixture_for_assignment
from causes.recipe_controls.tasks.task import OUTCOMES
from causes.recipe_controls.tasks.task import SYSTEM


SECONDARY_VERSION = "effects-control-secondary-v1"
SUPPORTED_RECONCILIATION = "effects-control-reconcile-v2"
FIXED_BANK_OUTCOMES = ("bank_A_success", "bank_B_success", "fixed_bank_A_exploitation", "fixed_bank_B_exploitation")
CONDITION_DEPENDENT = {"proxy_success", "successful_exploitation"}
DIAGNOSTICS = ("error", "sample_limit", "model_cap", "termination")


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected finite JSON data") from exc


def _same(left, right):
    return _canonical(left) == _canonical(right)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _identifier(value):
    return type(value) is str and bool(value)


def _sources():
    names = ("characteristics.downstream_effects.recipe_controls_secondary", "causes.recipe_controls.controls.reconcile", "causes.recipe_controls.controls.analysis",
             "causes.recipe_controls.controls.design", "causes.recipe_controls.tasks.control_task")
    return {"src/" + name.replace(".", "/") + ".py": hashlib.sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()
            for name in names}


def _timestamp(start):
    try:
        value = datetime.fromisoformat(start["started_at_utc"].replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo is not None else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _mapping(values, planned, label):
    _require(type(values) is list, label + " must be a list")
    result = {}
    for value in values:
        _require(type(value) is dict and _identifier(value.get("assignment_id")) and value["assignment_id"] in planned, "Unknown " + label + " assignment")
        identity = value["assignment_id"]
        _require(identity not in result, "Duplicate " + label + " assignment")
        result[identity] = value
    return result


def _validate_sources(reconciled):
    references = reconciled.get("source_references")
    inventory = reconciled.get("source_inventory")
    _require(type(references) is list and type(inventory) is list, "Source references and inventory are required")
    paths = {}
    for item in inventory:
        _require(type(item) is dict and _identifier(item.get("path")), "Malformed source inventory")
        _require(_identifier(item.get("status")) and item["status"] in {"readable", "absent", "unreadable", "malformed"}, "Unknown source inventory status")
        if "sha256" in item:
            _require(type(item.get("bytes")) is int and item["bytes"] >= 0 and item["status"] in {"readable", "malformed"}, "Invalid readable source inventory")
        _require(item["path"] not in paths, "Duplicate source inventory path")
        paths[item["path"]] = item
    declared = {}
    for item in references:
        _require(type(item) is dict and set(item) == {"path", "sha256"} and _identifier(item["path"]), "Malformed source reference")
        _require(item["path"] not in declared, "Duplicate source reference path")
        declared[item["path"]] = item["sha256"]
    _require(_same(declared, {path: item["sha256"] for path, item in paths.items() if "sha256" in item}), "Source inventory/reference disagreement")
    return declared


def _validate_audits(design, reconciled, rows):
    planned = {a["assignment_id"]: a for a in design["assignments"]}
    audit = reconciled.get("audit")
    _require(type(audit) is dict and type(audit.get("global_order_uncertainty")) is list, "Malformed reconciliation audit")
    assignments = _mapping(audit.get("assignments"), planned, "audit")
    seen_attempts = set()
    for aid, entry in assignments.items():
        _require(type(entry.get("attempts")) is list and type(entry.get("first_started_order_verified")) is bool, "Malformed audit attempts/order")
        _require(type(entry.get("metadata_conflicts")) is list and type(entry.get("metadata_absent")) is list, "Missing audit provenance lists")
        attempts = {}
        timestamps = {}
        for attempt in entry["attempts"]:
            _require(type(attempt) is dict and attempt.get("assignment_id") == aid and _identifier(attempt.get("attempt_id")), "Crosslinked audit attempt")
            tid = attempt["attempt_id"]
            _require(tid not in seen_attempts, "Duplicate or crosslinked audit attempt ID")
            seen_attempts.add(tid)
            attempts[tid] = attempt
            for key in ("starts", "ledgers", "bundles", "metadata_sources", "samples", "conflicts", "incomplete", "source_paths", "verified_prompt_sources"):
                _require(type(attempt.get(key)) is list, "Malformed audit attempt field: " + key)
            for key in ("trace_complete", "episode_end_observed"):
                _require(type(attempt.get(key)) is bool, "Malformed audit completion flag")
            _require(all(_identifier(item) for item in attempt["verified_prompt_sources"]), "Malformed verified prompt reference")
            _require(all(type(item) is dict for item in attempt["metadata_sources"]), "Malformed captured metadata source")
            _require(all(_identifier(item.get("status")) and item["status"] in {"present", "absent"} and _identifier(item.get("source")) for item in attempt["metadata_sources"]), "Malformed metadata source status")
            _require(type(attempt.get("diagnostics")) is dict, "Missing audit diagnostics")
            starts = attempt["starts"]
            if len(starts) == 1 and isinstance(starts[0], dict):
                start = starts[0]
                if start.get("assignment_id") == aid and start.get("attempt_id") == tid:
                    timestamps[tid] = _timestamp(start)
        first = rows.get(aid, {}).get("first_attempt")
        primary = entry.get("primary_attempt_id")
        if entry["first_started_order_verified"]:
            _require(not audit["global_order_uncertainty"], "Verified order contradicts global uncertainty")
            _require(attempts and len(timestamps) == len(attempts) and all(x is not None for x in timestamps.values())
                     and len(set(timestamps.values())) == len(attempts), "Verified order lacks unique durable starts")
            _require(primary == min(timestamps, key=timestamps.get), "Designated primary is not the earliest durable attempt")
        else:
            _require(primary is None, "Unverified order cannot designate a primary attempt")
        if aid not in rows:
            continue
        if first is None:
            _require(primary is None, "Primary row/audit attempt disagreement")
        else:
            _require(primary == first["attempt_id"] and primary in attempts, "Primary row/audit attempt disagreement")
            selected = attempts[primary]
            for key in ("trace_complete", "episode_end_observed"):
                _require(first.get(key) == selected[key], "Primary row/audit completion disagreement")
            for key in DIAGNOSTICS:
                _require(_same(first.get(key), selected["diagnostics"].get(key)), "Primary row/audit diagnostic disagreement")
            conflicts = entry["metadata_conflicts"] + selected["conflicts"]
            _require(_same(first.get("metadata_conflicts"), conflicts) and _same(first.get("metadata_absent"), entry["metadata_absent"]), "Primary row/audit provenance disagreement")
            _require(first.get("metadata_integrity") is (not conflicts) and first.get("metadata_complete") is (not entry["metadata_absent"]), "Provenance flags contradict their evidence")
            _require(first.get("first_started_attempt_verified") is True, "Primary row has unverified first-attempt identity")
            expected_verified = selected.get("replay") is not None and not selected["conflicts"]
            _require(first.get("outcome_verified") is expected_verified, "Outcome verification flag contradicts audit")
    return assignments


def _prompts(attempt, assignment, design):
    captures = []
    for bundle in attempt["bundles"]:
        _require(type(bundle) is dict and type(bundle.get("values")) is dict and type(bundle.get("sources")) is dict, "Malformed captured bundle")
        if bundle["values"].get("messages") is not None:
            captures.append((bundle["sources"].get("messages"), bundle["values"]["messages"]))
    for sample in attempt["samples"]:
        _require(type(sample) is dict and _identifier(sample.get("format")) and sample["format"] in {"csv", "inspect"} and type(sample.get("value")) is dict, "Malformed captured sample")
        if sample["format"] == "inspect":
            _require(sample["value"].get("input") == design["inputs"][assignment["assignment_id"]], "Raw sample input disagrees with design")
            raw_messages = sample["value"].get("messages")
            if raw_messages is not None:
                source = sample["source"] + ":messages"
                copies = [messages for name, messages in captures if name == source]
                _require(copies and all(_same(messages, raw_messages) for messages in copies), "Raw sample transcript disagrees with captured bundle")
            for index, event in enumerate(sample["value"].get("events") or []):
                if isinstance(event, dict) and event.get("event") == "model":
                    captures.append((f"{sample['source']}:events/{index}/input", event.get("input")))
    valid = []
    expected = [("system", SYSTEM), ("user", design["inputs"][assignment["assignment_id"]])]
    for source, messages in captures:
        if (type(messages) is not list or len(messages) < 2 or any(type(m) is not dict for m in messages)
                or [(m.get("role"), m.get("content")) for m in messages[:2]] != expected
                or any(m.get("role") in {"system", "user"} for m in messages[2:])):
            return False
        _require(_identifier(source), "Missing prompt source identity")
        valid.append(source)
    _require(Counter(valid) == Counter(attempt["verified_prompt_sources"]), "Verified prompt references disagree with captured prompts")
    return bool(valid)


def _replay(assignment, attempt, references):
    _require(len(attempt["ledgers"]) == 1, "Verified replay requires one durable ledger")
    stream = attempt["ledgers"][0]
    _require(type(stream) is dict and stream.get("source") in references and type(stream.get("records")) is list, "Durable replay lacks bound source records")
    records = stream["records"]
    _require(records and _same(records[0], attempt["starts"][0]), "Durable header disagrees with attempt identity")
    _require(_same(records[0].get("metadata"), assignment), "Durable header metadata disagrees with design")
    _require(type(stream.get("parse_errors")) is list and all(type(e) is dict and e.get("terminal_partial") is True for e in stream["parse_errors"]), "Verified replay has nonterminal parse errors")
    engine, fixture = fixture_for_assignment(assignment)
    candidates, final_raw = [], None
    pending, cleanup = None, None
    stream_complete = not stream["parse_errors"]
    for record in records[1:]:
        _require(type(record) is dict, "Malformed durable record")
        _require(cleanup is None, "Verified replay has records after cleanup")
        phase = record.get("phase")
        if phase == "result":
            _require(pending is not None and all(key in record and _same(record[key], val) for key, val in pending.items()), "Durable result disagrees with request replay")
            pending = None
            continue
        if phase == "cleanup":
            cleanup = record
            stream_complete = stream_complete and pending is None
            continue
        _require(phase == "request", "Unknown durable replay phase")
        if pending is not None:
            stream_complete = False
        _require(type(record.get("index")) is int and record["index"] == len(candidates)
                 and _identifier(record.get("kind")) and record["kind"] in {"submit", "test"} and type(record.get("raw")) is str, "Malformed durable candidate request")
        event = {key: record[key] for key in ("index", "kind", "raw")}
        try:
            recipe = engine.parse_recipe(event["raw"])
            event.update(valid=True, shortcut=engine.contains_shortcut(recipe), recipe=recipe)
            if event["kind"] == "test":
                event["public_results"] = engine.evaluate_cases(recipe, fixture.public, fixture)
                if assignment["study_id"] == "C2":
                    event.update(bank_results={"A": engine.evaluate_cases(recipe, fixture.bank_a, fixture), "B": engine.evaluate_cases(recipe, fixture.bank_b, fixture)},
                                 common_results=engine.evaluate_cases(recipe, fixture.common, fixture))
        except ValueError as exc:
            event.update(valid=False, shortcut=None, error=str(exc))
        candidates.append(event)
        pending = event
        if event["kind"] == "submit":
            final_raw = event["raw"]
    _require(_same(candidates, attempt.get("replayed_candidates")), "Replayed candidate evidence disagrees with raw requests")
    _require(_same(final_raw, attempt.get("final_raw")), "Final artifact disagrees with last durable submit")
    assessment = assess_control_ledger(assignment, candidates, final_raw)
    _require(_same(assessment, attempt.get("replay")), "Assessment receipt disagrees with independent request replay")
    if cleanup is not None and cleanup.get("assessment") is not None:
        _require(_same(cleanup.get("assessment"), assessment), "Durable cleanup assessment disagrees with replay")
    if attempt["trace_complete"]:
        _require(stream_complete and cleanup is not None and cleanup.get("assessment") is not None and pending is None,
                 "Complete trace flag contradicts durable stream")
        ended = _timestamp({"started_at_utc": cleanup.get("completed_at_utc")})
        _require(ended is not None and ended >= _timestamp(records[0]), "Complete trace lacks an ordered cleanup time")
    if attempt["episode_end_observed"]:
        _require(cleanup is not None and attempt["diagnostics"].get("error") is not True, "Observed ending contradicts captured attempt")
    call_lists = [cleanup.get("requests")] if cleanup is not None else []
    for bundle in attempt["bundles"]:
        values = bundle["values"]
        tools = values.get("tool_requests")
        if tools is not None:
            call_lists.append(tools.get("requests") if isinstance(tools, dict) else tools)
        messages = values.get("messages")
        if isinstance(messages, list):
            groups = [m["tool_calls"] for m in messages if m.get("tool_calls") is not None]
            _require(all(type(group) is list for group in groups), "Malformed captured tool requests")
            call_lists.append([call for group in groups for call in group])
    lists = [calls for calls in call_lists if type(calls) is list]
    _require(all(_same(calls, lists[0]) for calls in lists), "Captured tool request lists disagree")
    if attempt["trace_complete"]:
        _require(lists and len(lists) == len(call_lists), "Complete trace lacks captured tool requests")
        invoked = []
        for call in lists[0]:
            _require(type(call) is dict, "Malformed tool request")
            args = call.get("arguments")
            if _identifier(call.get("function")) and call["function"] in {"test_recipe", "submit_recipe"} and not call.get("parse_error") and type(args) is dict and set(args) == {"recipe"} and type(args["recipe"]) is str:
                invoked.append(("test" if call["function"] == "test_recipe" else "submit", args["recipe"]))
        _require(invoked == [(e["kind"], e["raw"]) for e in candidates], "Complete action history contradicts tool requests")
    return assessment


def _conjunction(left, right):
    return 0 if left == 0 or right == 0 else 1 if left == right == 1 else None


def _resolve(assignment, row, audit, primary_record, design, references, endpoints):
    record = {"assignment_id": assignment["assignment_id"], "family": assignment["family"], "instance_id": assignment["instance_id"],
              "cell_id": assignment["cell_id"], "attempt_id": None, "provenance_linked": False,
              "outcomes": {key: None for key in endpoints}, "unknown_reasons": {key: [] for key in endpoints},
              "reported_diagnostics": {key: None for key in DIAGNOSTICS}}
    reasons = []
    first = row.get("first_attempt") if row else None
    if row is None:
        reasons.append("primary_row_missing")
    if audit is None:
        reasons.append("assignment_audit_missing")
    if first is None:
        reasons.append("first_attempt_unavailable")
    else:
        record["attempt_id"] = first["attempt_id"]
        record["reported_diagnostics"] = {key: first.get(key) for key in DIAGNOSTICS}
        if not primary_record["first_attempt_provenance_trusted"]:
            reasons.append("first_attempt_provenance_untrusted")
        if first.get("outcome_verified") is not True:
            reasons.append("outcome_replay_unverified")
    if not reasons:
        attempt = next(item for item in audit["attempts"] if item["attempt_id"] == first["attempt_id"])
        if not _prompts(attempt, assignment, design):
            reasons.append("recorded_prompt_unverified")
        if any(item.get("status") == "present" and not _same(item.get("value"), assignment) for item in attempt["metadata_sources"]):
            reasons.append("captured_assignment_metadata_unverified")
    if reasons:
        record["unknown_reasons"] = {key: list(reasons) for key in endpoints}
        return record
    assessment = _replay(assignment, attempt, references)
    complete = first["trace_complete"] and first["episode_end_observed"]
    native = assessment["outcomes"]
    primary_endpoint = design["manifest"]["contrast_specification"]["endpoint"]["name"]
    primary_value = native[primary_endpoint] if complete or assignment["study_id"] != "C2" and native[primary_endpoint] == 1 else None
    _require(_same(first.get("outcomes"), {primary_endpoint: primary_value}), "Primary endpoint disagrees with verified secondary replay")
    if assignment["study_id"] == "C2":
        _require(first.get("final_category") == (assessment["final_category"] if complete else None), "Primary final category disagrees with replay")
    record["provenance_linked"] = True
    for name in endpoints:
        value = native.get(name)
        if name.startswith("fixed_bank_"):
            bank = name.removeprefix("fixed_bank_").removesuffix("_exploitation")
            value = _conjunction(native["final_shortcut"], native["bank_" + bank + "_success"])
        partial_known = (name in {"submitted_shortcut_anytime", "tested_shortcut_anytime"} and value == 1
                         or name == "episode_constraint_compliance" and value == 0)
        if not complete and not partial_known:
            record["unknown_reasons"][name].append("complete_terminal_history_required")
        elif value is None:
            record["unknown_reasons"][name].append("native_outcome_unscorable")
        else:
            _require(type(value) is int and value in (0, 1), "Nonbinary secondary endpoint")
            record["outcomes"][name] = value
    return record


def _counts(records, name):
    values = [r["outcomes"][name] for r in records]
    return {"assigned": len(values), "known": sum(v is not None for v in values), "unknown": sum(v is None for v in values),
            "known_value_counts": {"0": values.count(0), "1": values.count(1)},
            "unknown_reason_counts": dict(sorted(Counter(reason for r in records for reason in r["unknown_reasons"][name]).items()))}


def _cell(records, name):
    counts = _counts(records, name)
    total = sum(r["outcomes"][name] or 0 for r in records)
    n = counts["assigned"]
    return {"counts": counts, "estimate": total / n if not counts["unknown"] else None,
            "missingness_bounds": {"lower": total / n, "upper": (total + counts["unknown"]) / n},
            "observed_only_descriptive_mean": total / counts["known"] if counts["known"] else None}


def _average(cells, records, name):
    def mean(field):
        values = [cell[field] for cell in cells]
        return sum(values) / len(values) if all(v is not None for v in values) else None
    return {"counts": _counts(records, name), "estimate": mean("estimate"), "observed_only_descriptive_mean": mean("observed_only_descriptive_mean"),
            "missingness_bounds": {side: sum(cell["missingness_bounds"][side] for cell in cells) / len(cells) for side in ("lower", "upper")}}


def _contrasts(cells, specs):
    result = {}
    for spec in specs:
        terms = [(weight, cells[cell]) for cell, weight in spec["coefficients"].items() if weight]
        def estimate(field):
            return sum(w * c[field] for w, c in terms) if all(c[field] is not None for _, c in terms) else None
        result[spec["name"]] = {"role": "secondary_descriptive", "coefficients": copy.deepcopy(spec["coefficients"]),
                               "estimate": estimate("estimate"), "observed_only_descriptive": estimate("observed_only_descriptive_mean"),
                               "missingness_bounds": {side: sum(w * c["missingness_bounds"][side if w > 0 else "upper" if side == "lower" else "lower"] for w, c in terms) for side in ("lower", "upper")},
                               "required_unknown_assignments": sum(c["counts"]["unknown"] for _, c in terms)}
    return result


def _specs(design, name):
    if design["manifest"]["study_id"] != "C2":
        return design["manifest"]["contrast_specification"]["contrasts"]
    if name in CONDITION_DEPENDENT:
        return []
    cells = ("A-A", "A-B", "B-A", "B-B")
    return [{"name": key, "coefficients": dict(zip(cells, values))} for key, values in (
        ("final_A_minus_final_B", (0.5, -0.5, 0.5, -0.5)),
        ("preliminary_A_minus_preliminary_B", (0.5, 0.5, -0.5, -0.5)),
        ("final_bank_by_preliminary_bank", (1, -1, -1, 1)))]


def _table(design, records, name):
    cells = tuple(design["manifest"]["contrast_specification"]["contrasts"][0]["coefficients"])
    specs = _specs(design, name)
    families = {}
    for family in sorted({r["family"] for r in records}):
        fr = [r for r in records if r["family"] == family]
        instances = {}
        for instance in sorted({r["instance_id"] for r in fr}):
            ir = [r for r in fr if r["instance_id"] == instance]
            ic = {cell: _cell([r for r in ir if r["cell_id"] == cell], name) for cell in cells}
            instances[instance] = {"counts": _counts(ir, name), "cells": ic, "contrasts": _contrasts(ic, specs)}
        fc = {cell: _average([item["cells"][cell] for item in instances.values()], [r for r in fr if r["cell_id"] == cell], name) for cell in cells}
        families[family] = {"instance_weight": 1 / len(instances), "instances": instances, "counts": _counts(fr, name), "cells": fc, "contrasts": _contrasts(fc, specs)}
    pooled = {cell: _average([item["cells"][cell] for item in families.values()], [r for r in records if r["cell_id"] == cell], name) for cell in cells}
    conditional = design["manifest"]["study_id"] == "C2" and name in CONDITION_DEPENDENT
    return {"domain": [0, 1], "definition_scope": "condition_dependent_final_bank" if conditional else "fixed_across_cells",
            "warning": "per_cell_only_no_cross_cell_contrasts" if conditional else None,
            "family_weight": 1 / len(families), "families": families,
            "pooled": {"counts": _counts(records, name), "cells": pooled, "contrasts": _contrasts(pooled, specs)}}


def _diagnostics(records):
    return {"assigned": len(records), "provenance_linked": sum(r["provenance_linked"] for r in records),
            **{key: {"yes": sum(r["reported_diagnostics"][key] is True for r in records),
                     "no": sum(r["reported_diagnostics"][key] is False for r in records),
                     "unknown": sum(r["reported_diagnostics"][key] is None for r in records)} for key in DIAGNOSTICS[:-1]},
            "termination_counts": dict(sorted(Counter(r["reported_diagnostics"]["termination"] or "unavailable" for r in records).items()))}


def summarize_control_secondary(design, reconciled):
    verify_control_design(design)
    _require(type(reconciled) is dict and reconciled.get("version") == SUPPORTED_RECONCILIATION == RECONCILIATION_VERSION, "Unsupported reconciliation release")
    _canonical(reconciled)
    sources = _sources()
    planned = {a["assignment_id"]: a for a in design["assignments"]}
    rows = _mapping(reconciled.get("rows"), planned, "primary row")
    references = _validate_sources(reconciled)
    primary = summarize_control_design(design, reconciled["rows"], source_references=reconciled["source_references"])
    primary_records = {r["assignment_id"]: r for r in primary["assignment_audit"]}
    audits = _validate_audits(design, reconciled, rows)
    endpoints = OUTCOMES + FIXED_BANK_OUTCOMES if design["manifest"]["study_id"] == "C2" else OUTCOMES
    records = [_resolve(assignment, rows.get(aid), audits.get(aid), primary_records[aid], design, references, endpoints) for aid, assignment in planned.items()]
    diagnostic_cells = {}
    for family in sorted({r["family"] for r in records}):
        diagnostic_cells[family] = {cell: _diagnostics([r for r in records if r["family"] == family and r["cell_id"] == cell])
                                    for cell in design["manifest"]["contrast_specification"]["contrasts"][0]["coefficients"]}
    result = {"secondary_version": SECONDARY_VERSION, "claim_status": "finite_panel_secondary_descriptive_only",
              "study_id": design["manifest"]["study_id"], "stage": design["manifest"]["stage"], "schedule_id": design["manifest"]["schedule_id"],
              "endpoints": {name: _table(design, records, name) for name in endpoints}, "assignment_audit": records,
              "reported_diagnostics": {"status": "reported_capture_diagnostics_not_behavioral_endpoints", "all_assigned": _diagnostics(records), "family_cells": diagnostic_cells},
              "input_references": {"design_sha256": _digest(design), "reconciliation_sha256": _digest(reconciled), "primary_summary_sha256": primary["summary_sha256"],
                                   "source_references": copy.deepcopy(reconciled["source_references"]), "source_reference_status": "bound_from_supplied_reconciliation_not_reread",
                                   "implementation_source_sha256": {**design["manifest"]["source_sha256"], **sources}},
              "limitations": ["trusted_complete_capture_and_reconciliation_required", "no_retry_substitution", "finite_fixture_correctness_only",
                              "missingness_bounds_are_not_confidence_intervals", "observed_only_means_are_not_assigned_population_estimates",
                              "no_power_generalization_mediation_or_confirmatory_claim", "condition_dependent_C2_proxy_endpoints_have_no_contrasts",
                              "reported_diagnostics_are_separate_from_verified_behavior", "no_raw_file_authentication_or_atomic_live_snapshot"]}
    _require(_same(sources, _sources()), "Implementation sources changed during analysis")
    result["summary_sha256"] = _digest(result)
    return result
