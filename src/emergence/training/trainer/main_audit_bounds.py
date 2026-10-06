"""Exact-integer inversion for the prospectively fixed AM audit endpoints."""
from math import comb


def upper_nonpass_count(population, sample, observed, alpha_denominator=40):
    """Largest population count not rejected by the lower hypergeometric tail.

    alpha=1/40 gives a conservative one-sided 97.5% upper confidence bound.
    Equality is retained. No floating point is used for the inversion.
    """
    if not (type(population) is int and type(sample) is int and
            type(observed) is int and 0 <= observed <= sample <= population):
        raise ValueError('integer 0 <= observed <= sample <= population required')
    if alpha_denominator != 40:
        raise ValueError('the adopted protocol fixes alpha at 1/40')
    denominator = comb(population, sample)

    def retained(nonpasses):
        numerator = sum(comb(nonpasses, j) * comb(population-nonpasses, sample-j)
                        for j in range(max(0, sample-(population-nonpasses)),
                                       min(observed, nonpasses, sample)+1))
        return 40*numerator >= denominator

    low, high = observed, population
    while low < high:
        mid = (low+high+1)//2
        if retained(mid): low = mid
        else: high = mid-1
    assert retained(low) and (low == population or not retained(low+1))
    return low


def summarize(population, sample, critical, general, *, all_double_reviews_complete,
              unresolved_systemic_defect):
    if not 0 <= critical <= general <= sample:
        raise ValueError('critical endpoint must be contained in general endpoint')
    critical_upper = upper_nonpass_count(population, sample, critical)
    general_upper = upper_nonpass_count(population, sample, general)
    return {
        'N': population, 'n': sample,
        'critical_nonpasses': critical, 'general_nonpasses': general,
        'critical_97_5_upper_count': critical_upper,
        'general_97_5_upper_count': general_upper,
        'critical_97_5_upper_rate': critical_upper/population if population else None,
        'general_97_5_upper_rate': general_upper/population if population else None,
        'critical_at_most_one_percent': population > 0 and 100*critical_upper <= population,
        'general_at_most_two_percent': population > 0 and 50*general_upper <= population,
        'all_double_reviews_complete': all_double_reviews_complete,
        'unresolved_systemic_defect': unresolved_systemic_defect,
        'audit_gate_pass': population > 0 and sample == min(384, population)
            and all_double_reviews_complete is True
            and unresolved_systemic_defect is False
            and 100*critical_upper <= population and 50*general_upper <= population,
        'coverage_is_conditional_on_accurate_labels': True,
        'not_a_posterior_probability_or_a_training_admission': True,
    }


if __name__ == '__main__':
    # Boundary cases exercise the statistical inversion, not an implementation copy.
    assert upper_nonpass_count(20, 20, 0) == 0
    assert upper_nonpass_count(20, 20, 7) == 7
    assert upper_nonpass_count(20, 0, 0) == 20
    assert upper_nonpass_count(20, 5, 5) == 20
    assert upper_nonpass_count(4096, 384, 0) == 37
    assert upper_nonpass_count(4096, 384, 1) == 56
    assert upper_nonpass_count(4096, 384, 2) == 73
    assert not summarize(4096, 384, 0, 0, all_double_reviews_complete=False,
                         unresolved_systemic_defect=False)['audit_gate_pass']
    assert not summarize(4096, 384, 0, 0, all_double_reviews_complete=True,
                         unresolved_systemic_defect=True)['audit_gate_pass']
    print('9 finite-population boundary and completion checks passed; no actual audit outcomes read')
