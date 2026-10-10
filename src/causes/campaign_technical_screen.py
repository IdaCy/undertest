from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _pin(path: Path, raw: bytes | None = None) -> dict:
    raw = path.read_bytes() if raw is None else raw
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def screen(bindings_path: Path, bindings_sha256: str) -> dict:
    checked: dict[str, dict] = {}

    def read_bytes(ref: dict) -> bytes:
        _require(set(ref) == {"path", "bytes", "sha256"}, "invalid_pin_fields")
        path = Path(ref["path"])
        _require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), "noncanonical_input")
        raw = path.read_bytes()
        _require(_pin(path, raw) == ref, "input_pin_mismatch")
        checked[str(path)] = ref
        return raw

    def read(ref: dict):
        return json.loads(read_bytes(ref))

    bindings_path = Path(bindings_path)
    bindings_ref = _pin(bindings_path)
    _require(bindings_ref["sha256"] == bindings_sha256, "bindings_hash_mismatch")
    config = read(bindings_ref)
    schema = config.get("schema")
    _require(schema in {"causes.technical_screen_bindings.v1", "causes.technical_screen_bindings.v2"},
             "invalid_bindings_schema")
    fields = {"schema", "source", "model", "registered_roots", "assigned",
              "prior_observation", "current_observation", "prior_review", "current_review", "output"}
    if schema == "causes.technical_screen_bindings.v2":
        fields.add("prior_registered_roots")
    _require(set(config) == fields, "invalid_bindings_fields")
    _require(config["source"] == _pin(Path(__file__).resolve()), "source_binding_mismatch")
    read_bytes(config["source"])
    output = Path(config["output"])
    _require(output.is_absolute() and output.resolve() == output and not output.exists(), "output_not_fresh")
    model, roots = config["model"], config["registered_roots"]
    _require(isinstance(model, str) and isinstance(roots, list) and roots == sorted(set(roots)) and roots,
             "invalid_model_roots")
    prior_roots = config.get("prior_registered_roots", roots)
    _require(isinstance(prior_roots, list) and prior_roots and
             all(isinstance(root, str) for root in prior_roots) and
             prior_roots == sorted(set(prior_roots)) and set(prior_roots) <= set(roots),
             "invalid_prior_roots_or_root_removal")
    _require(type(config["assigned"]) is int and config["assigned"] > 0, "invalid_assigned_count")
    prior, current = [read(config[name]) for name in ("prior_observation", "current_observation")]
    for name, observation_ref in (("prior_review", config["prior_observation"]),
                                  ("current_review", config["current_observation"])):
        review = read(config[name])
        _require(review.get("accepted_metadata_observation") is True and review.get("observation") == observation_ref,
                 "observation_review_not_accepted")
    _require(current["prior_operational_tick"] == config["prior_observation"] and
             current["prior_operational_review"] == config["prior_review"], "nonadjacent_cuts")
    _require(prior["fixed_protocol"] == current["fixed_protocol"] and
             prior["original_job_pins"] == current["original_job_pins"], "frozen_inputs_changed")

    def rows_by_run(observation: dict, expected_roots: list[str]) -> dict:
        membership = observation["fixed_ids"][model]
        _require(membership["registered_roots"] == expected_roots and membership["assigned"] == config["assigned"],
                 "fixed_scope_changed")
        rows = read(observation["fixed_index_joins"])[model]
        result, original_ids = {}, set()
        for row in rows:
            run = PurePosixPath(row["run"])
            _require(str(run) == row["run"] and ".." not in run.parts and str(run.parent) in expected_roots,
                     "unregistered_fixed_root")
            _require(row["run"] not in result and run.name not in original_ids, "duplicate_fixed_id")
            result[row["run"]] = row
            original_ids.add(run.name)
        _require(len(result) == membership["accepted_generated_final_ids"], "fixed_count_mismatch")
        return result

    before, after = rows_by_run(prior, prior_roots), rows_by_run(current, roots)
    _require(all(after.get(run) == row for run, row in before.items()), "prior_fixed_row_changed")
    prior_index, current_index = [read_bytes(o["accepted_index_consumed"]) for o in (prior, current)]
    _require(current_index.startswith(prior_index), "prior_index_prefix_changed")
    _require(not prior_index or prior_index.endswith(b"\n"), "prior_index_torn")
    _require(not current_index or current_index.endswith(b"\n"), "current_index_torn")
    appended = [json.loads(line) for line in current_index[len(prior_index):].splitlines()]
    rows = [row for row in appended if str(PurePosixPath(row["run"]).parent) in roots]
    _require(len(rows) == len({row["run"] for row in rows}), "duplicate_appended_id")
    _require({row["run"] for row in rows} == set(after) - set(before), "append_join_mismatch")
    _require(all(after[row["run"]] == row for row in rows), "append_row_changed")

    summaries, positives = [], []
    for row in rows:
        summary_ref = _pin(Path(row["path"]) / "summary.json")
        _require(summary_ref["sha256"] == row["summary_sha256"], "summary_custody_hash_mismatch")
        summary = read(summary_ref)
        run_id = PurePosixPath(row["run"]).name
        _require(summary["run_id"] == run_id, "summary_identity_mismatch")
        probes = summary.get("probes", [])
        _require(isinstance(probes, list) and all(isinstance(p, dict) for p in probes), "invalid_probe_metadata")
        actor_error = bool(summary.get("actor_error"))
        failed_probes = sum(p.get("technical_status") != "complete" for p in probes)
        summaries.append({"run_id": run_id, "summary": summary_ref})
        if actor_error or failed_probes:
            positives.append({"run_id": run_id, "summary": summary_ref,
                              "actor_error_present": actor_error, "failed_probe_records": failed_probes})
    result = {
        "schema": "causes.fixed_append_technical_screen.v1",
        "at": dt.datetime.now(dt.timezone.utc).isoformat(), "bindings": bindings_ref,
        "source": config["source"], "model": model, "registered_roots": roots,
        "prior_observation": config["prior_observation"], "current_observation": config["current_observation"],
        "prior_review": config["prior_review"], "current_review": config["current_review"],
        "fixed_counts": {"prior": len(before), "current": len(after), "appended": len(rows)},
        "scanned_summaries": summaries, "positive_cases": positives,
        "positive_case_count": len(positives),
        "actor_error_summary_count": sum(p["actor_error_present"] for p in positives),
        "failed_probe_record_count": sum(p["failed_probe_records"] for p in positives),
        "semantic_fields_projected": False, "counter_gate_used": False,
        "event_case_identity_verified": False, "runtime_actions": False, "analysis_release": False,
        "limits": "Accepted cut reviews supply original-ID qualification. This screen projects only technical flags from appended summaries; it does not repeat full custody/receipt localization or identify dispatcher events. Separate counter cut times and unresolved residuals remain distinct.",
        "verified_inputs": list(checked.values()),
    }
    if schema == "causes.technical_screen_bindings.v2":
        result["schema"] = "causes.fixed_append_technical_screen.v2"
        result["prior_registered_roots"] = prior_roots
    raw = (json.dumps(result, indent=2) + "\n").encode()
    for ref in list(checked.values()):
        read_bytes(ref)
    with output.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return _pin(output, raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--bindings-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(screen(args.bindings, args.bindings_sha256)))


if __name__ == "__main__":
    main()
