from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _pin(path):
    path = Path(path)
    _require(path.is_absolute() and path.resolve() == path, "noncanonical_path")
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def read_pinned_bytes(reference):
    _require(type(reference) is dict and set(reference) == {"path", "bytes", "sha256"},
             "exact_pin_required")
    path = Path(reference["path"])
    _require(path.is_absolute() and path.resolve() == path, "noncanonical_path")
    _require(path.name not in {".env", "response.raw", "parsed.json", "result.json",
                               "initial_context.json", "early_context.json", "end_context.json"},
             "metadata_only")
    raw = path.read_bytes()
    _require(type(reference["bytes"]) is int and len(raw) == reference["bytes"]
             and hashlib.sha256(raw).hexdigest() == reference["sha256"], "pin_changed")
    return raw


def project_closed_reader(descriptor, *, read_bytes=read_pinned_bytes):
    """Derive a cumulative-reader interface from one fully accounted closed batch.

    No intermediate progress capture is required. When supplied, it must be an
    accepted prefix of this same batch. This function grants no review acceptance.
    """
    _require(type(descriptor) is dict and set(descriptor) == {
        "closure", "closure_review", "native_capture_source", "prior_fresh_closures",
        "base_result", "base_review", "progress",
    }, "descriptor_fields")

    def raw(reference):
        result = read_bytes(reference)
        _require(type(result) is bytes and type(reference["bytes"]) is int
                 and len(result) == reference["bytes"]
                 and hashlib.sha256(result).hexdigest() == reference["sha256"], "pin_changed")
        return result

    def load(reference):
        return _json(raw(reference))

    def accepted(actual_ref, review_ref, flag):
        actual, review = load(actual_ref), load(review_ref)
        _require(review.get(flag) is True and review.get("actual") == actual_ref
                 and review.get("findings") == [], "independent_acceptance_required")
        return actual, review

    a, peer = accepted(descriptor["closure"], descriptor["closure_review"],
                       "accepted_finite_reader_route_closed_cut")
    for key in ("plan", "plan_review", "reader_source", "root", "journal", "runtime_result",
                "prepared", "complete", "execution", "route_predecessor", "route_state_carry",
                "initial_consecutive_failed_contexts", "final_consecutive_failed_contexts",
                "native_failure_events"):
        _require(peer[key] == a[key], "closed_review_join:" + key)
    _require(peer["annotation_missing_contexts"] == len(a["annotation_missing_contexts"]),
             "annotation_missing_count")
    _require(a["source"] == descriptor["native_capture_source"]
             and a["schema"] == "causes.finite_reader_route_closed_cut.v1", "capture_source_join")
    raw(descriptor["native_capture_source"])
    n = a["claimed"]
    _require(type(n) is int and n > 0 and n == a["ready"] == a["finished_or_unresolved"]
             and a["active"] == a["unclaimed"] == 0 and a["unclaimed_keys"] == []
             and a["all_claims_accounted"] is True, "closed_claim_accounting")
    _require(all(a[key] is None for key in ("stop_reason", "quarantine", "owned_pause"))
             and a["final_for_reader_analysis"] is False, "natural_closure_required")

    journal = raw(a["journal"])
    _require(journal.endswith(b"\n"), "complete_journal_required")
    events = [_json(line) for line in journal.splitlines()]
    claims = [e["key"] for e in events if e["event"] == "claimed"]
    terminals = [e["key"] for e in events
                 if e["event"] in ("finished", "unresolved_current_reader_attempt")]
    _require(len(claims) == len(set(claims)) == len(terminals) == len(set(terminals)) == n
             and set(claims) == set(terminals) and claims == a["claimed_keys"], "journal_claim_set")
    _require(events[-1]["event"] == "finite_selection_complete"
             and events[-1]["new_admissions_stopped"] is True
             and events[-1]["reason"] is None, "natural_final_event")

    plan = load(a["plan"])
    plan_peer = load(a["plan_review"])
    _require(plan_peer.get("accepted_fresh_candidate_reader_plan") is True
             and plan_peer["plan"] == a["plan"] and plan_peer["findings"] == [], "plan_acceptance")
    for key, actual_key in (("prepared", "prepared"), ("source", "reader_source"),
                            ("output", "root"), ("route_predecessor", "route_predecessor")):
        _require(plan[key] == a[actual_key], "plan_join:" + key)

    cuts = descriptor["prior_fresh_closures"]
    _require(type(cuts) is list and cuts and cuts == plan["intervening_reader_cuts"][-len(cuts):]
             and cuts[-1]["actual"] == a["route_predecessor"], "prior_cut_order")
    prior_keys = []
    for cut in cuts:
        old, _ = accepted(cut["actual"], cut["review"], "accepted_finite_reader_route_closed_cut")
        keys = old["claimed_keys"]
        _require(len(keys) == len(set(keys)) and not set(keys).intersection(prior_keys),
                 "duplicate_prior_claim")
        prior_keys.extend(keys)
    declared = plan["previous_cohort_claimed_keys"]
    _require(len(declared) == len(set(declared)) and set(declared) == set(prior_keys)
             and not set(claims).intersection(prior_keys)
             and plan["expected_unclaimed_contexts"] == n, "claimed_attempt_replay")

    prepared = load(a["prepared"])
    prepared_peer = load(plan["prepared_review"])
    _require(prepared_peer.get("accepted_fresh_candidate_reader_inputs") is True
             and prepared_peer["preparation"] == a["prepared"]
             and prepared_peer.get("findings") == []
             and plan["additive_ready"] == prepared["ready"], "prepared_acceptance")
    ready = load(prepared["ready"])
    ready_keys = [row["measurement_key"] for row in ready]
    counts = prepared["counts"]
    _require(len(ready_keys) == len(set(ready_keys)) == counts["reader_ready"]
             and set(prior_keys).issubset(ready_keys)
             and set(ready_keys) - set(prior_keys) == set(claims), "ready_partition")
    _require(all(type(value) is int and value >= 0 for value in counts.values())
             and counts["copied"] == a["assigned"] == counts["reader_ready"] + counts["unavailable"]
             and counts["selected"] == counts["copied"] + counts["pending"]
             and counts["reader_requests"] == 3 * counts["reader_ready"]
             and counts["self_requests"] == 0, "prepared_counts")

    result = load(a["runtime_result"])
    _require(result["claimed"] == claims and result["finished_or_unresolved"] == terminals
             and result["unclaimed"] == [] and result["stop_reason"] is None
             and result["quarantine"] is None
             and result["consecutive_failed_contexts"] == a["final_consecutive_failed_contexts"],
             "runtime_result_join")
    complete, execution = load(a["complete"]), load(a["execution"])
    _require(complete["all_actors_succeeded"] is True and execution["returncode"] == 0
             and execution["source_stable"] is True and a["execution"] in complete["executions"],
             "successful_original_completion_required")

    progress_count = None
    if descriptor["progress"] is not None:
        pair = descriptor["progress"]
        progress, progress_peer = accepted(pair["actual"], pair["review"],
                                           "accepted_fresh_candidate_reader_progress")
        _require(progress_peer.get("accepted_saved_corpus_reader_progress") is True
                 and progress["plan"] == a["plan"] and progress["root"] == a["root"]
                 and progress["ready"] == n, "progress_identity")
        prefix = raw(progress["journal"]["saved"])
        _require(prefix.endswith(b"\n") and journal.startswith(prefix), "progress_prefix")
        old_keys = [e["key"] for e in map(_json, prefix.splitlines())
                    if e["event"] in ("finished", "unresolved_current_reader_attempt")]
        progress_count = len(old_keys)
        _require(progress_count == len(set(old_keys)) == progress["finished_or_unresolved"]
                 and set(old_keys).issubset(terminals), "progress_terminal_set")

    base, base_peer = accepted(descriptor["base_result"], descriptor["base_review"],
                               "accepted_individual_variant_measurement_readout")
    for key in ("source", "assignments", "manifest", "outputs"):
        _require(base_peer[key] == base[key], "base_review_join:" + key)
    _require(type(base["physical_measurements"]) is int and base["physical_measurements"] >= 0,
             "base_count")
    output = {key: copy.deepcopy(a[key]) for key in (
        "observed_at", "plan", "plan_review", "prepared", "reader_source", "root", "assigned",
        "claimed", "active", "finished_or_unresolved", "unclaimed", "quarantine",
        "final_for_reader_analysis")}
    output.update(
        schema="causes.saved_corpus_reader_measurement_roster_projection.v1",
        phase="natural_closed_fresh_batch", ready=len(ready), frozen_ready_count=len(ready),
        admission_ready=n, global_assigned=counts["selected"], copied_contexts=counts["copied"],
        pending_copies=counts["pending"], unavailable=counts["unavailable"],
        journal=dict(saved=a["journal"], original_path=a["journal_original"]["path"],
                     observed_bytes=len(journal), whole_newline_prefix_bytes=len(journal),
                     trailing_partial_bytes=0),
        native_actual=descriptor["closure"], native_review=descriptor["closure_review"],
        closed_actual=descriptor["closure"], closed_review=descriptor["closure_review"],
        intervening_reader_cuts=plan["intervening_reader_cuts"],
        retained_prior_cohort_claimed=len(prior_keys),
        final_consecutive_failures=a["final_consecutive_failed_contexts"],
        native_failure_events=a["native_failure_events"],
        annotation_missing_contexts=a["annotation_missing_contexts"],
        all_claims_accounted=True, metadata_only=True, current_liveness_claim=False,
        new_observations=0, model_calls=0, response_or_judgment_reads=0,
    )
    accounting = dict(prior_progress=descriptor["progress"], prior_terminal_contexts=progress_count,
                      final_terminal_contexts=n, conditional_new_slots=3 * n,
                      base_measurements=base["physical_measurements"],
                      conditional_total_slots=base["physical_measurements"] + 3 * n,
                      all_closed_claims_retained=True, only_metadata_read=True)
    return output, accounting


def write_projection(descriptor_reference, output):
    from src.shared.durable_process import save

    descriptor = _json(read_pinned_bytes(descriptor_reference))
    actual, accounting = project_closed_reader(descriptor)
    destination = Path(output)
    _require(destination.is_absolute() and destination.resolve() == destination
             and not destination.exists(), "exclusive_output_required")
    actual["source"] = _pin(__file__)
    actual["descriptor"] = descriptor_reference
    destination.mkdir()
    actual_ref = save(destination / "ACTUAL.v1.json", actual)
    preparation = dict(schema="causes.closed_reader_measurement_projection_preparation.v1",
                       source=actual["source"], descriptor=descriptor_reference, actual=actual_ref,
                       native_actual=descriptor["closure"], native_review=descriptor["closure_review"],
                       **accounting)
    return actual_ref, save(destination / "PREPARATION.v1.json", preparation)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True, type=json.loads)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    actual, preparation = write_projection(args.descriptor, args.output)
    print(json.dumps(dict(actual=actual, preparation=preparation)))


if __name__ == "__main__":
    main()
