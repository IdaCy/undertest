import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import types


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)),
            "noncanonical_cut_path")
    return path


def pin(path, raw=None):
    path = canonical(path)
    raw = path.read_bytes() if raw is None else raw
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def read(ref):
    raw = canonical(ref["path"]).read_bytes()
    require(pin(ref["path"], raw) == ref, "cut_input_pin_changed")
    return raw


def checked(ref):
    return json.loads(read(ref))


def save(directory, name, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, indent=2) + "\n").encode()
    path = directory / name
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return pin(path, raw)


def load_helper(ref, name):
    raw = read(ref)
    module = types.ModuleType(name)
    module.__file__ = ref["path"]
    exec(compile(raw, ref["path"], "exec"), module.__dict__)
    return module, raw


def registered_fixed_membership(entries, rosters, registry, registry_module):
    joined = registry_module.fixed_rows(entries, rosters, registry)
    matched, fixed = {}, {}
    for model, ids in rosters.items():
        matched[model] = sorted(joined[model].values(), key=lambda row: row["run"])
        fixed[model] = {
            "assigned": len(ids),
            "accepted_generated_final_ids": len(joined[model]),
            "registered_roots": sorted(registry["fixed_roots"][model]),
            "scope": "Original fixed-ID custody membership in registered roots; no outcome or quality disposition."}
    return matched, fixed


