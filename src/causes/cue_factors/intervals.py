"""Registered paired-difference intervals with assigned-position missingness."""

import math

def difference_summary(pairs, alpha=.05):
    """Hoeffding for independent paired differences in [-1,1], with missing bounds."""
    if not pairs or not 0 < alpha < 1:
        raise ValueError("nonempty pairs and valid alpha required")
    low_sum, high_sum, complete = 0, 0, []
    for before, after in pairs:
        if before not in (0, 1, None) or after not in (0, 1, None):
            raise ValueError("binary or unknown required")
        possibilities = [b - a for a in ((0, 1) if before is None else (before,))
                         for b in ((0, 1) if after is None else (after,))]
        low_sum += min(possibilities)
        high_sum += max(possibilities)
        if before is not None and after is not None:
            complete.append(after - before)
    n = len(pairs)
    lo, hi = low_sum / n, high_sum / n
    # P(|mean - E(mean)| >= t) <= 2 exp(-n*t^2/2), range length two.
    radius = math.sqrt(2 * math.log(2 / alpha) / n)
    return {"assigned_pairs": n, "complete_pairs": len(complete),
            "assigned_difference_identification_bounds": [lo, hi],
            "complete_pair_difference_descriptive": sum(complete) / len(complete) if complete else None,
            "pointwise95_mean_difference_interval": [max(-1, lo - radius), min(1, hi + radius)],
            "interval_method": "paired bounded-difference Hoeffding; envelope over all missing completions",
            "interval_scope": "independent assigned attempts; conservative pointwise95, not simultaneous"}
