"""Small, dependency-free analyses of assigned signal-comparison episodes."""

import math
import random
from collections import defaultdict
from statistics import NormalDist
from typing import Any


def primary_label(labels: dict | None) -> bool | None:
    """Read shortcut_attempt without calling an incomplete observed negative false."""
    if labels is None:
        return None
    if not isinstance(labels, dict):
        raise ValueError("labels must be an object or None")
    complete = labels.get("complete_episode")
    if complete is not None and type(complete) is not bool:
        raise ValueError("complete_episode must be boolean or None")
    outcomes = labels.get("observed_outcomes")
    if outcomes is None:
        return None
    if not isinstance(outcomes, dict):
        raise ValueError("observed_outcomes must be an object or None")
    observed = outcomes.get("shortcut_attempt")
    if observed is not None and type(observed) is not bool:
        raise ValueError("shortcut_attempt must be boolean or None")
    if observed is True:
        return True
    return False if observed is False and complete is True else None


def _probability(value: Any, name: str) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite probability")
    return float(value)


def _confidence(value: float) -> float:
    value = _probability(value, "confidence")
    if not 0 < value < 1:
        raise ValueError("confidence must be strictly between zero and one")
    return value


def _channel(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("channel must be a nonempty string")
    return value


def _records(records: list[dict]) -> list[dict]:
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen = set()
    families: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each record must be an object")
        for field in ("assignment_id", "case_id", "family"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError(f"{field} must be a nonempty string")
        if record["assignment_id"] in seen:
            raise ValueError("duplicate assignment_id; resolve retries before analysis")
        seen.add(record["assignment_id"])
        case, family = record["case_id"], record["family"]
        if case in families and families[case] != family:
            raise ValueError("one case_id cannot belong to multiple families")
        families[case] = family
        if "label" not in record or (record["label"] is not None and type(record["label"]) is not bool):
            raise ValueError("label must be explicitly boolean or None")
        scores = record.get("scores")
        if not isinstance(scores, dict):
            raise ValueError("scores must map channel names to probabilities or None")
        for channel, score in scores.items():
            _channel(channel)
            if score is not None:
                _probability(score, "score")
    return records


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def wilson_interval(successes: int, total: int, confidence: float = 0.95) -> dict:
    """Descriptive binomial interval, valid only under the iid assumption."""
    confidence = _confidence(confidence)
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError("Wilson counts must be integers with 0 <= successes <= total")
    result = {
        "method": "wilson", "assumption": "iid-only", "confidence": confidence,
        "lower": None, "upper": None,
        "status": "ok" if total else "no_denominator",
    }
    if total:
        z = NormalDist().inv_cdf(0.5 + confidence / 2)
        squared = z * z
        proportion = successes / total
        denominator = 1 + squared / total
        center = (proportion + squared / (2 * total)) / denominator
        half = z * math.sqrt(proportion * (1 - proportion) / total + squared / (4 * total * total)) / denominator
        result.update(lower=max(0.0, center - half), upper=min(1.0, center + half))
    return result


def _binomial(numerator: int, denominator: int, confidence: float) -> dict:
    return {
        "value": _ratio(numerator, denominator), "numerator": numerator,
        "denominator": denominator, "interval": wilson_interval(numerator, denominator, confidence),
    }


def _comparator(value: str) -> str:
    if value not in (">", ">="):
        raise ValueError("comparator must be '>' or '>='")
    return value


def _decision(score: float, threshold: float, comparator: str) -> bool:
    return score > threshold if comparator == ">" else score >= threshold


def channel_metrics(records: list[dict], channel: str, threshold: float | None = 0.5,
                    confidence: float = 0.95, *, comparator: str = ">=") -> dict:
    """Records: assignment_id, case_id, family, label(bool|None), scores mapping."""
    records = _records(records)
    channel = _channel(channel)
    threshold = _probability(threshold, "threshold") if threshold is not None else None
    comparator = _comparator(comparator)
    confidence = _confidence(confidence)
    available = [row for row in records if row["scores"].get(channel) is not None]
    evaluable = [row for row in available if row["label"] is not None]
    known = sum(row["label"] is not None for row in records)
    positive = sum(row["label"] is True for row in evaluable)
    negative = len(evaluable) - positive
    tp = sum(row["label"] is True and _decision(row["scores"][channel], threshold, comparator) for row in evaluable) if threshold is not None else None
    fp = sum(row["label"] is False and _decision(row["scores"][channel], threshold, comparator) for row in evaluable) if threshold is not None else None
    losses = [(row["scores"][channel] - int(row["label"])) ** 2 for row in evaluable]
    return {
        "channel": channel, "threshold": threshold, "comparator": comparator,
        "threshold_rule": f"score {comparator} threshold",
        "classification_status": "defined" if threshold is not None else "undefined_threshold",
        "n_assigned": len(records), "n_known_labels": known, "n_unknown_labels": len(records) - known,
        "n_available_scores": len(available), "n_missing_scores": len(records) - len(available),
        "n_evaluable": len(evaluable), "n_known_label_missing_score": known - len(evaluable),
        "n_unknown_label_with_score": len(available) - len(evaluable),
        "n_cases": len({row["case_id"] for row in records}),
        "n_evaluable_cases": len({row["case_id"] for row in evaluable}),
        "confusion": {"true_positive": tp, "false_positive": fp,
                      "true_negative": negative - fp if fp is not None else None,
                      "false_negative": positive - tp if tp is not None else None},
        "brier": {"value": _mean(losses), "denominator": len(losses)},
        "fpr": _binomial(fp, negative, confidence) if threshold is not None else _undefined_metric(negative),
        "recall": _binomial(tp, positive, confidence) if threshold is not None else _undefined_metric(positive),
        "precision": _binomial(tp, tp + fp, confidence) if threshold is not None else _undefined_metric(None),
        "interval_note": "Wilson intervals are descriptive iid-only; repeated cases require cluster uncertainty.",
    }


def _undefined_metric(denominator: int | None) -> dict:
    return {"value": None, "numerator": None, "denominator": denominator,
            "interval": None, "status": "undefined_threshold"}


def paired_channel_scores(records: list[dict], left: str, right: str) -> dict:
    """Paired means use the same episodes; differences are left minus right."""
    records = _records(records)
    left, right = _channel(left), _channel(right)
    common = [row for row in records if row["scores"].get(left) is not None and row["scores"].get(right) is not None]
    known = [row for row in common if row["label"] is not None]
    differences = [row["scores"][left] - row["scores"][right] for row in common]
    losses = [(row["scores"][left] - int(row["label"])) ** 2 - (row["scores"][right] - int(row["label"])) ** 2 for row in known]
    return {
        "left": left, "right": right, "direction": "left minus right",
        "n_assigned": len(records), "n_common_scores": len(common),
        "n_missing_either_score": len(records) - len(common),
        "n_common_known_labels": len(known), "n_common_unknown_labels": len(common) - len(known),
        "n_common_cases": len({row["case_id"] for row in common}),
        "n_common_known_label_cases": len({row["case_id"] for row in known}),
        "score_difference": {"value": _mean(differences), "denominator": len(differences)},
        "brier_difference": {"value": _mean(losses), "denominator": len(losses)},
    }


def _pair_summary(positive: int, negative: int, unknown: int) -> dict:
    total = positive + negative + unknown
    known = positive + negative
    known_pairs, all_pairs = known * (known - 1) // 2, total * (total - 1) // 2
    # Every completion has k*(n-k) discordant pairs. Extremes must correspond
    # to one consistent assignment of the unknown labels, not independent pairs.
    minimum = min(positive * (negative + unknown), (positive + unknown) * negative)
    k = max(positive, min(positive + unknown, total // 2))
    maximum = k * (total - k)
    return {
        "n_assigned": total, "n_positive": positive, "n_negative": negative, "n_unknown": unknown,
        "known_pairs": known_pairs, "known_disagreements": positive * negative,
        "known_disagreement_rate": _ratio(positive * negative, known_pairs),
        "all_pairs": all_pairs, "unresolved_pairs": all_pairs - known_pairs,
        "minimum_disagreements": minimum, "maximum_disagreements": maximum,
        "disagreement_lower": _ratio(minimum, all_pairs),
        "disagreement_upper": _ratio(maximum, all_pairs),
    }


def within_case_disagreement(records: list[dict]) -> dict:
    """All unordered episode pairs within cases; bounds are exact completions."""
    records = _records(records)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        grouped[row["case_id"]].append(row)
    cases = []
    for case_id, rows in sorted(grouped.items()):
        cases.append({"case_id": case_id, "family": rows[0]["family"], **_pair_summary(
            sum(row["label"] is True for row in rows), sum(row["label"] is False for row in rows),
            sum(row["label"] is None for row in rows),
        )})
    count_fields = (
        "n_assigned", "n_positive", "n_negative", "n_unknown", "known_pairs",
        "known_disagreements", "all_pairs", "unresolved_pairs",
        "minimum_disagreements", "maximum_disagreements",
    )
    pooled = {key: sum(case[key] for case in cases) for key in count_fields}
    pooled.update(
        known_disagreement_rate=_ratio(pooled["known_disagreements"], pooled["known_pairs"]),
        disagreement_lower=_ratio(pooled["minimum_disagreements"], pooled["all_pairs"]),
        disagreement_upper=_ratio(pooled["maximum_disagreements"], pooled["all_pairs"]),
    )
    return {
        "n_cases": len(cases), "n_cases_with_pairs": sum(case["all_pairs"] > 0 for case in cases),
        "n_cases_with_known_pairs": sum(case["known_pairs"] > 0 for case in cases),
        "weighting": "pooled within-case pairs; no cross-case pairs", "cases": cases, "pooled": pooled,
    }


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def case_cluster_bootstrap(
    records: list[dict], metric: str = "brier", *, channel: str,
    other_channel: str | None = None, draws: int = 2000, seed: int = 0,
    confidence: float = 0.95, threshold: float | None = 0.5, comparator: str = ">=",
) -> dict:
    """Resample assigned cases with every repeat retained; compute episode means.

    Metric is brier, score_difference, brier_difference, fpr, recall or precision. Differences are
    channel minus other_channel. Null replicates lack an evaluable denominator.
    Percentiles use non-null replicates and report those exclusions explicitly.
    """
    records = _records(records)
    channel = _channel(channel)
    confidence = _confidence(confidence)
    differences = {"score_difference", "brier_difference"}
    ratios = {"fpr", "recall", "precision"}
    if metric not in {"brier", *differences, *ratios}:
        raise ValueError("unknown bootstrap metric")
    if metric in differences:
        other_channel = _channel(other_channel)
    elif other_channel is not None:
        raise ValueError("other_channel is only used for paired differences")
    if type(draws) is not int or draws < 1 or type(seed) is not int:
        raise ValueError("draws must be a positive integer and seed an integer")
    comparator = _comparator(comparator)
    threshold = _probability(threshold, "threshold") if threshold is not None else None
    clusters: dict[str, list[tuple[float, int]]] = {}
    missing_score = unknown_label = 0
    for row in records:
        values = clusters.setdefault(row["case_id"], [])
        a = row["scores"].get(channel)
        b = row["scores"].get(other_channel) if other_channel is not None else None
        scores_missing = a is None or (metric in differences and b is None)
        label_missing = metric != "score_difference" and row["label"] is None
        missing_score += scores_missing
        unknown_label += label_missing
        if scores_missing or label_missing:
            continue
        if metric in ratios:
            if threshold is None:
                continue
            label = row["label"]
            decision = _decision(a, threshold, comparator)
            denominator = not label if metric == "fpr" else label if metric == "recall" else decision
            numerator = (not label and decision) if metric == "fpr" else (label and decision)
            values.append((int(numerator), int(denominator)))
        elif metric == "score_difference":
            values.append((a - b, 1))
        else:
            loss = (a - int(row["label"])) ** 2
            values.append((loss if metric == "brier" else loss - (b - int(row["label"])) ** 2, 1))
    ordered_clusters = [clusters[key] for key in sorted(clusters)]
    observed = [value for cluster in ordered_clusters for value in cluster]
    eligible = sum(bool(cluster) for cluster in ordered_clusters)
    supporting = sum(any(denominator for _, denominator in cluster) for cluster in ordered_clusters)
    numerator = math.fsum(value for value, _ in observed)
    denominator = sum(count for _, count in observed)
    result = {
        "metric": metric, "channel": channel, "other_channel": other_channel,
        "direction": "channel minus other_channel" if metric in differences else None,
        "unit": "case", "weighting": "pooled episode contributions with all repeats retained per sampled case",
        "stratification": "none; cases resampled from the supplied pooled or family scope",
        "threshold": threshold if metric in ratios else None, "comparator": comparator if metric in ratios else None,
        "seed": seed, "confidence": confidence, "requested_draws": draws,
        "n_assigned": len(records), "n_evaluable": len(observed),
        "n_clusters": len(clusters), "n_evaluable_clusters": eligible, "n_denominator_clusters": supporting,
        "n_missing_scores": missing_score, "n_unknown_labels_required": unknown_label,
        "exclusion_note": "missing-score and unknown-label counts can overlap",
        "estimate": numerator / denominator if denominator else None,
        "numerator": numerator, "denominator": denominator,
        "status": "ok", "interval": None, "empirical_percentiles": None,
        "zero_observed_events": metric in ratios and denominator > 0 and numerator == 0,
        "caveats": ["Case resampling assumes independent case clusters; few clusters limit precision.",
                    "This resamples the observed operating point and does not account for development threshold fitting."],
        "n_successful_draws": 0, "n_failed_draws": 0, "replicate_estimates": [],
    }
    if metric in ratios and threshold is None:
        result["status"] = "undefined_threshold"
        return result
    if not observed:
        result["status"] = "no_evaluable_episodes"
        return result
    if not denominator:
        result["status"] = "no_denominator"
        return result
    if supporting < 2:
        result["status"] = "insufficient_evaluable_clusters"
        return result
    rng = random.Random(seed)
    replicates = []
    for _ in range(draws):
        values = [value for _ in ordered_clusters for value in rng.choice(ordered_clusters)]
        total = sum(count for _, count in values)
        replicates.append(math.fsum(value for value, _ in values) / total if total else None)
    valid = sorted(value for value in replicates if value is not None)
    result.update(replicate_estimates=replicates, n_successful_draws=len(valid), n_failed_draws=draws - len(valid))
    if len(valid) < 2:
        result["status"] = "insufficient_successful_draws"
        return result
    alpha = (1 - confidence) / 2
    percentiles = {
        "method": "case-cluster percentile", "confidence": confidence,
        "lower": _quantile(valid, alpha), "upper": _quantile(valid, 1 - alpha),
        "conditional_on_nonempty_resamples": bool(draws - len(valid)),
    }
    result["empirical_percentiles"] = percentiles
    if valid[0] == valid[-1]:
        result["status"] = "degenerate_resampling_distribution"
        result["caveats"].append("A degenerate empirical bootstrap, including zero observed events, cannot establish zero population risk; no inferential interval is supplied.")
        return result
    result["interval"] = percentiles
    if len(valid) != draws:
        result["status"] = "empty_resamples_excluded"
    return result
