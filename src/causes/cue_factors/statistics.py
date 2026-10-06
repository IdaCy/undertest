"""Finite-library, prospectively registered calculations; no model calls."""
from math import comb, exp, log, log1p


def bernoulli_kl(x, p):
    if not 0 <= x <= 1 or not 0 <= p <= 1:
        raise ValueError("probability outside [0,1]")
    if p in (0, 1):
        return 0.0 if x == p else float("inf")
    if x == 0:
        return -log1p(-p)
    if x == 1:
        return -log(p)
    return x * log(x / p) + (1 - x) * log((1 - x) / (1 - p))


def mean_bounds(yes, n, alpha=0.05):
    """Separate one-sided bounds for independent, nonidentical Bernoullis.

    Concavity gives product_i(1-p_i+p_i*exp(t)) <=
    (1-p_bar+p_bar*exp(t))**n. Chernoff optimization therefore bounds
    each relevant tail by exp(-n*KL(observed_mean || p_bar)). Invert
    each tail at alpha. Both bounds together have coverage >=1-2*alpha.
    Set alpha=.025 for a two-sided 95% interval. A fixed mixture of
    task/variant probabilities is allowed; independence is required.
    """
    if not isinstance(yes, int) or not isinstance(n, int) or not 0 <= yes <= n or n <= 0:
        raise ValueError("invalid Bernoulli counts")
    if not 0 < alpha < 1:
        raise ValueError("invalid alpha")
    x, threshold = yes / n, -log(alpha) / n
    bounds = []
    for upper in (False, True):
        if (upper and yes == n) or (not upper and yes == 0):
            bounds.append(float(upper))
            continue
        low, high = (x, 1.0) if upper else (0.0, x)
        for _ in range(70):
            mid = (low + high) / 2
            if (bernoulli_kl(x, mid) < threshold) == upper:
                low = mid
            else:
                high = mid
        bounds.append((low + high) / 2)
    return tuple(bounds)


def paired_randomization_p(positive, negative):
    """Exact two-sided endpoint-label swap test, conditional on pair discordance.

    Each block has randomly assigned ordinary/strong endpoint seed slots.
    Concordant pairs contribute zero; swapping labels flips every nonzero
    contrast independently. This tests the sharp endpoint no-effect null.
    """
    if any(not isinstance(v, int) or v < 0 for v in (positive, negative)):
        raise ValueError("invalid paired counts")
    n = positive + negative
    return min(1.0, 2 * sum(comb(n, k) for k in range(min(positive, negative) + 1)) / (2 ** n))


def conservative_missing_pair_test(pairs):
    """Maximize exact p over every binary completion of missing outcomes.

    Each pair is (ordinary, strongest), with 0/1 known and None unknown.
    All assigned pairs remain in the denominator. Max-p is conservative
    because the actual unknown completion is included in this finite set.
    """
    states = {(0, 0)}
    count = missing = 0
    for ordinary, strongest in pairs:
        if ordinary not in (0, 1, None) or strongest not in (0, 1, None):
            raise ValueError("binary outcome or None required")
        count += 1
        missing += ordinary is None or strongest is None
        a = (0, 1) if ordinary is None else (ordinary,)
        b = (0, 1) if strongest is None else (strongest,)
        deltas = {(int(y > x), int(y < x)) for x in a for y in b}
        states = {(p + dp, m + dm) for p, m in states for dp, dm in deltas}
    if count == 0:
        raise ValueError("assigned pairs required")
    return {
        "assigned_pairs": count,
        "pairs_with_missing": missing,
        "p_worst_completion": max(paired_randomization_p(p, m) for p, m in states),
        "effect_identification_lower": min((p - m) / count for p, m in states),
        "effect_identification_upper": max((p - m) / count for p, m in states),
        "possible_count_states": len(states),
    }


def holm_adjust(pvalues):
    ordered = sorted(enumerate(pvalues), key=lambda item: item[1])
    if any(not 0 <= p <= 1 for p in pvalues):
        raise ValueError("invalid p-value")
    result, running = [None] * len(ordered), 0.0
    for rank, (original, p) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - rank) * p))
        result[original] = running
    return result


def endpoint_power(n, ordinary_rate, strongest_rate, alpha=0.05 / 8):
    """Exact independent-generation illustration for the paired test."""
    discord = strongest_rate * (1 - ordinary_rate) + ordinary_rate * (1 - strongest_rate)
    if discord == 0:
        return 0.0
    positive = strongest_rate * (1 - ordinary_rate) / discord
    power = 0.0
    for m in range(n + 1):
        probability_m = comb(n, m) * discord ** m * (1 - discord) ** (n - m)
        rejection = sum(
            comb(m, k) * positive ** k * (1 - positive) ** (m - k)
            for k in range(m + 1)
            if paired_randomization_p(k, m - k) <= alpha
        )
        power += probability_m * rejection
    return power
