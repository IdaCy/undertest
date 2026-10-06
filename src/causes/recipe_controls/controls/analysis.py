import copy
import hashlib
import json
import re
from collections import Counter, defaultdict

from causes.recipe_controls.controls.design import verify_control_design


ANALYSIS_VERSION = "effects-control-descriptive-v1"
INPUT_SCHEMA_VERSION = "effects-control-first-attempt-v1"
ATTEMPT_FIELDS = {
    "attempt_id", "first_started_attempt_verified", "metadata_integrity", "metadata_complete",
    "metadata_conflicts", "metadata_absent", "outcome_verified", "episode_end_observed", "trace_complete",
    "outcomes", "final_category", "error", "model_cap", "sample_limit", "termination",
}
BOOLEAN_FIELDS = (
    "first_started_attempt_verified", "metadata_integrity", "metadata_complete", "outcome_verified",
    "episode_end_observed", "trace_complete", "error", "model_cap", "sample_limit",
)
SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected finite JSON data") from exc


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validate_row(row, endpoint):
    if type(row) is not dict or set(row) != {"schema_version", "assignment_id", "assignment_metadata", "first_attempt"}:
        raise ValueError("Expected the explicit control first-attempt row schema")
    if row["schema_version"] != INPUT_SCHEMA_VERSION:
        raise ValueError("Unsupported reconciled control input schema")
    if type(row["assignment_id"]) is not str or not row["assignment_id"]:
        raise ValueError("Assignment ID must be a nonempty string")
    if row["assignment_metadata"] is not None and type(row["assignment_metadata"]) is not dict:
        raise ValueError("Assignment metadata must be an object or null")
    attempt = row["first_attempt"]
    if attempt is None:
        return
    if type(attempt) is not dict or set(attempt) - ATTEMPT_FIELDS:
        raise ValueError("Unexpected first-attempt fields; no retry selection is supported")
    if type(attempt.get("attempt_id")) is not str or not attempt["attempt_id"]:
        raise ValueError("Supplied first attempt requires a nonempty attempt ID")
    for field in BOOLEAN_FIELDS:
        if attempt.get(field) is not None and type(attempt[field]) is not bool:
            raise ValueError(f"{field} must be an exact boolean or null")
    for field in ("metadata_conflicts", "metadata_absent"):
        if attempt.get(field) is not None and type(attempt[field]) is not list:
            raise ValueError(f"{field} must be a list or null")
    if attempt.get("termination") is not None and (type(attempt["termination"]) is not str or not attempt["termination"]):
        raise ValueError("Termination must be a nonempty status string or null")
    outcomes = attempt.get("outcomes", {})
    if type(outcomes) is not dict or set(outcomes) - {endpoint["name"]}:
        raise ValueError("Only the registered primary endpoint belongs in this input schema")
    value = outcomes.get(endpoint["name"])
    if value is not None and (type(value) is not int or value not in endpoint["domain"]):
        raise ValueError("Endpoint value is outside its exact integer domain")
    category = attempt.get("final_category")
    category_values = endpoint.get("category_values", {})
    if category is not None and (type(category) is not str or category not in category_values):
        raise ValueError("Final category is outside the registered endpoint domain")
    if category is not None and value is not None and category_values[category] != value:
        raise ValueError("Reported final category contradicts the signed endpoint")


def _resolve(assignment, row, row_index, endpoint):
    record = {"assignment_id": assignment["assignment_id"], "family": assignment["family"],
              "instance_id": assignment["instance_id"], "cell_id": assignment["cell_id"],
              "value": None, "final_category": None, "reported_final_category": None,
              "attempt_id": None, "first_attempt_provenance_trusted": False,
              "input_row_index": row_index, "input_row_sha256": _digest(row) if row is not None else None,
              "unknown_reasons": [], "reported_diagnostics": {field: None for field in ("error", "model_cap", "sample_limit", "termination")}}
    reasons = record["unknown_reasons"]
    if row is None:
        reasons.append("assignment_row_missing")
        return record
    if _canonical(row["assignment_metadata"]) != _canonical(assignment):
        reasons.append("assignment_metadata_missing_or_mismatched")
    attempt = row["first_attempt"]
    if attempt is None:
        reasons.append("first_attempt_unavailable")
        return record
    record["attempt_id"] = attempt["attempt_id"]
    record["reported_final_category"] = attempt.get("final_category")
    record["reported_diagnostics"] = {field: attempt.get(field) for field in record["reported_diagnostics"]}
    for field in ("first_started_attempt_verified", "metadata_integrity", "metadata_complete"):
        if attempt.get(field) is not True:
            reasons.append(field + "_not_true")
    for field in ("metadata_conflicts", "metadata_absent"):
        if attempt.get(field) != []:
            reasons.append(field + "_not_verified_empty")
    record["first_attempt_provenance_trusted"] = not reasons
    if attempt.get("outcome_verified") is not True:
        reasons.append("outcome_not_verified")
    value = attempt.get("outcomes", {}).get(endpoint["name"])
    category = attempt.get("final_category")
    if "category_values" in endpoint and category is not None:
        value = endpoint["category_values"][category]
    if value is None:
        reasons.append("endpoint_unavailable")
    if "category_values" in endpoint or value == 0:
        if attempt.get("episode_end_observed") is not True:
            reasons.append("episode_end_not_observed")
        if attempt.get("trace_complete") is not True:
            reasons.append("action_trace_not_complete")
    if not reasons:
        record["value"] = value
        record["final_category"] = category
    return record


