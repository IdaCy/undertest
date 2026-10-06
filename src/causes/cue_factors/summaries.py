"""Saved-category and paired-endpoint summaries; the caller supplies an authenticated assigned frame."""
from collections import Counter
from causes.cue_factors import statistics
from causes.cue_factors.intervals import difference_summary
from signals.validation.recognition_measurement.categories import BINARY, require

def categories(values, stats=statistics, intervals=True):
    counts = Counter(v if v in BINARY else 'unknown' for v in values)
    n, yes, unknown = len(values), counts['yes'], counts['unknown']
    require(n > 0, 'fixed assigned denominator')
    out = {'assigned': n, 'valid': n-unknown, **{k:counts[k] for k in (*BINARY,'unknown')},
           'observed_valid_affirmation_fraction_descriptive':yes/(n-unknown) if n>unknown else None,
           'assigned_mean_identification_bounds':[yes/n,(yes+unknown)/n]}
    if intervals:
        out['pointwise95_mean_interval_envelope_over_missing_completions'] = [
            stats.mean_bounds(yes,n,.025)[0],stats.mean_bounds(yes+unknown,n,.025)[1]]
    return out

def contrast(pairs, stats=statistics, primary=False):
    interval=difference_summary(pairs)
    known=[(a,b) for a,b in pairs if a is not None and b is not None]
    out={'assigned_pairs':len(pairs),'complete_pairs_descriptive':len(known),
         'pairs_with_missing':len(pairs)-len(known),
         'positive_discordant_complete_pairs':sum(a==0 and b==1 for a,b in known),
         'negative_discordant_complete_pairs':sum(a==1 and b==0 for a,b in known),
         'complete_pair_difference_descriptive':sum(b-a for a,b in known)/len(known) if known else None,
         'effect_identification_lower':interval['assigned_difference_identification_bounds'][0],
         'effect_identification_upper':interval['assigned_difference_identification_bounds'][1],
         'paired_sampling_interval':interval}
    if primary:
        exact=stats.conservative_missing_pair_test(pairs)
        require(all(exact[k]==out[k] for k in ('assigned_pairs','pairs_with_missing','effect_identification_lower','effect_identification_upper')),
                  'independent exact-test and interval identification agreement')
        out.update(exact)
    return out
