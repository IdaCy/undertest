import math

import unittest

from causes.campaign_analysis import contrast, contrast_family, holm, paired_stage, reader_label, self_label


def allocation():
    return {'factor': 'F01', 'members': {
        'task_a': {'treatment': ['a_t'], 'control': ['a_c']},
        'task_b': {'treatment': ['b_t1', 'b_t2', 'b_t3'], 'control': ['b_c1', 'b_c2', 'b_c3']}}}


class AnalysisTests(unittest.TestCase):
    def test_equal_task_weights_do_not_pool_unequal_repetitions(self):
        labels = {'a_t': 'yes', 'a_c': 'no', 'b_t1': 'no', 'b_t2': 'no', 'b_t3': 'no',
                  'b_c1': 'no', 'b_c2': 'no', 'b_c3': 'no'}
        result = contrast(allocation(), labels, release=True)
        assert result['equal_task_effect'] == .5  # pooled difference would be .25
        assert result['effective_per_arm'] == 3
        assert math.isclose(result['se_upper_bound'], math.sqrt(1/6))
        assert result['category_equal_task_weights']['treatment']['yes'] == .5


    def test_missing_cells_remain_assigned_and_bounds_include_both_extremes(self):
        result = contrast(allocation(), {'a_t': 'yes', 'a_c': 'no'}, release=True)
        assert result['missing_assigned_sample_effect_bounds'] == [0, 1]
        assert result['equal_task_effect'] is None
        assert result['approximate_p'] == 1
        assert result['assigned'] == {'treatment': 4, 'control': 4}
        assert result['observed'] == {'treatment': 1, 'control': 1}


    def test_all_missing_is_uninformative_and_never_zero_effect(self):
        result = contrast(allocation(), {})
        assert result['missing_assigned_sample_effect_bounds'] == [-1, 1]
        assert result['equal_task_effect'] is None
        assert result['approximate_p'] is None
        assert result['bonferroni_approximate_interval'] is None


    def test_extra_unassigned_control_cannot_enter_comparison(self):
        labels = {'a_t': 'yes', 'a_c': 'no'}
        assert contrast(allocation(), labels) == contrast(allocation(), {**labels, 'unassigned_control': 'yes'})


    def test_holm_adjustment_monotonic_and_order_independent(self):
        assert holm({'c': .2, 'b': .03, 'a': .01}) == {'a': .03, 'b': .06, 'c': .2}


    def test_family_inference_stays_unreleased_until_assignment_resolved(self):
        result = contrast_family([allocation()], {})[0]
        assert result['approximate_p_holm'] is None
        assert result['hoeffding_sensitivity_p_holm'] is None


    def test_pair_missing_bound_uses_observed_endpoint(self):
        result = paired_stage(['a', 'b'], {'a': 'yes'}, {'b': 'yes'}, release=True)
        assert result['missing_assigned_sample_effect_bounds'] == [-.5, .5]
        assert result['observed_complete_pairs'] == 0
        assert result['paired_effect'] is None
        assert result['approximate_p'] == 1


    def test_pair_transitions_do_not_conflate_non_yes_categories(self):
        result = paired_stage(['a', 'b'], {'a': 'insufficient_evidence', 'b': 'no'}, {'a': 'yes', 'b': 'not_understandable'})
        assert result['paired_effect'] == .5
        assert result['observed_complete_pairs'] == 2
        assert len(result['transitions']) == 2


    def test_parser_only_accepts_complete_self_response(self):
        assert self_label({'technical_status': 'truncated', 'answer': 'no'}) is None
        assert self_label({'technical_status': 'complete', 'answer': 'insufficient_evidence'}) == 'insufficient_evidence'


    def test_reader_uses_admissibility_and_retains_partial_observed_positive(self):
        row = {'status': 'validated_partial_observation', 'judgments': {'evaluation': {'label': 'no', 'admissible_label': None}}}
        assert reader_label(row) is None
        row['judgments']['evaluation'].update(label='yes', admissible_label='yes')
        assert reader_label(row) == 'yes'


    def test_treatment_and_control_must_be_independent_physical_draws(self):
        row = {'factor': 'F', 'members': {'a': {'treatment': ['same'], 'control': ['same']}}}
        with self.assertRaisesRegex(ValueError, 'duplicate_or_shared'):
            contrast(row, {})

    def test_cross_task_physical_draw_reuse_is_rejected(self):
        row = allocation()
        row['members']['task_b']['treatment'][0] = 'a_t'
        with self.assertRaisesRegex(ValueError, 'reused_across_tasks'):
            contrast(row, {})

    def test_release_cannot_silently_shrink_frozen_family(self):
        with self.assertRaisesRegex(ValueError, 'requires_frozen_family'):
            contrast_family([allocation()], {}, release=True)
        with self.assertRaisesRegex(ValueError, 'membership_mismatch'):
            contrast_family([allocation()], {}, release=True, expected_factors=['F01', 'F02'], family_size=2)
