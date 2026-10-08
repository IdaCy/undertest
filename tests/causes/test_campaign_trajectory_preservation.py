import copy
import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/campaign_trajectory_preservation.py'
spec = importlib.util.spec_from_file_location('trajectory_preservation', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PriorPreservationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'run_id': 'prior', 'model': 'test_model', 'task_id': 'fixture',
                    'allocation': {'reader_stages': ['early', 'end'], 'seed': 1},
                    'context_sources': {'early': {'context_sha256': 'unchanged'}},
                    'self': {'early': {'fixture': {'label': 'yes'}}},
                    'reader': {'early': {'path': '/original/early/RESULT.json',
                        'result_sha256': 'earlier', 'instruments': {'presence': {'labels': {'fixture': 'yes'}}}}},
                    'reader_dispositions': {'early': 'final_verified_reader_result', 'end': 'pending_reader'}}
        self.old = {'prior': self.row}
        self.actual = copy.deepcopy(self.old)
        self.actual['prior']['reader']['early']['path'] = '/copy/early/RESULT.json'
        self.aliases = {'/copy/early/RESULT.json': {'path': '/original/early/RESULT.json', 'sha256': 'earlier'},
                        '/copy/end/RESULT.json': {'path': '/original/end/RESULT.json', 'sha256': 'later'}}

    def compare(self):
        return module.preserve_prior(self.old, self.actual, self.aliases)

    def resolve_end(self):
        self.actual['prior']['reader']['end'] = {'path': '/copy/end/RESULT.json', 'result_sha256': 'later',
                                               'instruments': {'presence': {'labels': {'fixture': 'no'}}}}
        self.actual['prior']['reader_dispositions']['end'] = 'final_verified_reader_result'

    def test_exact_alias_and_new_identity_preserve_prior_inputs(self):
        before = copy.deepcopy((self.old, self.actual, self.aliases))
        self.actual['new'] = {'run_id': 'new'}
        result = self.compare()
        self.assertTrue(result['all_prior_fields_preserved'])
        self.assertEqual(result['new_ids'], ['new'])
        self.assertEqual(self.old, before[0])
        self.assertEqual(self.actual['prior'], before[1]['prior'])
        self.assertEqual(self.aliases, before[2])

    def test_changed_prior_reader_measurement_refuses(self):
        self.actual['prior']['reader']['early']['instruments']['presence']['labels']['fixture'] = 'no'
        self.assertFalse(self.compare()['all_prior_fields_preserved'])

    def test_actor_context_type_or_added_null_field_refuses(self):
        self.actual['prior']['allocation']['seed'] = True
        self.assertFalse(self.compare()['all_prior_fields_preserved'])
        self.actual['prior']['allocation']['seed'] = 1
        self.actual['prior']['new_field'] = None
        self.assertFalse(self.compare()['all_prior_fields_preserved'])

    def test_pending_resolution_is_explicit_monotone_addition(self):
        self.resolve_end()
        result = self.compare()
        self.assertTrue(result['all_prior_fields_preserved'])
        self.assertEqual(result['monotone_reader_additions'], [{'run_id': 'prior', 'stage': 'end',
                         'before': 'pending_reader', 'after': 'final_verified_reader_result'}])

    def test_terminal_missingness_cannot_be_rewritten(self):
        self.row['reader_dispositions']['end'] = 'final_reader_technical_missing'
        self.resolve_end()
        self.assertFalse(self.compare()['all_prior_fields_preserved'])

    def test_unallocated_reader_stage_and_unproved_alias_refuse(self):
        self.actual['prior']['reader']['early']['path'] = '/unlisted/result'
        self.assertFalse(self.compare()['all_prior_fields_preserved'])
        self.actual['prior']['reader']['early']['path'] = '/copy/early/RESULT.json'
        self.actual['prior']['reader']['unknown'] = copy.deepcopy(self.actual['prior']['reader']['early'])
        self.actual['prior']['reader_dispositions']['unknown'] = 'final_verified_reader_result'
        self.assertFalse(self.compare()['all_prior_fields_preserved'])

    def test_missing_prior_identity_refuses(self):
        self.actual.clear()
        self.assertFalse(self.compare()['all_prior_fields_preserved'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
