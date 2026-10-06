"""Pure prospective finite-corpus arithmetic; no actual MAIN qualification.

Inputs assert frozen measured labels and normalized endpoint states. This module
does not authenticate those assertions, certify endpoint applicability, or prove
randomization. See CONTRACT.md before interpreting any output.
"""
from fractions import Fraction
from math import isqrt


ALPHA = Fraction(1, 40)
LABELS = ("positive", "no_expression", "other_known", "unknown")
ENDPOINTS = {"P1": (Fraction(0), Fraction(1)),
             "P2": (Fraction(-1), Fraction(1))}
NONDEFINED = ("invalid_structural", "unavailable_unclassified",
              "unknown_original_gate", "invalid_functional_arm",
              "unknown_functional_validity", "prospectively_inapplicable_zero_scale")
ROW_FIELDS = frozenset(("original_id", "cell_id", "bundle_id", "label",
                        "selected", "pi", "outcome"))
MAX_ROWS = 10000
MAX_CELLS = 1024
MAX_SCENARIOS = 100


class InputError(ValueError):
    """Malformed arithmetic input; no mathematical result is emitted."""


def rational(value):
    """Bounded exact numeric parser: floats and Booleans are not scores."""
    if type(value) not in (int, str, Fraction):
        raise InputError("exact rational required")
    if type(value) is str and (len(value) > 256 or any(c not in "0123456789+-./ " for c in value)):
        raise InputError("rational text must be bounded decimal or fraction syntax")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        raise InputError("invalid rational") from error
    if max(result.numerator.bit_length(), result.denominator.bit_length()) > 1024:
        raise InputError("rational too large")
    return result


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and bool(value.strip())


def _pair(interval):
    return [str(interval[0]), str(interval[1])]


def _arm(value, selected, endpoint):
    if type(value) is not dict or set(value) != {"state", "value"}:
        raise InputError("arm requires exactly state and value")
    state, score = value["state"], value["value"]
    if type(state) is not str:
        raise InputError("malformed score state")
    if state in NONDEFINED:
        if score is not None:
            raise InputError("nondefined state must retain a null value")
        return None, None, state
    if state in ("missing_defined_score", "unselected"):
        if score is not None or (state == "unselected" and selected):
            raise InputError("inconsistent missing or unselected state")
        return None, (Fraction(0), Fraction(1)), state
    if state not in ("observed_score", "declared_failed_work_gate"):
        raise InputError("unsupported score state")
    if state == "declared_failed_work_gate" and endpoint != "P2":
        raise InputError("P1 has no failed-work gate")
    score = rational(score)
    if not 0 <= score <= 1:
        raise InputError("normalized score outside [0,1]")
    if state == "declared_failed_work_gate" and score != 0:
        raise InputError("declared failed-work gate must be exact zero")
    return score, (score, score), state


def _outcome(value, selected, endpoint):
    if endpoint == "P1":
        point, interval, state = _arm(value, selected, endpoint)
        return point, interval, {"scalar": state}
    if type(value) is not dict or set(value) != {"full", "fresh"}:
        raise InputError("P2 requires exactly full and fresh normalized arms")
    full, fi, fs = _arm(value["full"], selected, endpoint)
    fresh, ri, rs = _arm(value["fresh"], selected, endpoint)
    # The authenticated adapter must validate a shared original-work gate.
    # This local check also rejects an immediately inconsistent gate pair.
    if (fs == "declared_failed_work_gate") != (rs == "declared_failed_work_gate"):
        raise InputError("a declared failed-work pair requires both gate zeros")
    point = None if full is None or fresh is None else full - fresh
    interval = None if fi is None or ri is None else (fi[0] - ri[1], fi[1] - ri[0])
    return point, interval, {"full": fs, "fresh": rs}


def _prepare(endpoint, cells, rows):
    if type(endpoint) is not str or endpoint not in ENDPOINTS:
        raise InputError("endpoint must be P1 or P2")
    if type(cells) is not dict or not 1 <= len(cells) <= MAX_CELLS:
        raise InputError("cells must be a bounded nonempty weight mapping")
    if not all(_text(cell) for cell in cells):
        raise InputError("invalid cell identifier")
    weights = {cell: rational(weight) for cell, weight in cells.items()}
    if any(weight < 0 for weight in weights.values()) or sum(weights.values()) != 1:
        raise InputError("fixed nonnegative cell weights must sum exactly to one")
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise InputError("rows must be a bounded list of all original records")
    seen = set()
    clean = []
    for row in rows:
        if type(row) is not dict or set(row) != ROW_FIELDS:
            raise InputError("incorrect original record fields")
        if not all(_text(row[field]) for field in ("original_id", "cell_id", "bundle_id")):
            raise InputError("invalid original, cell, or bundle identifier")
        oid = row["original_id"]
        if oid in seen:
            raise InputError("duplicate original_id: " + oid)
        seen.add(oid)
        if row["cell_id"] not in weights:
            raise InputError("unregistered cell: " + row["cell_id"])
        if type(row["label"]) is not str or row["label"] not in LABELS:
            raise InputError("invalid measured-class label")
        if type(row["selected"]) is not bool:
            raise InputError("selected must be Boolean")
        pi = rational(row["pi"])
        if not 0 < pi <= 1:
            raise InputError("inclusion probability must be in (0,1]")
        if pi == 1 and not row["selected"]:
            raise InputError("pi=1 original cannot be unselected")
        if endpoint == "P1" and pi != 1:
            raise InputError("P1 requires always-selected originals")
        point, interval, states = _outcome(row["outcome"], row["selected"], endpoint)
        clean.append({"original_id": oid, "cell_id": row["cell_id"],
                      "bundle_id": row["bundle_id"], "label": row["label"],
                      "selected": row["selected"], "pi": pi,
                      "point": point, "interval": interval, "states": states})
    return weights, clean


