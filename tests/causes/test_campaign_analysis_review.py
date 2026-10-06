import itertools
import math
import unittest

from causes.campaign_analysis import contrast, holm, paired_stage


def small_assignment():
    return {
        'factor': 'F',
        'members': {
            'one': {'treatment': ['t1'], 'control': ['c1']},
            'two': {'treatment': ['t2', 't3'], 'control': ['c2', 'c3']},
        },
    }


IDS = ('t1', 'c1', 't2', 't3', 'c2', 'c3')


def independent_effect(values):
    return ((values['t1'] - values['c1']) +
            (values['t2'] + values['t3'] - values['c2'] - values['c3']) / 2) / 2


class IndependentAnalysisReviewTests(unittest.TestCase):
    def test_bounds_are_sharp_over_every_completion_of_every_missingness_pattern(self):
        for observations in itertools.product((None, 0, 1), repeat=len(IDS)):
            observed = dict(zip(IDS, observations))
            labels = {key: ('yes' if value else 'no') for key, value in observed.items()
                      if value is not None}
            result = contrast(small_assignment(), labels, release=True)
            missing = [key for key, value in observed.items() if value is None]
            effects = []
            for completion in itertools.product((0, 1), repeat=len(missing)):
                completed = dict(observed)
                completed.update(zip(missing, completion))
                effects.append(independent_effect(completed))
            self.assertEqual(result['missing_assigned_sample_effect_bounds'],
                             [min(effects), max(effects)])
            self.assertEqual(result['all_assigned_observed'], not missing)
            self.assertEqual(result['equal_task_effect'], effects[0] if not missing else None)

    def test_exact_enumeration_attains_variance_bound_at_half_probability(self):
        effects = [independent_effect(dict(zip(IDS, draws)))
                   for draws in itertools.product((0, 1), repeat=len(IDS))]
        self.assertEqual(sum(effects) / len(effects), 0)
        variance = sum(value * value for value in effects) / len(effects)
        bound = contrast(small_assignment(), {})['se_upper_bound'] ** 2
        self.assertAlmostEqual(variance, bound)

    def test_heterogeneous_null_bernoulli_variance_is_below_bound(self):
        probabilities = dict(zip(IDS, (.1, .1, .8, .8, .8, .8)))
        mean, second_moment, mass = 0., 0., 0.
        for draws in itertools.product((0, 1), repeat=len(IDS)):
            values = dict(zip(IDS, draws))
            probability = math.prod(probabilities[key] if value else 1 - probabilities[key]
                                    for key, value in values.items())
            effect = independent_effect(values)
            mass += probability
            mean += probability * effect
            second_moment += probability * effect * effect
        self.assertAlmostEqual(mass, 1)
        self.assertAlmostEqual(mean, 0)
        self.assertLess(second_moment - mean ** 2,
                        contrast(small_assignment(), {})['se_upper_bound'] ** 2)

    def test_paired_missingness_bounds_are_sharp_with_observed_endpoints(self):
        ids = ('a', 'b')
        for values in itertools.product((None, 0, 1), repeat=4):
            early = dict(zip(ids, values[:2]))
            end = dict(zip(ids, values[2:]))
            labels = lambda rows: {key: ('yes' if value else 'no')
                                   for key, value in rows.items() if value is not None}
            result = paired_stage(ids, labels(early), labels(end), release=True)
            possibilities = [((0, 1) if value is None else (value,)) for value in values]
            effects = [(completion[2] + completion[3] - completion[0] - completion[1]) / 2
                       for completion in itertools.product(*possibilities)]
            self.assertEqual(result['missing_assigned_sample_effect_bounds'],
                             [min(effects), max(effects)])

    def test_missingness_p_value_is_never_smaller_than_any_complete_sample_p(self):
        for observations in itertools.product((None, 0, 1), repeat=len(IDS)):
            observed = dict(zip(IDS, observations))
            labels = {key: ('yes' if value else 'no') for key, value in observed.items()
                      if value is not None}
            incomplete = contrast(small_assignment(), labels, release=True)
            missing = [key for key, value in observed.items() if value is None]
            for completion in itertools.product((0, 1), repeat=len(missing)):
                completed_labels = dict(labels)
                completed_labels.update((key, 'yes' if value else 'no')
                                        for key, value in zip(missing, completion))
                complete = contrast(small_assignment(), completed_labels, release=True)
                for name in ('approximate_p', 'hoeffding_sensitivity_p'):
                    self.assertGreaterEqual(incomplete[name] + 1e-14, complete[name])

    def test_holm_matches_step_down_rejections_with_ties_and_reordered_families(self):
        pvalues = {'a': .001, 'b': .009, 'c': .009, 'd': .04, 'e': 1.}
        adjusted = holm(pvalues)
        for alpha in (.01, .025, .05, .1):
            accepted = set()
            for rank, (name, value) in enumerate(sorted(pvalues.items(), key=lambda item: (item[1], item[0]))):
                if value > alpha / (len(pvalues) - rank):
                    break
                accepted.add(name)
            self.assertEqual({name for name, value in adjusted.items() if value <= alpha}, accepted)
        self.assertEqual(holm(dict(reversed(list(pvalues.items())))), adjusted)


if __name__ == '__main__':
    unittest.main()
