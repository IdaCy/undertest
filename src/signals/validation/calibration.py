"""Finite-sample calibration and sharp bounds under explicitly missing data.

Bounds concern the supplied assigned episodes, not a new-task population, and
are identification bounds rather than sampling-confidence intervals. Callers
must join every assignment, choose the intended label/view-availability policy,
and represent unavailable labels or scores with None before using these helpers.
"""

import heapq
import math
from collections import defaultdict
from fractions import Fraction

from signals.validation.controls.analysis import _channel
from signals.validation.controls.analysis import _probability
from signals.validation.controls.analysis import _records


def _comparator(value: str) -> str:
    if value not in (">", ">="):
        raise ValueError("comparator must be '>' or '>='")
    return value


def threshold_prediction(score: float, threshold: float, comparator: str = ">") -> bool:
    """Apply an explicit comparator; do not discard it when saving a threshold."""
    score = _probability(score, "score")
    threshold = _probability(threshold, "threshold")
    comparator = _comparator(comparator)
    return score > threshold if comparator == ">" else score >= threshold


def calibrate_negative_threshold(
    records: list[dict], channel: str, target_fpr: float = 0.05, *, comparator: str = ">",
) -> dict:
    """Choose the least restrictive feasible threshold on scored known negatives.

    The caller must supply development data. Missing negatives are counted but
    cannot calibrate an observed-score operating point. Thresholds lie in [0, 1].
    '>' excludes ties; '>=' includes them and uses nextafter to exclude a tie
    where possible. No finite threshold for '>=' can exclude a negative score 1.
    The comparator is part of the operating point; analysis.channel_metrics uses
    '>=' and must not receive a strict-'>' threshold value without adaptation.
    """
    records = _records(records)
    channel = _channel(channel)
    target_fpr = _probability(target_fpr, "target_fpr")
    comparator = _comparator(comparator)
    negatives = [row for row in records if row["label"] is False]
    scores = sorted(float(row["scores"][channel]) for row in negatives
                    if row["scores"].get(channel) is not None)
    result = {
        "channel": channel, "target_fpr": target_fpr, "comparator": comparator,
        "threshold_rule": f"score {comparator} threshold", "threshold": None,
        "calibration_population": "known development negatives with available scores",
        "n_assigned": len(records), "n_known_negatives": len(negatives),
        "n_known_positives": sum(row["label"] is True for row in records),
        "n_unknown_labels": sum(row["label"] is None for row in records),
        "n_scored_negatives": len(scores), "n_missing_negative_scores": len(negatives) - len(scores),
        "false_positive_count": None, "empirical_fpr": None,
        "n_negative_scores_equal_threshold": None, "status": "no_known_negatives",
        "generalization_note": "An empirical development operating point is not a held-out FPR guarantee.",
    }
    if not negatives:
        return result
    if not scores:
        result["status"] = "no_scored_negatives"
        return result
    candidates = {0.0}
    if comparator == ">":
        candidates.update(scores)
    else:
        candidates.update(math.nextafter(score, 1.0) for score in scores if score < 1)
    for threshold in sorted(candidates):
        false_positives = sum(threshold_prediction(score, threshold, comparator) for score in scores)
        empirical_fpr = false_positives / len(scores)
        if empirical_fpr <= target_fpr:
            result.update(
                threshold=threshold, false_positive_count=false_positives,
                empirical_fpr=empirical_fpr,
                n_negative_scores_equal_threshold=sum(score == threshold for score in scores),
                status="calibrated",
            )
            return result
    result["status"] = "unattainable_with_comparator"
    return result


def _labels(row: dict) -> tuple[bool, ...]:
    return (False, True) if row["label"] is None else (row["label"],)


def _decisions(score: float | None, threshold: float, comparator: str) -> tuple[bool, ...]:
    if score is not None:
        return (threshold_prediction(score, threshold, comparator),)
    return tuple(sorted({threshold_prediction(endpoint, threshold, comparator) for endpoint in (0.0, 1.0)}))