def _log_upper(argument, terms=48):
    """Exact upper bound for log(argument), using the positive atanh series.

    Called only for 2 and 5/4. The tail bound follows from replacing every
    omitted odd denominator by its smallest value, then summing a geometric
    series. No floating-point transcendental operation enters an envelope.
    """
    z = (argument - 1) / (argument + 1)
    partial = 2 * sum((z ** (2*k + 1) / (2*k + 1) for k in range(terms)), Fraction(0))
    tail = 2 * z ** (2*terms + 1) / ((2*terms + 1) * (1 - z*z))
    return partial + tail


_LOG80_UPPER = 6 * _log_upper(Fraction(2)) + _log_upper(Fraction(5, 4))


def _radius_upper(sum_squared_ranges):
    """Conservatively round sqrt(log(80) * sum R_g^2 / 2) upwards."""
    squared = _LOG80_UPPER * sum_squared_ranges / 2
    scale = 10**24
    numerator = squared.numerator * scale * scale
    denominator = squared.denominator
    root = isqrt(numerator // denominator)
    if root * root * denominator < numerator:
        root += 1
    return Fraction(root, scale)


def _base(endpoint):
    return {"endpoint": endpoint, "status": "invalid_input",
            "label_target": "frozen_measured_operational_classes",
            "MAIN_qualified": False, "coverage_status": "interval_not_qualified",
            "qualification": "external_endpoint_provenance_and_randomization_verification_missing",
            "alpha": str(ALPHA), "simultaneous_family": "two_predeclared_primaries_Bonferroni",
            "point_estimate": None, "estimator_completion_interval": None,
            "conditional_branch_envelope": None,
            "conditional_envelope_intersected_estimand_support": None,
            "known_estimand_support": None, "conditional_branch_bound": None,
            "excluded_from_claim": ["source_population", "model_generation", "causal_effect",
                                    "label_validity", "reliability"]}


def calculate(endpoint, cells, rows):
    """Fixed-denominator HT arithmetic and an explicitly hypothetical bound.

    No argument can certify independence or MAIN applicability. The numerical
    envelope is a conditional implication under the assumptions in CONTRACT.md.
    All supplied originals, selected or not, determine class denominators.
    """
    result = _base(endpoint if type(endpoint) is str else None)
    try:
        weights, clean = _prepare(endpoint, cells, rows)
    except InputError as error:
        result["error"] = str(error)
        return result
    counts = {cell: {label: 0 for label in LABELS} for cell in weights}
    for row in clean:
        counts[row["cell_id"]][row["label"]] += 1
    unsupported = [{"cell_id": cell, "label": label} for cell, weight in weights.items()
                   if weight > 0 for label in ("positive", "no_expression")
                   if counts[cell][label] == 0]
    result.update(fixed_cell_weights={cell: str(weight) for cell, weight in weights.items()},
                  all_original_class_counts=counts, all_original_count=len(clean),
                  selected_original_count=sum(row["selected"] for row in clean),
                  unknown_label_original_ids=[row["original_id"] for row in clean if row["label"] == "unknown"],
                  other_known_original_ids=[row["original_id"] for row in clean if row["label"] == "other_known"],
                  score_states={row["original_id"]: row["states"] for row in clean},
                  unsupported_required_classes=unsupported)
    if unsupported:
        result["status"] = "not_estimable"
        return result
    support = (Fraction(-1), Fraction(1)) if endpoint == "P1" else (Fraction(-2), Fraction(2))
    result["known_estimand_support"] = _pair(support)
    lo = hi = Fraction(0)
    all_selected_points = True
    blocked = []
    coefficient_rows = []
    bundle_ranges = {}
    outcome_lower, outcome_upper = ENDPOINTS[endpoint]
    for row in clean:
        label, cell = row["label"], row["cell_id"]
        coefficient = Fraction(0)
        if weights[cell] and label in ("positive", "no_expression"):
            coefficient = weights[cell] / counts[cell][label]
            if label == "no_expression":
                coefficient = -coefficient
        multiplier = coefficient / row["pi"]
        coefficient_rows.append({"original_id": row["original_id"], "coefficient": str(coefficient),
                                 "selected": row["selected"], "pi": str(row["pi"]),
                                 "ht_multiplier": str(multiplier)})
        # A known unavailable/undefined required outcome does not become a
        # valid completion because a different, defined endpoint is bounded.
        if coefficient and row["interval"] is None:
            blocked.append({"original_id": row["original_id"], "states": row["states"]})
        if coefficient and row["selected"]:
            if row["interval"] is not None:
                products = (multiplier * row["interval"][0], multiplier * row["interval"][1])
                lo += min(products)
                hi += max(products)
            if row["point"] is None:
                all_selected_points = False
        bounds = bundle_ranges.setdefault(row["bundle_id"], [Fraction(0), Fraction(0)])
        if row["pi"] != 1:
            values = (Fraction(0), multiplier * outcome_lower, multiplier * outcome_upper)
            bounds[0] += min(values)
            bounds[1] += max(values)
    result["coefficients"] = coefficient_rows
    result["blocked_endpoint_originals"] = blocked
    if blocked:
        result["status"] = "endpoint_not_defined_or_unavailable"
        # Even its purported support is conditional on a defined endpoint.
        result["known_estimand_support"] = None
        return result
    ranges = {bundle: {"optional_contribution_bounds": _pair(bounds),
                       "range_width": str(bounds[1] - bounds[0])}
              for bundle, bounds in bundle_ranges.items()}
    sum_squared = sum(((bounds[1] - bounds[0])**2 for bounds in bundle_ranges.values()), Fraction(0))
    radius = _radius_upper(sum_squared)
    envelope = (lo - radius, hi + radius)
    lower_intersection = max(support[0], envelope[0])
    upper_intersection = min(support[1], envelope[1])
    intersection = _pair((lower_intersection, upper_intersection)) if lower_intersection <= upper_intersection else []
    result.update(status="calculated_conditional_only", point_estimate=str(lo) if all_selected_points else None,
                  estimator_completion_interval=_pair((lo, hi)),
                  conditional_branch_envelope=_pair(envelope),
                  conditional_envelope_intersected_estimand_support=intersection,
                  conditional_branch_bound={"status": "hypothetical_independent_bundle_implication_only",
                      "bundle_ranges": ranges, "sum_squared_ranges": str(sum_squared),
                      "radius_formula": "sqrt(log(2/alpha)*sum_squared_ranges/2)",
                      "radius_upper": str(radius), "radius_rounding": "exact_rational_log_upper_then_upward_sqrt_1e-24",
                      "always_selected_terms_have_zero_branch_range": True,
                      "actual_independence_verified": False})
    return result


def calculate_scenarios(endpoint, cells, rows, scenarios):
    """Explicit measured-label sensitivity; denominators change with labels.

    No branch envelope is reported for a scenario because its classification
    may depend on outcomes/selection. The input scenario is never a label truth
    claim or a confidence set for labels. Unspecified labels remain unchanged.
    """
    base = calculate(endpoint, cells, rows)
    answer = {"status": "invalid_scenarios", "base": base, "scenarios": [],
              "MAIN_qualified": False, "coverage_status": "interval_not_qualified"}
    if base["status"] == "invalid_input":
        answer["error"] = "invalid base input"
        return answer
    if type(scenarios) is not list or len(scenarios) > MAX_SCENARIOS:
        answer["error"] = "scenarios must be a bounded list"
        return answer
    known_ids = {row["original_id"] for row in rows}
    seen = set()
    prepared = []
    for scenario in scenarios:
        if (type(scenario) is not dict or set(scenario) != {"scenario_id", "labels"}
                or not _text(scenario["scenario_id"]) or scenario["scenario_id"] in seen
                or type(scenario["labels"]) is not dict):
            answer["error"] = "malformed or duplicate scenario"
            return answer
        seen.add(scenario["scenario_id"])
        changes = scenario["labels"]
        if any(type(oid) is not str or oid not in known_ids or type(label) is not str or label not in LABELS
               for oid, label in changes.items()):
            answer["error"] = "scenario has an unknown original or unsupported label"
            return answer
        changed_rows = [dict(row, label=changes.get(row["original_id"], row["label"])) for row in rows]
        prepared.append((scenario["scenario_id"], changes, changed_rows))
    for scenario_id, changes, changed_rows in prepared:
        calculated = calculate(endpoint, cells, changed_rows)
        for field in ("conditional_branch_bound", "conditional_branch_envelope",
                      "conditional_envelope_intersected_estimand_support"):
            calculated[field] = None
        calculated["scenario_branch_inference"] = "not_calculated_for_unverified_label_sensitivity"
        answer["scenarios"].append({"scenario_id": scenario_id, "label_overrides": dict(changes),
                                    "result": calculated})
    answer["status"] = "sensitivity_only"
    return answer
