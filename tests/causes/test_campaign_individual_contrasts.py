import copy

import unittest

from causes.campaign_analysis import contrast
from causes.campaign_individual_contrasts import individual_contrasts


def observed(label):
    return {'status': 'observed', 'label': label}


def design():
    assignments = [
        {'factor': 'one', 'members': {
            'task_a': {'treatment': ['a1', 'a2'], 'control': ['b1', 'b2']},
            'task_b': {'treatment': ['c1', 'c2', 'c3', 'c4'], 'control': ['d1', 'd2', 'd3', 'd4']},
        }},
        {'factor': 'two', 'members': {
            'task_a': {'treatment': ['e1', 'e2'], 'control': ['b1', 'b2']},
        }},
    ]
    ids = {r for a in assignments for arms in a['members'].values() for group in arms.values() for r in group}
    values = {r: observed('yes' if r.startswith(('a', 'c', 'e')) else 'no') for r in ids}
    return assignments, {'self': copy.deepcopy(values), 'reader': copy.deepcopy(values)}


def run(assignments, readouts, **kwargs):
    return individual_contrasts(assignments, readouts, expected_factors=['one', 'two'],
                                primary_readouts=['self', 'reader'], **kwargs)


class IndividualContrastTests(unittest.TestCase):
    def test_each_contrast_releases_independently_and_shared_controls_are_not_duplicates(self):
        assignments, values = design()
        del values['reader']['e2']
        result = run(assignments, values)
        first, second = result['results']
        assert first['primary_inference_released'] is True
        assert second['primary_inference_released'] is False
        assert second['readouts']['self']['analysis'] is None
        assert second['readouts']['reader']['counts']['pending'] == 1
        assert second['readouts']['self']['experiment_wide_holm_p'] is None
        assert result['experiment_wide_family_size'] == 4
        assert result['all_primary_contrasts_released'] is False


    def test_explicit_missing_is_bounded_and_never_becomes_no(self):
        assignments, values = design()
        values['self']['a1'] = {'status': 'missing', 'reason': 'preserved_request_failure'}
        first = run(assignments, values)['results'][0]['readouts']['self']['analysis']
        assert first['equal_task_effect'] is None
        assert first['missing_assigned_sample_effect_bounds'] == [.75, 1.0]
        assert first['cells']['task_a']['treatment']['technical_missing'] == 1
        assert first['primary_alpha'] == .025
        assert first['pointwise_approximate_interval_95'][0] > first['bonferroni_approximate_interval'][0]


    def test_complete_result_preserves_native_equal_task_analysis(self):
        assignments, values = design()
        result = run(assignments, values)
        actual = result['results'][0]['readouts']['self']['analysis']
        expected = contrast(assignments[0], {r: v['label'] for r, v in values['self'].items()},
                            release=True, family_size=2)
        assert all(actual[k] == value for k, value in expected.items())
        self.assertAlmostEqual(actual['effective_per_arm'], 16 / 3)
        assert result['all_primary_contrasts_released'] is True


    def test_secondary_and_epoch_outputs_are_descriptive_and_keep_task_weights(self):
        assignments, values = design()
        values['strong'] = copy.deepcopy(values['self'])
        epochs = {r: 'old' if int(r[1:]) % 2 else 'new' for r in values['self']}
        result = run(assignments, values, epochs=epochs)
        entry = result['results'][0]['readouts']['strong']
        assert entry['analysis']['approximate_p'] is None
        assert entry['analysis']['primary_rejects'] is None
        assert entry['epochs_descriptive']['old']['task_count'] == 2
        assert entry['epochs_descriptive']['new']['inference_released'] is False
        del values['strong']['a1']
        assert run(assignments, values)['results'][0]['readouts']['strong']['analysis'] is None


    def test_invalid_measurement_states_refused(self):
        for bad in [
            {'status': 'missing'}, {'status': 'missing', 'reason': 'x', 'label': 'no'},
            {'status': 'pending', 'label': 'yes'}, {'status': 'observed', 'label': 'unknown'},
            {'status': 'finished'},
        ]:
            with self.subTest(record=bad):
                assignments, values = design()
                values['reader']['a1'] = bad
                with self.assertRaises(ValueError):
                    run(assignments, values)


    def test_frozen_membership_and_epoch_balance_cannot_drift(self):
        assignments, values = design()
        with self.assertRaisesRegex(ValueError, 'frozen_contrast_membership_mismatch'):
            run(assignments[:1], values)
        values['self']['unassigned'] = observed('yes')
        with self.assertRaisesRegex(ValueError, 'outside_frozen'):
            run(assignments, values)
        del values['self']['unassigned']
        epochs = {r: 'old' if int(r[1:]) % 2 else 'new' for r in values['self']}
        epochs['a1'] = 'new'
        with self.assertRaisesRegex(ValueError, 'unbalanced_task_arm_assignment'):
            run(assignments, values, epochs=epochs)


if __name__ == '__main__':
    unittest.main()