def _tri(values):
    values = list(values)
    return {"yes": sum(item is True for item in values), "no": sum(item is False for item in values),
            "unknown": sum(item is None for item in values)}


def _counts(records, endpoint):
    counts = {str(value): sum(record["value"] == value for record in records) for value in endpoint["domain"]}
    categories = {category: sum(record["final_category"] == category for record in records)
                  for category in endpoint.get("category_values", {})}
    return {"assigned": len(records), "known": sum(counts.values()), "unknown": sum(record["value"] is None for record in records),
            "known_value_counts": counts, "trusted_final_category_counts": categories,
            "final_category_unknown_or_unreported": len(records) - sum(categories.values()) if categories else None,
            "first_attempt_records_supplied": sum(record["attempt_id"] is not None for record in records),
            "first_attempt_provenance_trusted": sum(record["first_attempt_provenance_trusted"] for record in records),
            "unknown_reason_counts": dict(sorted(Counter(reason for record in records for reason in record["unknown_reasons"]).items())),
            "reported_diagnostics": {**{field: _tri(record["reported_diagnostics"][field] for record in records)
                                         for field in ("error", "model_cap", "sample_limit")},
                                     "termination_counts": dict(sorted(Counter(record["reported_diagnostics"]["termination"] or "unavailable" for record in records).items()))}}


def _cell(records, endpoint):
    counts = _counts(records, endpoint)
    assigned, known, unknown = counts["assigned"], counts["known"], counts["unknown"]
    total = sum(record["value"] for record in records if record["value"] is not None)
    lower, upper = min(endpoint["domain"]), max(endpoint["domain"])
    return {"counts": counts, "estimate": total / assigned if not unknown else None,
            "missingness_bounds": {"lower": (total + unknown * lower) / assigned, "upper": (total + unknown * upper) / assigned},
            "observed_only_descriptive_mean": total / known if known else None}


def _average(entries, field):
    values = [entry[field] for entry in entries]
    return sum(values) / len(values) if all(value is not None for value in values) else None


def _average_cells(cells, records, endpoint):
    return {"counts": _counts(records, endpoint), "estimate": _average(cells, "estimate"),
            "missingness_bounds": {side: sum(cell["missingness_bounds"][side] for cell in cells) / len(cells) for side in ("lower", "upper")},
            "observed_only_descriptive_mean": _average(cells, "observed_only_descriptive_mean")}


def _contrasts(cells, specifications):
    result = {}
    for spec in specifications:
        required = [(coefficient, cells[cell]) for cell, coefficient in spec["coefficients"].items() if coefficient != 0]
        known = all(cell["estimate"] is not None for _, cell in required)
        observed = all(cell["observed_only_descriptive_mean"] is not None for _, cell in required)
        lower = sum(coefficient * cell["missingness_bounds"]["lower" if coefficient > 0 else "upper"] for coefficient, cell in required)
        upper = sum(coefficient * cell["missingness_bounds"]["upper" if coefficient > 0 else "lower"] for coefficient, cell in required)
        result[spec["name"]] = {"role": spec["role"], "coefficients": copy.deepcopy(spec["coefficients"]),
                              "estimate": sum(coefficient * cell["estimate"] for coefficient, cell in required) if known else None,
                              "missingness_bounds": {"lower": lower, "upper": upper},
                              "observed_only_descriptive": sum(coefficient * cell["observed_only_descriptive_mean"] for coefficient, cell in required) if observed else None,
                              "required_unknown_assignments": sum(cell["counts"]["unknown"] for _, cell in required)}
    return result


