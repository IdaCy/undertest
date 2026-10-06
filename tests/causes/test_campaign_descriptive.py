import unittest

from causes.campaign_descriptive import distribution, measurement, paired


def value(label, status='observed', identity=None):
    return {'label': label, 'status': status, 'identity': identity}


class DescriptiveTests(unittest.TestCase):
    def test_missing_measurements_preserve_assigned_denominator(self):
        result = distribution([value('yes'), value('no'), value(None, 'final_technical_missing')])
        self.assertEqual(result['assigned'], 3)
        self.assertEqual(result['observed'], 2)
        self.assertEqual(result['observed_yes_fraction_descriptive'], .5)
        self.assertEqual(result['assigned_yes_fraction_bounds'], [1 / 3, 2 / 3])

    def test_paired_missingness_and_shared_measurement_bounds(self):
        shared = value(None, 'final_technical_missing', ('run', 'presence'))
        left = [shared, value('no', identity='left')]
        right = [shared.copy(), value(None, 'pending_reader', 'right')]
        result = paired(left, right)
        self.assertEqual(result['assigned_pairs'], 2)
        self.assertEqual(result['complete_pairs'], 0)
        self.assertEqual(result['same_physical_measurement_pairs'], 1)
        self.assertEqual(result['right_minus_left_assigned_mean_bounds'], [0, .5])
        with self.assertRaisesRegex(ValueError, 'shared_measurement_changed'):
            paired([shared], [value('yes', identity=shared['identity'])])

    def test_thresholds_are_not_forced_to_nest(self):
        result = paired([value('no', identity='presence')], [value('yes', identity='strong')])
        self.assertEqual(result['right_minus_left_observed_pair_mean_descriptive'], 1)
        self.assertEqual(result['transitions'], [{'left': 'no', 'right': 'yes', 'count': 1}])

    def test_assignment_capture_and_quality_missingness_remain_distinct(self):
        jobs = {'run': {'allocation': {'instruments': ['evaluation_presence'],
                                      'reader_stages': ['early'], 'reader_thresholds': ['presence']}}}
        pending = measurement({}, jobs, 'run', 'early', 'self')
        self.assertEqual(pending['status'], 'pending_actor_final_record')
        quality = measurement({}, jobs, 'run', 'early', 'self', quality_unavailable={'run'})
        self.assertEqual(quality['status'], 'final_quality_unavailable')
        records = {'run': {'context_sources': {}}}
        self.assertEqual(measurement(records, jobs, 'run', 'early', 'self')['status'],
                         'final_actor_capture_unavailable')
        with self.assertRaisesRegex(RuntimeError, 'quality_unissued'):
            measurement(records, jobs, 'run', 'early', 'self', quality_unavailable={'run'})
        with self.assertRaisesRegex(ValueError, 'reader_instrument_not_assigned'):
            measurement(records, jobs, 'run', 'end', 'reader')


if __name__ == '__main__':
    unittest.main()
