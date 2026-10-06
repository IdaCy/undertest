"""Read frozen calibration exports; never invoke a parser, scorer, or model."""

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


VALIDITY_FIELDS = (
    "segmented_format_valid", "raw_text_format_valid", "behavior_observed",
    "choice_depends_on_segmentation", "conservative_behavior_observed",
)
CHOICE_FIELDS = (
    "shortcut_selection", "conservative_shortcut_selection", "task_success",
    "conservative_task_success", "ordinary_bug_selection", "weak_proxy_success",
)
DIAGNOSTIC_FIELDS = ("diagnostic_success", "conservative_diagnostic_success")
MEASUREMENT_SOURCES = (
    "src/undertest/development/choice.py", "src/undertest/development/evidence.py",
    "src/undertest/development/response.py", "src/undertest/results.py",
)
RUNNER_SOURCE = "experiments/development/run_panel.py"
# Historical archive member names above remain valid. New snapshots use this
# layout; source hashes still distinguish the two implementations.
CURRENT_MEASUREMENT_SOURCES = (
    "src/emergence/checkpoints/choice.py", "src/emergence/checkpoints/evidence.py",
    "src/emergence/checkpoints/response.py", "src/shared/results.py",
)
CURRENT_RUNNER_SOURCE = "src/emergence/checkpoints/calibration/run_panel.py"
# Reviewed 87ef0cb -> 3f63a77 change: disable provider/SDK retries, stream=False.
# No other runner revision is silently treated as measurement-equivalent.
RETRY_ONLY_RUNNERS = {
    "7773bdf1b089c21f1a6684481b7970b93727d485e420a252f7a51ff90d82e5e9",
    "4e91be7b92f0c54f3c580f6fa1a28cff36adf370ebb59a8cfdd4df738c31910d",
}
CONDITIONS = {f"{f}_{g}" for f in ("ordinary", "evaluation") for g in ("limited", "broad")}


def binary_summary(values: list[bool | None]) -> dict:
    if any(value is not None and type(value) is not bool for value in values):
        raise ValueError("Binary outcomes must be Boolean or unknown")
    count = len(values)
    positive = sum(value is True for value in values)
    negative = sum(value is False for value in values)
    known = positive + negative
    return {
        "assigned": count, "true": positive, "false": negative,
        "unknown": count - known,
        "observed_rate": positive / known if known else None,
        "all_assigned_bounds": [positive / count, (count - negative) / count] if count else None,
    }


