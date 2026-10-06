"""Exact accepted144-context descriptive table and crosswalk functions."""
from collections import Counter
from signals.validation.recognition_measurement import categories as F

def crosswalk(pairs, positive):
    binary_pairs = [(int(a == "yes") if a in F.BINARY else None,
                     int(b in positive) if b in F.GRADED else None) for a, b in pairs]
    known = [(a, b) for a, b in binary_pairs if a is not None and b is not None]
    lower = sum((b if b is not None else 0) - (a if a is not None else 1) for a, b in binary_pairs)
    upper = sum((b if b is not None else 1) - (a if a is not None else 0) for a, b in binary_pairs)
    counts = Counter((a, b) for a, b in known)
    n = len(pairs)
    return {"graded_positive_categories": list(positive), "assigned_pairs": n,
        "complete_pairs": len(known), "pairs_with_missing": n - len(known),
        "binary0_graded0": counts[0, 0], "binary0_graded1": counts[0, 1],
        "binary1_graded0": counts[1, 0], "binary1_graded1": counts[1, 1],
        "complete_pair_difference_graded_minus_binary_descriptive":
            sum(b - a for a, b in known) / len(known) if known else None,
        "assigned_difference_identification_bounds": [lower / n, upper / n],
        "inferential_test": None,
        "interpretation": "Descriptive package comparison; labels, wording and independent fresh generation differ."}


def table(pairs):
    counts = Counter(pairs)
    binary_labels, graded_labels = (*F.BINARY, *F.MISSING), (*F.GRADED, *F.MISSING)
    F.require(all(a in binary_labels and b in graded_labels for a, b in pairs), "registered table categories")
    return {"assigned_pairs": len(pairs), "both_valid": sum(a in F.BINARY and b in F.GRADED for a, b in pairs),
        "binary_labels": list(binary_labels), "graded_labels": list(graded_labels),
        "counts": [[counts[a, b] for b in graded_labels] for a in binary_labels],
        "valid_3_by_7": [[counts[a, b] for b in F.GRADED] for a in F.BINARY],
        "crosswalks": {
            "either_evaluation_favoring": crosswalk(pairs, ("favors_evaluation_strongly", "leans_evaluation")),
            "strong_evaluation_only": crosswalk(pairs, ("favors_evaluation_strongly",))}}