def _ratio_bounds(options: list[set[tuple[int, int]]]) -> dict:
    """Sharp sum(numerator)/sum(denominator), including undefined completions."""
    # For every reachable denominator, numerator extrema are jointly attainable.
    reachable = {0: (0, 0)}
    for choices in options:
        following = {}
        for denominator, (minimum, maximum) in reachable.items():
            for numerator_delta, denominator_delta in sorted(choices):
                target = denominator + denominator_delta
                low, high = minimum + numerator_delta, maximum + numerator_delta
                if target in following:
                    previous_low, previous_high = following[target]
                    low, high = min(low, previous_low), max(high, previous_high)
                following[target] = (low, high)
        reachable = following
    defined = [(denominator, low, high) for denominator, (low, high) in sorted(reachable.items())
               if denominator > 0]
    result = {
        "lower": None, "upper": None, "lower_counts": None, "upper_counts": None,
        "denominator_minimum": min(reachable), "denominator_maximum": max(reachable),
        "undefined_completion_possible": 0 in reachable,
        "bounds_conditioned_on_positive_denominator": True,
        "status": "undefined_for_all_completions",
    }
    if not defined:
        return result
    lower = min((Fraction(low, denominator), low, denominator) for denominator, low, _ in defined)
    upper = max((Fraction(high, denominator), high, denominator) for denominator, _, high in defined)
    result.update(
        lower=float(lower[0]), upper=float(upper[0]),
        lower_counts={"numerator": lower[1], "denominator": lower[2]},
        upper_counts={"numerator": upper[1], "denominator": upper[2]},
        status="bounds_with_undefined_completions" if 0 in reachable else "bounds",
    )
    return result


def _mean_bounds(intervals: list[tuple[float, float]]) -> dict:
    denominator = len(intervals)
    return {
        "lower": math.fsum(pair[0] for pair in intervals) / denominator if denominator else None,
        "upper": math.fsum(pair[1] for pair in intervals) / denominator if denominator else None,
        "denominator": denominator, "status": "bounds" if denominator else "no_assignments",
    }


def performance_bounds(
    records: list[dict], channel: str, threshold: float, *, comparator: str = ">",
) -> dict:
    """Sharp all-assigned recall/FPR/precision/Brier bounds with missing fields.

    Each missing label may be either boolean and each missing score any value in
    [0, 1]. Observed fields stay fixed. Ratio bounds optimize over completions
    with positive denominators and separately identify undefined completions.
    """
    records = _records(records)
    channel = _channel(channel)
    threshold = _probability(threshold, "threshold")
    comparator = _comparator(comparator)
    choices = {metric: [] for metric in ("fpr", "recall", "precision")}
    losses = []
    for row in records:
        score = row["scores"].get(channel)
        states = [(label, decision) for label in _labels(row)
                  for decision in _decisions(score, threshold, comparator)]
        choices["fpr"].append({(int(not label and decision), int(not label)) for label, decision in states})
        choices["recall"].append({(int(label and decision), int(label)) for label, decision in states})
        choices["precision"].append({(int(label and decision), int(decision)) for label, decision in states})
        possible_losses = [(value - int(label)) ** 2 for label in _labels(row)
                           for value in ((0.0, 1.0) if score is None else (score,))]
        losses.append((min(possible_losses), max(possible_losses)))
    known_positive = sum(row["label"] is True for row in records)
    unknown = sum(row["label"] is None for row in records)
    return {
        "channel": channel, "threshold": threshold, "comparator": comparator,
        "threshold_rule": f"score {comparator} threshold", "n_assigned": len(records),
        "n_unknown_labels": unknown,
        "n_missing_scores": sum(row["scores"].get(channel) is None for row in records),
        "population": "all supplied assigned episodes", "weighting": "episode",
        "interval_kind": "sharp identification bounds; not confidence intervals",
        "missing_score_domain": [0.0, 1.0],
        "prevalence": {"lower": known_positive / len(records) if records else None,
                       "upper": (known_positive + unknown) / len(records) if records else None,
                       "denominator": len(records)},
        **{metric: _ratio_bounds(options) for metric, options in choices.items()},
        "brier": _mean_bounds(losses),
    }


def paired_brier_bounds(records: list[dict], left: str, right: str) -> dict:
    """Sharp all-assigned mean loss(left)-loss(right); unknown labels are shared."""
    records = _records(records)
    left, right = _channel(left), _channel(right)
    losses = []
    for row in records:
        a, b = row["scores"].get(left), row["scores"].get(right)
        if left == right:
            losses.append((0.0, 0.0))
            continue
        differences = [(x - int(label)) ** 2 - (y - int(label)) ** 2
                       for label in _labels(row)
                       for x in ((0.0, 1.0) if a is None else (a,))
                       for y in ((0.0, 1.0) if b is None else (b,))]
        losses.append((min(differences), max(differences)))
    return {
        "left": left, "right": right, "direction": "left minus right",
        "n_assigned": len(records), "n_unknown_labels": sum(row["label"] is None for row in records),
        "n_missing_left_scores": sum(row["scores"].get(left) is None for row in records),
        "n_missing_right_scores": sum(row["scores"].get(right) is None for row in records),
        "population": "all supplied assigned episodes", "weighting": "episode",
        "interval_kind": "sharp identification bounds; not confidence intervals",
        "brier_difference": _mean_bounds(losses),
    }