def grading_contrast(records: list[dict], field: str) -> dict:
    """Limited minus broad, weighting families, instances, and framing equally."""
    cells = defaultdict(list)
    for record in records:
        metadata = record["metadata"]
        if metadata["sample_kind"] == "main":
            cells[(metadata["family"], metadata["instance_id"], metadata["framing"], metadata["grading"])].append(record["outcomes"].get(field))
    instances = sorted({key[:2] for key in cells})
    families = defaultdict(list)
    details = []
    for family, instance in instances:
        bounds, observed, cell_details = [], [], []
        for framing in ("ordinary", "evaluation"):
            summaries = {}
            for grading in ("limited", "broad"):
                values = cells.get((family, instance, framing, grading), [])
                if not values:
                    raise ValueError(f"Missing assigned factorial cell: {family}/{instance}/{framing}/{grading}")
                summaries[grading] = binary_summary(values)
            limited, broad = summaries["limited"], summaries["broad"]
            bounds.append([
                limited["all_assigned_bounds"][0] - broad["all_assigned_bounds"][1],
                limited["all_assigned_bounds"][1] - broad["all_assigned_bounds"][0],
            ])
            observed.append(
                limited["observed_rate"] - broad["observed_rate"]
                if limited["observed_rate"] is not None and broad["observed_rate"] is not None else None
            )
            cell_details.append({"framing": framing, **summaries})
        item = {
            "family": family, "instance_id": instance, "cells": cell_details,
            "all_assigned_bounds": [mean(b[0] for b in bounds), mean(b[1] for b in bounds)],
            "observed_cell_contrast": mean(observed) if all(value is not None for value in observed) else None,
        }
        details.append(item)
        families[family].append(item)
    bounds = [mean(mean(item["all_assigned_bounds"][index] for item in items) for items in families.values()) for index in (0, 1)] if families else None
    observed = [item["observed_cell_contrast"] for item in details]
    return {
        "field": field, "family_count": len(families), "instance_count": len(details),
        "weighting": "equal families, equal instances within family, equal framing cells; equal assigned replicates within cells",
        "all_assigned_bounds": bounds,
        "fully_observed_contrast": bounds[0] if bounds and bounds[0] == bounds[1] else None,
        "observed_cell_contrast": mean(mean(item["observed_cell_contrast"] for item in items) for items in families.values()) if families and all(value is not None for value in observed) else None,
        "instances": details,
    }


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _csv_records(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8")
    previous_limit = csv.field_size_limit()
    try:
        # Exported evidence can exceed Python's 128 KiB per-field default.
        # Bound the temporary allowance by the already-read file, then restore it.
        csv.field_size_limit(max(previous_limit, len(text)))
        return list(csv.DictReader(io.StringIO(text)))
    finally:
        csv.field_size_limit(previous_limit)


class _Bundle:
    """Only resolve bundle-relative paths, never CSV paths from the remote host."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.hashes = {}

    def read(self, relative: str) -> bytes:
        path = self.directory / relative
        if not path.resolve().is_relative_to(self.directory):
            raise ValueError(f"Artifact escapes run directory: {relative}")
        raw = path.read_bytes()
        self.hashes[relative] = _sha(raw)
        return raw

    def json(self, relative: str):
        return json.loads(self.read(relative))


def _validate_panel(panel: dict) -> dict:
    if panel.get("panel_kind") != "calibration" or panel.get("schema_version") != 1:
        raise ValueError("A full calibration panel is required")
    config = panel["build_config"]
    repetitions, instances = config["repetitions"], config["instance_ids"]
    if type(repetitions) is not int or repetitions < 1 or not instances or len(set(instances)) != len(instances):
        raise ValueError("Invalid full panel build configuration")
    if set(config["conditions"]) != CONDITIONS or len(config["conditions"]) != 4 or config["include_controls"] is not True:
        raise ValueError("Full panel requires all four factorial cells and controls")
    expected, cells, controls, families = {}, Counter(), Counter(), {}
    for sample in panel["samples"]:
        identifier, metadata = sample["id"], sample["metadata"]
        if not isinstance(identifier, str) or not identifier or identifier in expected:
            raise ValueError("Duplicate or invalid assignments in full panel")
        expected[identifier] = sample
        if not isinstance(sample["input"], str) or metadata["input_sha256"] != _sha(sample["input"].encode("utf-8")):
            raise ValueError("Panel prompt hash mismatch")
        if metadata["split"] != "calibration" or metadata["assay_version"] != panel["assay_version"] or metadata["cue_variant"] != config["cue_variant"]:
            raise ValueError("Panel assignment metadata mismatch")
        kind, instance = metadata["sample_kind"], metadata["instance_id"]
        if kind != "format":
            if instance not in instances:
                raise ValueError("Unexpected panel instance")
            if families.setdefault(instance, metadata["family"]) != metadata["family"]:
                raise ValueError("Inconsistent instance family")
        if kind == "main":
            if metadata["condition"] != f'{metadata["framing"]}_{metadata["grading"]}' or metadata["condition"] not in CONDITIONS:
                raise ValueError("Invalid factorial condition")
            cells[(instance, metadata["condition"], metadata["replicate"])] += 1
        elif kind in ("format", "competence", "score_comprehension"):
            controls[(kind, instance)] += 1
            if not metadata["condition"].startswith("control_"):
                raise ValueError("Control lacks separate condition")
        else:
            raise ValueError("Unknown sample kind")
    required = Counter({(instance, condition, replicate): 1 for instance in instances for condition in CONDITIONS for replicate in range(repetitions)})
    if cells != required:
        raise ValueError("Incomplete or duplicated assigned factorial")
    if any(controls[(kind, instance)] != 1 for kind in ("competence", "score_comprehension") for instance in instances) or sum(count for (kind, _), count in controls.items() if kind == "format") != len(instances) or any(count != 1 for count in controls.values()):
        raise ValueError("Incomplete or duplicated assigned controls")
    return expected


def _without_local_paths(value):
    if isinstance(value, dict):
        return {key: _without_local_paths(item) for key, item in value.items() if key not in ("local_path", "local_evidence_directory")}
    if isinstance(value, list):
        return [_without_local_paths(item) for item in value]
    return value


def _signature(manifest: dict) -> dict:
    receipt, sources = manifest["server_receipt"], manifest["source_sha256"]
    historical = any(name in sources for name in (*MEASUREMENT_SOURCES, RUNNER_SOURCE))
    measurements, runner_name = (MEASUREMENT_SOURCES, RUNNER_SOURCE) if historical else (CURRENT_MEASUREMENT_SOURCES, CURRENT_RUNNER_SOURCE)
    for name in (*measurements, runner_name):
        if name not in sources:
            raise ValueError(f"Missing measurement source pin: {name}")
    runner = sources[runner_name]
    return {
        "model": manifest["model"], "model_revision": manifest["model_revision"],
        "mode": manifest["mode"], "checkpoint": _without_local_paths(manifest["checkpoint"]),
        "assay_version": manifest["assay_version"],
        "generation": {key: value for key, value in manifest["generation"].items() if key != "max_retries"},
        "system_message": manifest["system_message"], "epochs": manifest["epochs"],
        "max_connections": manifest["max_connections"],
        "inspect_ai_version": manifest["inspect_ai_version"], "openai_version": manifest["openai_version"],
        "serving": {key: receipt.get(key) for key in ("model_id", "model_revision", "tokenizer_id", "tokenizer_revision", "served_model", "precision", "server_version", "versions", "launch_argv", "chat_template_sha256", "generation_defaults_source", "max_model_len", "quantization")},
        "served_files": {kind: [{key: item.get(key) for key in ("sha256", "bytes")} for item in receipt.get(kind, [])] for kind in ("weight_files", "tokenizer_files")},
        "measurement_sources": {key: value for key, value in sources.items() if key.startswith("src/")},
        "runner_compatibility": "reviewed_retry_only_87ef0cb_to_3f63a77" if runner in RETRY_ONLY_RUNNERS else runner,
    }


def _verify_snapshot(bundle: _Bundle, manifest: dict) -> None:
    with zipfile.ZipFile(io.BytesIO(bundle.read("source.zip"))) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate source archive members")
        actual = {name: _sha(archive.read(name)) for name in names}
    if actual != manifest["source_sha256"]:
        raise ValueError("Saved source archive differs from run manifest")


def _tokenization_hashes(bundle: _Bundle, manifest: dict, inputs: list[dict]) -> dict:
    tokenized = bundle.json("tokenized_inputs.json")
    if set(tokenized) != {sample["id"] for sample in inputs}:
        raise ValueError("Saved tokenization assignments differ from inputs")
    hashes = {}
    for sample in inputs:
        payload = {"model": manifest["server_receipt"]["served_model"], "add_special_tokens": False}
        if manifest["mode"] == "raw":
            payload["prompt"] = sample["input"]
        elif manifest["mode"] == "native-chat":
            payload["messages"] = [
                {"role": "system", "content": manifest["system_message"]},
                {"role": "user", "content": sample["input"]},
            ]
            payload["add_generation_prompt"] = True
        else:
            raise ValueError("Unknown elicitation mode")
        item = tokenized[sample["id"]]
        tokens = item["response"].get("tokens")
        if item["request"] != payload or not isinstance(tokens, list) or not tokens or any(type(token) is not int or token < 0 for token in tokens):
            raise ValueError("Saved tokenization changes prompt or has invalid tokens")
        if item["response"].get("count", len(tokens)) != len(tokens):
            raise ValueError("Saved tokenization count differs from tokens")
        hashes[sample["id"]] = _sha(json.dumps(tokens, separators=(",", ":")).encode("utf-8"))
    return hashes


def _metadata_matches(actual: dict, expected: dict, manifest: dict) -> bool:
    runtime = {
        "elicitation_mode": manifest["mode"],
        "reasoning_prefix_in_prompt": manifest["mode"] == "native-chat" and manifest["checkpoint"].get("injected_reasoning_prefix_native_chat") == "<think>",
    }
    # Generation may have failed before the solver stored its seed.
    if "generation_seed" in actual:
        runtime["generation_seed"] = expected["seed"]
    return actual == {**expected, **runtime}


def _validate_outcomes(outcomes: dict) -> None:
    for field in (*VALIDITY_FIELDS, *CHOICE_FIELDS, *DIAGNOSTIC_FIELDS):
        if field not in outcomes:
            raise ValueError(f"Missing saved endpoint: {field}")
        binary_summary([outcomes[field]])


def _reconcile(reconciliation: dict, bundle: _Bundle, expected: dict, records: dict, panel_hash: str) -> set:
    if reconciliation["original_panel_sha256"] != panel_hash or reconciliation["full_assigned_count"] != len(expected):
        raise ValueError("Reconciliation changes original assignment denominator")
    log_name = Path(reconciliation["original_log"]).name
    if _sha(bundle.read(f"logs/{log_name}")) != reconciliation["original_log_sha256"]:
        raise ValueError("Reconciliation log hash mismatch")
    groups = [reconciliation[key] for key in ("completed_ids", "interrupted_retained_unknown_ids", "unstarted_ids")]
    identifiers = [identifier for group in groups for identifier in group]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(expected):
        raise ValueError("Reconciliation must partition original assignments exactly once")
    completed, interrupted, unstarted = map(set, groups)
    if completed != {identifier for identifier, row in records.items() if row["measurement_available"]} or interrupted != set(records) - completed or unstarted != set(expected) - set(records):
        raise ValueError("Reconciliation disagrees with original exported attempts")
    attempts = reconciliation["attempts"]
    if len(attempts) != len(records) or {row["sample_id"] for row in attempts} != set(records) or any(row["event_count"] != 1 or row.get("framework_retries") not in (None, 0) for row in attempts):
        raise ValueError("Reconciliation contains repeated or unmatched generation attempts")
    return unstarted


def load_cohort(panel_path: Path, run_directories: list[Path], *, reconciliation_path: Path | None = None) -> dict:
    """Load original full panel first, followed by disjoint continued attempts.

    A reconciliation is required for continuation: absent rows alone do not prove
    that an interrupted request was never started. Its claims are preserved and
    bound to the original log hash; hidden provider requests are not inferred.
    """
    raw_panel = panel_path.read_bytes()
    panel, panel_hash = json.loads(raw_panel), _sha(raw_panel)
    expected = _validate_panel(panel)
    if not run_directories or len({directory.resolve() for directory in run_directories}) != len(run_directories):
        raise ValueError("Supply unique run directories, original first")
    reconciliation_raw = reconciliation_path.read_bytes() if reconciliation_path else None
    reconciliation = json.loads(reconciliation_raw) if reconciliation_raw is not None else None
    if len(run_directories) > 1 and reconciliation is None:
        raise ValueError("Continuation requires interruption reconciliation")
    records, runs, seen_runs, continued_inputs = {}, [], set(), set()
    reference, eligible, original_tokenizations = None, set(), {}
    for index, directory in enumerate(run_directories):
        bundle = _Bundle(directory)
        manifest = bundle.json("manifest.json")
        if manifest["run_type"] != "calibration" or manifest["preview_only"] or manifest["epochs"] != 1:
            raise ValueError("Only executed, single-epoch calibration runs belong in this cohort")
        if manifest["run_id"] in seen_runs:
            raise ValueError("Repeated run identity")
        seen_runs.add(manifest["run_id"])
        _verify_snapshot(bundle, manifest)
        signature = _signature(manifest)
        if reference is None:
            reference = signature
        elif signature != reference:
            raise ValueError("Measurement, model, serving or decoding differs across continuation runs")
        saved_panel = bundle.json("panel.json")
        if bundle.hashes["panel.json"] != manifest["panel_sha256"]:
            raise ValueError("Saved run panel hash differs from manifest")
        inputs = bundle.json("inputs.json")
        ids = [sample["id"] for sample in inputs]
        if len(set(ids)) != len(ids) or ids != manifest["sample_ids"] or inputs != saved_panel["samples"]:
            raise ValueError("Duplicate inputs or saved assignment list differs from manifest/panel")
        if index == 0 and (saved_panel != panel or ids != list(expected)):
            raise ValueError("First run must be the full original assigned panel")
        for sample in inputs:
            if sample != expected.get(sample["id"]):
                raise ValueError("Continuation changes an original assignment or prompt")
        tokenizations = _tokenization_hashes(bundle, manifest, inputs)
        if index == 0:
            original_tokenizations = tokenizations
        elif any(value != original_tokenizations[identifier] for identifier, value in tokenizations.items()):
            raise ValueError("Continuation changes tokenized prompt")
        if index:
            if not set(ids) <= eligible or set(ids) & continued_inputs:
                raise ValueError("Continuation includes a started, unknown, or repeated assigned input")
            continued_inputs.update(ids)
        rows = _csv_records(bundle.read("samples.csv"))
        sample_keys = set()
        for row in rows:
            identifier = row.get("sample_id")
            if not identifier:
                if row.get("sample_status") != "no_samples":
                    raise ValueError("Export row lacks assignment identity")
                continue
            if row.get("latest_attempt") != "True" or row.get("epoch") != "1" or json.loads(row.get("error_retries") or "[]"):
                raise ValueError("Multiple attempts require explicit adjudication")
            if identifier not in ids or identifier in records:
                raise ValueError("Unexpected or repeated attempted assignment")
            key = row["sample_key"]
            if not key or key in sample_keys or Path(key).name != key or key in (".", ".."):
                raise ValueError("Duplicate or invalid artifact sample key")
            sample_keys.add(key)
            for field in ("run_id", "config_id", "model", "model_revision"):
                if row[field] != str(manifest[field]):
                    raise ValueError(f"Export row {field} differs from manifest")
            metadata = expected[identifier]["metadata"]
            if row["condition"] != metadata["condition"] or not _metadata_matches(json.loads(row["sample_metadata"]), metadata, manifest):
                raise ValueError("Exported assignment metadata differs from original")
            relative = f"artifacts/{key}"
            artifact_metadata = bundle.json(f"{relative}/metadata.json")
            if artifact_metadata["sample"] != json.loads(row["sample_metadata"]):
                raise ValueError("Artifact assignment metadata differs from export row")
            status = row["sample_status"]
            if status not in ("completed", "error", "limit"):
                raise ValueError("Unknown sample execution status")
            complete = status == "completed" and not row.get("error") and not row.get("limit")
            export_errors = bundle.json(f"{relative}/export_errors.json")
            if complete and (row.get("artifact_error") or json.loads(row.get("artifact_export_errors") or "[]") or export_errors):
                raise ValueError("Completed sample has artifact export errors")
            saved = {}
            for name in ("outcomes", "evidence", "model_output"):
                filename = f"{relative}/development/{name}.json"
                if complete or (directory / filename).is_file():
                    saved[name] = bundle.json(filename)
            scores = bundle.json(f"{relative}/scores.json")
            if complete:
                _validate_outcomes(saved["outcomes"])
                score = scores.get("development_outcomes", {})
                if score.get("value") != saved["outcomes"] or json.loads(row.get("score.development_outcomes.value") or "null") != saved["outcomes"]:
                    raise ValueError("Saved score differs from exported outcomes")
            usage = saved.get("model_output", {}).get("usage") or {}
            tokens = {}
            for field in ("input_tokens", "output_tokens"):
                value = usage.get(field)
                if value is not None and (type(value) is not int or value < 0 or str(value) != row[field]):
                    raise ValueError("Reported usage differs from exported usage")
                tokens[field] = value
            records[identifier] = {
                "sample_id": identifier, "metadata": metadata, "sample_status": status,
                "measurement_available": complete, "run_id": manifest["run_id"], "sample_key": key,
                "outcomes": saved.get("outcomes", {}) if complete else {},
                "saved_outcomes": saved.get("outcomes"), "evidence": saved.get("evidence", {}),
                "error": row.get("error") or None, "limit": row.get("limit") or None,
                "exclusion_reason": None if complete else "execution_error_or_limit; scientific endpoints unknown",
                "artifact_path": relative, **tokens,
            }
        audit = bundle.json("execution_audit.json")
        run_ids = {identifier for identifier, row in records.items() if row["run_id"] == manifest["run_id"]}
        if audit["expected_count"] != len(ids) or audit["latest_count"] != len(run_ids) or set(audit["missing_ids"]) != set(ids) - run_ids or audit["unexpected_ids"] or audit["duplicate_ids"]:
            raise ValueError("Execution audit disagrees with exported assignment coverage")
        if index == 0 and reconciliation is not None:
            eligible = _reconcile(reconciliation, bundle, expected, records, panel_hash)
        runs.append({
            "bundle_name": directory.name, "run_id": manifest["run_id"], "config_id": manifest["config_id"],
            "git": manifest["git"], "retry_policy": manifest["retry_policy"],
            "generation_max_retries": manifest["generation"].get("max_retries"),
            "source_sha256": manifest["source_sha256"], "files_sha256": bundle.hashes,
            "tokenized_input_sha256": tokenizations,
            "execution_audit": audit, "assigned_ids": ids,
        })
    ordered = []
    for identifier, sample in expected.items():
        ordered.append(records.get(identifier, {
            "sample_id": identifier, "metadata": sample["metadata"],
            "sample_status": "unstarted" if identifier in eligible and identifier not in continued_inputs else "not_recorded",
            "measurement_available": False, "outcomes": {}, "saved_outcomes": None, "evidence": {},
            "exclusion_reason": "no exported attempt; scientific endpoints unknown",
            "input_tokens": None, "output_tokens": None,
        }))
    return {
        "schema_version": 1, "panel_sha256": panel_hash, "assigned_count": len(expected),
        "panel_build_config": panel["build_config"], "measurement_signature": reference,
        "reconciliation": reconciliation,
        "reconciliation_sha256": _sha(reconciliation_raw) if reconciliation_raw is not None else None,
        "runs": runs, "episodes": ordered,
        "interpretation": "Frozen exported scores only. Error, limit and absent assignments remain unknown. Reconciliation claims unstarted status; CSV absence alone does not. No provider-request count or complete token expenditure is inferred.",
    }


def summarize_cohort(cohort: dict) -> dict:
    records = cohort["episodes"]
    groups = defaultdict(list)
    for record in records:
        metadata = record["metadata"]
        groups[(metadata["sample_kind"], metadata["condition"])].append(record)
    summaries = []
    for (kind, condition), group in sorted(groups.items()):
        fields = [*VALIDITY_FIELDS]
        fields += list(CHOICE_FIELDS) if kind != "format" else []
        fields += list(DIAGNOSTIC_FIELDS) if kind != "main" else []
        summaries.append({
            "sample_kind": kind, "condition": condition, "assigned": len(group),
            "outcomes": {field: binary_summary([row["outcomes"].get(field) for row in group]) for field in fields},
            "raw_text_selected_id": dict(Counter(row["outcomes"].get("raw_text_selected_id") or "unknown" for row in group)),
            "segmented_selected_id": dict(Counter(row["outcomes"].get("selected_id") or "unknown" for row in group)),
            "execution_status": dict(Counter(row["sample_status"] for row in group)),
            "stop_reason": dict(Counter(row["evidence"].get("stop_reason") or "missing" for row in group)),
            "inline_segmentation_status": dict(Counter(row["evidence"].get("inline_segmentation_status") or "missing" for row in group)),
            "reasoning_available": dict(Counter(str(row["evidence"].get("reasoning_available")) for row in group)),
            "visible_working_available": dict(Counter(str(row["evidence"].get("elicited_working_available")) for row in group)),
        })
    return {
        "schema_version": 1, "panel_sha256": cohort["panel_sha256"], "runs": cohort["runs"],
        "assigned_count": len(records), "execution_status": dict(Counter(row["sample_status"] for row in records)),
        "measurement_available_count": sum(row["measurement_available"] for row in records),
        "returned_input_tokens": sum(row["input_tokens"] or 0 for row in records),
        "returned_output_tokens": sum(row["output_tokens"] or 0 for row in records),
        "missing_usage_assignments": [row["sample_id"] for row in records if row["input_tokens"] is None or row["output_tokens"] is None],
        "groups": summaries,
        "primary_grading_contrast": grading_contrast(records, "shortcut_selection"),
        "conservative_grading_contrast": grading_contrast(records, "conservative_shortcut_selection"),
        "endpoint_definitions": {
            "raw_text": "Saved raw_text_format_valid and raw_text_selected_id only. Raw behavioral scores were not exported and are not reconstructed.",
            "segmented": "Saved segmented_format_valid, behavior_observed and unsuffixed choice/diagnostic endpoints; primary contrast uses shortcut_selection.",
            "conservative": "Saved conservative endpoints; segmentation-dependent behavioral endpoints stay unknown, not false.",
        },
        "interpretation": "Calibration diagnostics only. Controls are separate from main contrasts. Bounds span every assigned unknown and are not confidence intervals. Observed rates/contrasts condition on available outcomes; token totals cover returned usage only. No awareness or checkpoint-trend claim is inferred.",
    }


def write_analysis(panel_path: Path, run_directories: list[Path], output: Path, *, reconciliation_path: Path | None = None, cli_source: Path | None = None) -> dict:
    """Emit a new bundle only after validating all inputs; never alter sources."""
    output = output.resolve()
    if any(output.is_relative_to(directory.resolve()) for directory in run_directories):
        raise ValueError("Analysis output must be outside source run directories")
    cohort = load_cohort(panel_path, run_directories, reconciliation_path=reconciliation_path)
    summary = summarize_cohort(cohort)
    analysis_sources = {"src/emergence/checkpoints/analysis.py": _sha(Path(__file__).read_bytes())}
    if cli_source is not None:
        analysis_sources["src/emergence/checkpoints/calibration/summarize_calibration.py"] = _sha(cli_source.read_bytes())
    cohort["analysis_sources_sha256"] = analysis_sources
    raw_cohort = (json.dumps(cohort, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    summary["cohort_sha256"] = _sha(raw_cohort)
    summary["analysis_sources_sha256"] = analysis_sources
    raw_summary = (json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    output.mkdir(parents=True, exist_ok=False)
    (output / "cohort.json").write_bytes(raw_cohort)
    (output / "summary.json").write_bytes(raw_summary)
    (output / "hashes.json").write_text(json.dumps({"cohort.json": _sha(raw_cohort), "summary.json": _sha(raw_summary)}, indent=2) + "\n", encoding="utf-8")
    return summary
