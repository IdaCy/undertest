import math
import unittest

from causes.task_cluster_intervals import summarize_task_bounds


class TaskClusterIntervalsTests(unittest.TestCase):
    def test_unknown_completions_keep_the_null_possible(self):
        result = summarize_task_bounds({'a': (-1., 0.), 'b': (0., 1.)}, bootstrap_draws=50)
        self.assertEqual(result['assigned_completion_bounds'], [-.5, .5])
        self.assertEqual(result['hoeffding_p'], 1.)
        self.assertFalse(result['reject_zero_mean'])

    def test_complete_uniform_effect_has_degenerate_bootstrap(self):
        result = summarize_task_bounds({str(i): (1., 1.) for i in range(60)}, bootstrap_draws=50)
        self.assertEqual(result['assigned_completion_bounds'], [1., 1.])
        self.assertEqual(result['bootstrap_sensitivity_interval'], [1., 1.])
        self.assertTrue(result['bootstrap_degenerate'])
        self.assertTrue(result['reject_zero_mean'])
        self.assertAlmostEqual(result['hoeffding_radius'], .35066030352816463)
        self.assertAlmostEqual(result['hoeffding_p'], 2 * math.exp(-30))

    def test_same_task_draws_and_sign_reversal(self):
        bounds = {'a': (-.5, -.25), 'b': (.1, .2), 'c': (.3, .8)}
        result = summarize_task_bounds(bounds, bootstrap_draws=200, seed=9)
        reordered = summarize_task_bounds(dict(reversed(list(bounds.items()))), bootstrap_draws=200, seed=9)
        reverse = summarize_task_bounds({k: (-hi, -lo) for k, (lo, hi) in bounds.items()}, bootstrap_draws=200, seed=9)
        self.assertEqual(result, reordered)
        self.assertEqual(result['hoeffding_p'], reverse['hoeffding_p'])
        for a, b in zip(result['bootstrap_sensitivity_interval'], reversed(reverse['bootstrap_sensitivity_interval'])):
            self.assertAlmostEqual(a, -b)

    def test_refuses_invalid_intervals(self):
        for bounds in ({}, {'a': (1, 0)}, {'a': (-2, 0)}, {'a': (0, float('nan'))}):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                summarize_task_bounds(bounds, bootstrap_draws=10)


if __name__ == '__main__':
    unittest.main()