def _category_records(records: list[dict], categories: tuple[str, ...], field: str) -> None:
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen, families = set(), {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("each record must be an object")
        for key in ("assignment_id", "case_id", "family"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"{key} must be a nonempty string")
        if row["assignment_id"] in seen:
            raise ValueError("duplicate assignment_id; resolve retries before analysis")
        seen.add(row["assignment_id"])
        if row["case_id"] in families and families[row["case_id"]] != row["family"]:
            raise ValueError("one case_id cannot belong to multiple families")
        families[row["case_id"]] = row["family"]
        if field not in row or (row[field] is not None and row[field] not in categories):
            raise ValueError("category must be explicitly declared or None for unavailable")


def _pairs(total: int) -> int:
    return total * (total - 1) // 2


def _category_summary(counts: dict[str, int], unavailable: int) -> dict:
    known = sum(counts.values())
    total = known + unavailable
    known_pairs, all_pairs = _pairs(known), _pairs(total)
    known_disagreements = known_pairs - sum(_pairs(count) for count in counts.values())
    concentrated = list(counts.values())
    largest = max(range(len(concentrated)), key=concentrated.__getitem__)
    concentrated[largest] += unavailable
    balanced = list(counts.values())
    heapq.heapify(balanced)
    for _ in range(unavailable):
        heapq.heappush(balanced, heapq.heappop(balanced) + 1)
    minimum = all_pairs - sum(_pairs(count) for count in concentrated)
    maximum = all_pairs - sum(_pairs(count) for count in balanced)
    return {
        "n_assigned": total, "n_known": known, "n_unavailable": unavailable,
        "category_counts": counts, "known_pairs": known_pairs, "all_pairs": all_pairs,
        "known_disagreements": known_disagreements, "unresolved_pairs": all_pairs - known_pairs,
        "minimum_disagreements": minimum, "maximum_disagreements": maximum,
        "known_disagreement_rate": known_disagreements / known_pairs if known_pairs else None,
        "disagreement_lower": minimum / all_pairs if all_pairs else None,
        "disagreement_upper": maximum / all_pairs if all_pairs else None,
    }


def categorical_within_case_disagreement(
    records: list[dict], categories: list[str] | tuple[str, ...], *, field: str = "category",
) -> dict:
    """Within-case verbalization disagreement with sharp missing-category bounds.

    The finite category alphabet must be prespecified, including possible unseen
    semantic categories. None alone denotes unavailable. A no-relevant-statement
    category is observed data, not evidence of absence of an internal belief.
    """
    if not isinstance(categories, (list, tuple)) or not categories:
        raise ValueError("categories must be a nonempty prespecified list or tuple")
    if any(not isinstance(category, str) or not category for category in categories):
        raise ValueError("categories must be nonempty strings")
    if len(set(categories)) != len(categories) or "unavailable" in categories:
        raise ValueError("categories must be unique; use None rather than an unavailable category")
    field = _channel(field)
    categories = tuple(categories)
    _category_records(records, categories, field)
    grouped = defaultdict(list)
    for row in records:
        grouped[row["case_id"]].append(row)
    cases = []
    for case_id, rows in sorted(grouped.items()):
        counts = {category: sum(row[field] == category for row in rows) for category in categories}
        cases.append({"case_id": case_id, "family": rows[0]["family"],
                      **_category_summary(counts, sum(row[field] is None for row in rows))})
    count_fields = ("n_assigned", "n_known", "n_unavailable", "known_pairs", "all_pairs",
                    "known_disagreements", "unresolved_pairs", "minimum_disagreements", "maximum_disagreements")
    pooled = {key: sum(case[key] for case in cases) for key in count_fields}
    pooled.update(
        known_disagreement_rate=pooled["known_disagreements"] / pooled["known_pairs"] if pooled["known_pairs"] else None,
        disagreement_lower=pooled["minimum_disagreements"] / pooled["all_pairs"] if pooled["all_pairs"] else None,
        disagreement_upper=pooled["maximum_disagreements"] / pooled["all_pairs"] if pooled["all_pairs"] else None,
    )
    return {
        "categories": list(categories), "field": field, "unavailable_value": None,
        "n_cases": len(cases), "n_cases_with_pairs": sum(case["all_pairs"] > 0 for case in cases),
        "n_cases_with_known_pairs": sum(case["known_pairs"] > 0 for case in cases),
        "weighting": "pooled within-case pairs; no cross-case pairs",
        "interval_kind": "sharp identification bounds; not confidence intervals",
        "cases": cases, "pooled": pooled,
    }