def capture(binding_ref, review_ref):
    binding = checked(binding_ref)
    review = checked(review_ref)
    require(binding["schema"] == "causes.operational_cut_binding.v1", "cut_binding_schema")
    require(review.get("accepted_operational_cut_binding") is True
            and review.get("binding") == binding_ref, "cut_binding_not_reviewed")
    require(binding["source"] == pin(Path(__file__).resolve()), "cut_source_changed")
    campaign = canonical(binding["campaign_root"])
    directory = canonical(binding["output_directory"])
    require(directory.is_relative_to(campaign) and directory != campaign, "cut_output_scope")
    not_before = dt.datetime.fromisoformat(binding["not_before"])
    require(not_before.tzinfo is not None and dt.datetime.now(dt.timezone.utc) >= not_before,
            "cut_before_due_time")
    require(binding["phase_inputs"], "cut_phase_inputs_required")
    for ref in binding["phase_inputs"].values():
        read(ref)
    heartbeat_raw = read(binding["heartbeat"])
    protocol_raw = read(binding["protocol"])
    protocol = json.loads(protocol_raw)
    prior = checked(binding["prior_observation"])
    prior_review = checked(binding["prior_review"])
    require(prior_review.get("accepted_metadata_observation") is True
            and prior_review.get("observation") == binding["prior_observation"],
            "prior_cut_not_accepted")
    rosters, job_pins = {}, []
    for model, entry in binding["jobs"].items():
        ref = entry["source"]
        require(ref in protocol["artifacts"], "job_not_in_frozen_protocol")
        jobs = checked(ref)
        ids = {job["allocation"]["run_id"] for job in jobs}
        require(type(entry["expected_count"]) is int
                and len(ids) == len(jobs) == entry["expected_count"], "frozen_job_count_changed")
        rosters[model] = ids
        job_pins.append(ref)
    require(rosters, "fixed_rosters_required")
    registry_module, registry_source = load_helper(binding["registry_source"], "qualified_fixed_registry")
    registry_raw = read(binding["registry"])
    health_module, health_source = load_helper(binding["reader_health_source"], "qualified_reader_health_only")
    preserved = binding["preserved_artifacts"]
    for ref in preserved:
        read(ref)
    directory.mkdir(exist_ok=False)
    save(directory, "BINDING.consumed.json", read(binding_ref))
    save(directory, "BINDING_REVIEW.consumed.json", read(review_ref))
    index_path = canonical(binding["accepted_index"])
    require(index_path.is_relative_to(campaign), "index_outside_campaign")
    index_started = dt.datetime.now(dt.timezone.utc).isoformat()
    index_raw = index_path.read_bytes()
    index_ended = dt.datetime.now(dt.timezone.utc).isoformat()
    prefix = index_raw[:index_raw.rfind(b"\n") + 1]
    require(prefix and prefix.endswith(b"\n"), "complete_index_prefix_required")
    index_pin = save(directory, "ACCEPTED_INDEX_PREFIX.jsonl", prefix)
    index_cut = save(directory, "INDEX_CUT.json", {
        "read_started_at": index_started, "read_completed_at": index_ended,
        "consumed": index_pin, "raw_bytes_read": len(index_raw),
        "unconsumed_torn_tail_bytes": len(index_raw) - len(prefix)})
    entries = [json.loads(line) for line in prefix.splitlines()]
    roots = Counter()
    for row in entries:
        path = Path(row["run"])
        require(not path.is_absolute() and ".." not in path.parts, "invalid_index_run_path")
        roots[str(path.parent)] += 1
    registry_pin = save(directory, "FIXED_ROOT_REGISTRY.consumed.json", registry_raw)
    registry_source_pin = save(directory, "FIXED_ROOT_REGISTRY_SOURCE.consumed.py", registry_source)
    registry = registry_module.checked_registry(registry_pin["path"], registry_pin["sha256"])
    matched, fixed = registered_fixed_membership(entries, rosters, registry, registry_module)
    joins_pin = save(directory, "FIXED_INDEX_JOINS.json", matched)
    require(prefix.startswith(read(prior["accepted_index_consumed"])), "prior_index_prefix_changed")
    prior_matched = checked(prior["fixed_index_joins"])
    require(set(prior_matched) == set(matched), "fixed_model_set_changed")
    comparison = {}
    for model, rows in matched.items():
        previous = {row["run"]: row for row in prior_matched[model]}
        current = {row["run"]: row for row in rows}
        require(set(previous) <= set(current)
                and all(current[run] == row for run, row in previous.items()), "prior_fixed_rows_changed")
        comparison[model] = {"prior_fixed_final_ids": len(previous), "current_fixed_final_ids": len(current),
            "new_fixed_final_ids": len(current) - len(previous), "all_prior_summary_pins_and_accepted_rows_preserved": True}
    health_module.HERE = directory
    health_started = dt.datetime.now(dt.timezone.utc).isoformat()
    health_path = canonical(health_module.health())
    health_ended = dt.datetime.now(dt.timezone.utc).isoformat()
    require(health_path.parent == directory, "reader_health_output_scope")
    health_pin = pin(health_path)
    health = checked(health_pin)
    require(health["journal_prefix"] == pin(directory / "READER_JOURNAL_PREFIX.jsonl"),
            "reader_journal_binding_changed")
    require(read(health["journal_prefix"]).startswith(read(prior["reader_journal_prefix"])),
            "prior_reader_prefix_changed")
    health_cut = save(directory, "READER_CUT.json", {
        "invocation_started_at": health_started, "invocation_completed_at": health_ended,
        "health_observed_at": health["at"], "health": health_pin, "journal_prefix": health["journal_prefix"],
        "unconsumed_torn_tail_bytes": None,
        "tail_limit": "The pinned health helper retains complete lines but does not expose discarded tail length; no second live read."})
    missing = Counter("quote" if str(row.get("reason", "")).startswith("evidence_not_exact_channel_substring:")
        else "origin" if str(row.get("reason", "")).startswith("evidence_not_generated_actor:")
        else "other" for row in health["missing_calls"])
    require(sum(value for key, value in health["instrument_statuses"].items() if key != "validated")
            == len(health["missing_calls"]), "reader_technical_accounting_changed")
    heartbeat_pin = save(directory, "HEARTBEAT_STATE.consumed.json", heartbeat_raw)
    value = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "One local fixed-ID custody and reader-health metadata cut; no analysis, model call or runtime change.",
        "source": binding["source"], "binding": binding_ref, "binding_review": review_ref,
        "phase_inputs": binding["phase_inputs"], "fixed_ids": fixed, "fixed_protocol": binding["protocol"],
        "original_job_pins": job_pins, "index_observed_at": index_started, "index_read_completed_at": index_ended,
        "index_cut": index_cut, "accepted_index_source_path": str(index_path), "accepted_index_consumed": index_pin,
        "accepted_index_rows": len(entries), "index_last_verified_at": entries[-1]["verified_at"],
        "unconsumed_torn_tail_bytes": len(index_raw) - len(prefix), "fixed_index_joins": joins_pin,
        "accepted_rows_by_root": dict(sorted(roots.items())), "fixed_root_registry": registry_pin,
        "fixed_root_registry_source": registry_source_pin, "fixed_root_registry_input": binding["registry"],
        "fixed_root_registry_source_input": binding["registry_source"], "reader_health": health_pin,
        "reader_cut": health_cut, "reader_journal_prefix": health["journal_prefix"],
        "health_function_source": pin(binding["reader_health_source"]["path"], health_source),
        "reader_observation": {"at": health["at"], "events": health["events"],
            "instrument_statuses": health["instrument_statuses"], "missing_instrument_count": len(health["missing_calls"]),
            "missing_reason_counts": {key: missing[key] for key in ("quote", "origin", "other")},
            "source_gap_count": len(health["missing_contexts"]),
            "last_event_at": dt.datetime.fromtimestamp(health["last_event"]["at_unix"], dt.timezone.utc).isoformat(),
            "last_event_kind": health["last_event"]["event"], "daemon_read_returncode": health["daemon_read_returncode"]},
        "heartbeat_state_consumed": heartbeat_pin, "accepted_analyses_preserved": preserved,
        "prior_operational_tick": binding["prior_observation"], "prior_operational_review": binding["prior_review"],
        "prior_comparison": comparison,
        "limits": "Index and reader journal are separate cuts. Fixed custody membership is not outcome quality, family readiness or analysis authority; corpus remains excluded."}
    return save(directory, "OBSERVATION.json", value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    parser.add_argument("--review", required=True)
    args = parser.parse_args()
    print(json.dumps(capture(pin(args.binding), pin(args.review))))


if __name__ == "__main__":
    main()