def summarize_control_design(design, rows, *, source_references=None):
    verify_control_design(design)
    if type(rows) is not list:
        raise ValueError("Reconciled rows must be a list keyed by unique assignment_id fields")
    _canonical(rows)
    references = [] if source_references is None else source_references
    if type(references) is not list:
        raise ValueError("Source references must be a list")
    for reference in references:
        if (type(reference) is not dict or set(reference) != {"path", "sha256"}
                or type(reference["path"]) is not str or not reference["path"]
                or type(reference["sha256"]) is not str or SHA256.fullmatch(reference["sha256"]) is None):
            raise ValueError("Source references require an exact path and lowercase SHA-256")
    manifest = design["manifest"]
    spec = manifest["contrast_specification"]
    endpoint = spec["endpoint"]
    assignments = design["assignments"]
    planned = {assignment["assignment_id"]: assignment for assignment in assignments}
    supplied = {}
    attempts = set()
    for index, row in enumerate(rows):
        _validate_row(row, endpoint)
        identity = row["assignment_id"]
        if identity not in planned or identity in supplied:
            raise ValueError("Extra or duplicate assignment ID")
        supplied[identity] = (index, row)
        if row["first_attempt"] is not None:
            attempt_id = row["first_attempt"]["attempt_id"]
            if attempt_id in attempts:
                raise ValueError("Attempt ID is cross-linked across assignments")
            attempts.add(attempt_id)
    records = []
    groups = defaultdict(list)
    for assignment in assignments:
        index, row = supplied.get(assignment["assignment_id"], (None, None))
        record = _resolve(assignment, row, index, endpoint)
        records.append(record)
        groups[(assignment["family"], assignment["instance_id"], assignment["cell_id"])].append(record)
    cell_ids = tuple(spec["contrasts"][0]["coefficients"])
    panel = {}
    for assignment in assignments:
        panel.setdefault(assignment["family"], set()).add(assignment["instance_id"])
    families = {}
    for family in sorted(panel):
        instances = {}
        for instance in sorted(panel[family]):
            cells = {cell: _cell(groups[(family, instance, cell)], endpoint) for cell in cell_ids}
            instance_records = [record for record in records if record["instance_id"] == instance and record["family"] == family]
            instances[instance] = {"counts": _counts(instance_records, endpoint), "cells": cells, "contrasts": _contrasts(cells, spec["contrasts"])}
        family_records = [record for record in records if record["family"] == family]
        cells = {cell: _average_cells([instance["cells"][cell] for instance in instances.values()],
                                     [record for record in family_records if record["cell_id"] == cell], endpoint) for cell in cell_ids}
        families[family] = {"instance_weight": 1 / len(instances), "instances": instances, "counts": _counts(family_records, endpoint),
                            "cells": cells, "contrasts": _contrasts(cells, spec["contrasts"])}
    pooled_cells = {cell: _average_cells([family["cells"][cell] for family in families.values()],
                                        [record for record in records if record["cell_id"] == cell], endpoint) for cell in cell_ids}
    result = {"analysis_version": ANALYSIS_VERSION, "input_schema_version": INPUT_SCHEMA_VERSION,
              "claim_status": "descriptive_only_not_confirmatory", "study_id": manifest["study_id"],
              "stage": manifest["stage"], "schedule_id": manifest["schedule_id"], "endpoint": copy.deepcopy(endpoint),
              "contrast_specification": copy.deepcopy(spec), "family_weight": 1 / len(families),
              "families": families, "pooled": {"counts": _counts(records, endpoint), "cells": pooled_cells, "contrasts": _contrasts(pooled_cells, spec["contrasts"])},
              "assignment_audit": records,
              "input_references": {"design_sha256": _digest(design), "reconciled_rows_sha256": _digest(rows),
                                   "source_references": copy.deepcopy(references), "source_reference_status": "caller_supplied_not_reread"},
              "limitations": ["finite_verified_panel_only", "declared_first_attempt_provenance_requires_audited_reconciler",
                              "missingness_bounds_are_not_sampling_confidence_intervals", "observed_only_descriptives_are_not_assigned_population_estimates",
                              "signed_zero_categories_do_not_establish_honesty", "reported_diagnostics_are_not_provenance_verified_behavior",
                              "equal_planned_rollouts_required_by_verified_design", "no_bootstrap_power_or_adequacy_claim"]}
    result["summary_sha256"] = _digest(result)
    return result
