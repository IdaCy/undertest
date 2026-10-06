import copy
import unittest

from causes.campaign_analysis import contrast_family, holm


class InferenceEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.factors = [f'F{i:02d}' for i in range(1, 12)]
        self.assignments = []
        self.labels = {}
        for index, factor in enumerate(self.factors):
            arms = {arm: [f'{factor}_{arm}_{i}' for i in range(100)]
                    for arm in ('treatment', 'control')}
            self.assignments.append({'factor': factor, 'members': {'task': arms}})
            for arm, ids in arms.items():
                positives = 100 if factor == 'F03' and arm == 'treatment' else (0 if factor == 'F03' else (65 + index if arm == 'treatment' else 40))
                self.labels.update({rid: 'yes' if i < positives else 'no' for i, rid in enumerate(ids)})

    def calculate(self, **kwargs):
        return contrast_family(self.assignments, self.labels, expected_factors=self.factors,
                               family_size=11, **kwargs)

    def test_ineligible_contrast_keeps_data_but_cannot_be_reported_as_tested(self):
        original_assignments, original_labels = copy.deepcopy(self.assignments), self.labels.copy()
        raw = self.calculate(release=True)
        result = self.calculate(release=True, ineligible_factors={'F03': 'stimulus isolation defect'})
        before, after = raw[2], result[2]
        self.assertLess(before['approximate_p'], .001)
        self.assertEqual(after['cells'], before['cells'])
        self.assertEqual(after['equal_task_effect'], 1)
        self.assertEqual(after['assigned'], before['assigned'])
        self.assertFalse(after['inference_eligible'])
        self.assertFalse(after['inference_released'])
        self.assertEqual(after['multiplicity_placeholder'], 1)
        for key in ('approximate_p', 'hoeffding_sensitivity_p', 'approximate_p_holm',
                    'hoeffding_sensitivity_p_holm', 'bonferroni_approximate_interval'):
            self.assertIsNone(after[key])
        self.assertEqual((self.assignments, self.labels), (original_assignments, original_labels))

    def test_other_tests_keep_eleven_slots_and_original_intervals(self):
        raw = self.calculate(release=True)
        result = self.calculate(release=True, ineligible_factors={'F03': 'stimulus isolation defect'})
        self.assertEqual(len(result), 11)
        for key in ('approximate_p', 'hoeffding_sensitivity_p'):
            expected = holm({row['factor']: 1 if row['factor'] == 'F03' else row[key] for row in raw})
            for before, after in zip(raw, result):
                self.assertEqual(after['family_size'], 11)
                if after['factor'] == 'F03':
                    continue
                self.assertEqual(after[key + '_holm'], expected[after['factor']])
                self.assertGreaterEqual(after[key + '_holm'], before[key + '_holm'])
                self.assertEqual(after['bonferroni_approximate_interval'], before['bonferroni_approximate_interval'])

    def test_unavailable_outcomes_remain_unknown_and_assigned(self):
        rid = self.assignments[2]['members']['task']['treatment'][0]
        del self.labels[rid]
        result = self.calculate(release=True, ineligible_factors={'F03': 'quality hold'})[2]
        self.assertEqual(result['assigned']['treatment'], 100)
        self.assertEqual(result['observed']['treatment'], 99)
        self.assertEqual(result['missing_assigned_sample_effect_bounds'], [.99, 1.0])
        self.assertIsNone(result['equal_task_effect'])

    def test_interim_results_stay_unreleased(self):
        for row in self.calculate(release=False, ineligible_factors={'F03': 'quality hold'}):
            self.assertFalse(row['inference_released'])
            self.assertIsNone(row['approximate_p'])
            self.assertIsNone(row['approximate_p_holm'])

    def test_ineligibility_does_not_authorize_shrinking_family(self):
        self.assignments.pop(2)
        with self.assertRaisesRegex(ValueError, 'family_membership'):
            self.calculate(release=True, ineligible_factors={'F03': 'quality hold'})

    def test_unknown_or_undocumented_exclusion_rejected(self):
        for value in ({'F99': 'defect'}, {'F03': ''}, {'F03': None}, ['F03']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_ineligible'):
                self.calculate(release=True, ineligible_factors=value)


if __name__ == '__main__':
    unittest.main()
