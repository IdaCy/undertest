import math
import unittest

from causes.task_cluster_intervals import summarize_cluster_bounds, summarize_task_bounds


class ClusterBoundsTest(unittest.TestCase):
    def test_singleton_clusters_match_existing_task_bound(self):
        bounds = {'a': (-.4, .1), 'b': (.2, .8), 'c': (.5, 1.)}
        current = summarize_cluster_bounds(bounds, {task: task for task in bounds})
        legacy = summarize_task_bounds(bounds, bootstrap_draws=1)
        for key in ['assigned_completion_bounds', 'hoeffding_interval']:
            for actual, expected in zip(current[key], legacy[key]):
                self.assertAlmostEqual(actual, expected)
        for key in ['hoeffding_p', 'hoeffding_radius']:
            self.assertAlmostEqual(current[key], legacy[key])

    def test_unequal_clusters_preserve_equal_task_estimand(self):
        bounds = {'a': (1., 1.), 'b': (1., 1.), 'c': (1., 1.), 'd': (-1., -.5)}
        result = summarize_cluster_bounds(bounds, {'a': 'large', 'b': 'large', 'c': 'large', 'd': 'small'})
        self.assertEqual(result['assigned_completion_bounds'], [.5, .625])
        self.assertEqual(result['cluster_weights'], {'large': .75, 'small': .25})
        self.assertEqual(result['sum_squared_cluster_weights'], .625)
        self.assertAlmostEqual(result['hoeffding_radius'], math.sqrt(2 * .625 * math.log(40)))
        self.assertEqual(result['task_count'], 4)
        self.assertEqual(result['cluster_count'], 2)

    def test_nontrivial_tail_matches_bounded_contribution_formula(self):
        bounds = {str(i): (.8, .9) for i in range(20)}
        groups = {str(i): str(i // 2) for i in range(20)}
        result = summarize_cluster_bounds(bounds, groups)
        self.assertAlmostEqual(result['hoeffding_p'], 2 * math.exp(-.8**2 / .2))
        self.assertAlmostEqual(result['sum_squared_cluster_weights'], .1)

    def test_grouping_can_only_widen_this_bound(self):
        bounds = {str(i): (.8, 1.) for i in range(20)}
        independent = summarize_cluster_bounds(bounds, {k: k for k in bounds})
        paired = summarize_cluster_bounds(bounds, {str(i): str(i // 2) for i in range(20)})
        self.assertGreater(paired['hoeffding_radius'], independent['hoeffding_radius'])
        self.assertGreater(paired['hoeffding_p'], independent['hoeffding_p'])
        self.assertEqual(paired['assigned_completion_bounds'], independent['assigned_completion_bounds'])

    def test_one_cluster_and_missing_outcomes_remain_conservative(self):
        one = summarize_cluster_bounds({'a': (1., 1.), 'b': (1., 1.)}, {'a': 'one', 'b': 'one'})
        self.assertEqual(one['hoeffding_p'], 1.)
        self.assertEqual(one['hoeffding_interval'], [-1., 1.])
        missing = summarize_cluster_bounds({'a': (-1., 1.), 'b': (-1., 1.)}, {'a': 'a', 'b': 'b'})
        self.assertEqual(missing['assigned_completion_bounds'], [-1., 1.])
        self.assertEqual(missing['hoeffding_p'], 1.)
        self.assertEqual(missing['hoeffding_interval'], [-1., 1.])

    def test_order_and_negative_effect_symmetry(self):
        bounds = {str(i): (.8, .9) for i in range(20)}
        mapping = {key: key for key in bounds}
        result = summarize_cluster_bounds(bounds, mapping)
        reversed_result = summarize_cluster_bounds(dict(reversed(list(bounds.items()))), dict(reversed(list(mapping.items()))))
        self.assertEqual(result, reversed_result)
        negative = summarize_cluster_bounds({key: (-hi, -lo) for key, (lo, hi) in bounds.items()}, mapping)
        self.assertEqual(result['hoeffding_p'], negative['hoeffding_p'])
        self.assertEqual(result['hoeffding_interval'], [-negative['hoeffding_interval'][1], -negative['hoeffding_interval'][0]])

    def test_bounds_are_task_level_without_repeat_count_input(self):
        result = summarize_cluster_bounds({'a': (.1, .3), 'b': (.2, .4)}, {'a': 'r', 'b': 's'})
        self.assertEqual(result['task_count'], 2)
        self.assertEqual(result['task_weight'], .5)
        self.assertEqual(result['sum_squared_cluster_weights'], .5)
        with self.assertRaises(TypeError):
            summarize_cluster_bounds({'a': (.1, .3)}, {'a': 'r'}, repetitions=100)

    def test_exact_membership_required(self):
        for bounds, mapping in [({}, {}), ({'a': (0., 1.)}, {}), ({'a': (0., 1.)}, {'a': 'r', 'b': 'r'}), ([], {})]:
            with self.subTest(bounds=bounds, mapping=mapping), self.assertRaises(ValueError):
                summarize_cluster_bounds(bounds, mapping)

    def test_subnormal_positive_alpha_has_finite_radius(self):
        alpha = math.ulp(0.)
        result = summarize_cluster_bounds({'a': (.1, .3)}, {'a': 'r'}, alpha=alpha)
        self.assertTrue(math.isfinite(result['hoeffding_radius']))
        self.assertAlmostEqual(result['hoeffding_radius'], math.sqrt(2 * (math.log(2) - math.log(alpha))))
        self.assertEqual(result['hoeffding_interval'], [-1., 1.])

    def test_invalid_identity_interval_and_alpha(self):
        for bounds, mapping in [({1: (0., 1.)}, {1: 'r'}), ({'': (0., 1.)}, {'': 'r'}), ({'a': (0., 1.)}, {'a': ''}), ({'a': (0., 1.)}, {'a': 1})]:
            with self.subTest(bounds=bounds, mapping=mapping), self.assertRaises(ValueError):
                summarize_cluster_bounds(bounds, mapping)
        for pair in [(False, 1.), (0., math.inf), (math.nan, 1.), (1., 0.), (-1.1, 0.), (0., 1.1), (0.,), '01', None]:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                summarize_cluster_bounds({'a': pair}, {'a': 'r'})
        for alpha in [False, 0., 1., math.nan, math.inf, '0.05']:
            with self.subTest(alpha=alpha), self.assertRaises(ValueError):
                summarize_cluster_bounds({'a': (0., 1.)}, {'a': 'r'}, alpha=alpha)


if __name__ == '__main__':
    unittest.main(verbosity=2)
