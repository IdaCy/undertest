import copy
import json
import unittest

from causes.campaign_corpus_count import count_prefixes


class CorpusCountTests(unittest.TestCase):
    def setUp(self):
        self.registry = {'schema': 'causes.output_root_registry.v1',
                         'fixed_roots': {'a': ['runs/old/a', 'runs/new/a'],
                                         'b': ['runs/old/b']},
                         'corpus_roots': ['runs/extra/a'],
                         'sync_roots': ['runs/old/a', 'runs/old/b', 'runs/extra/a', 'runs/new/a']}
        self.models = {root: root.rsplit('/', 1)[1] for root in self.registry['sync_roots']}
        self.frozen = {'a': ['fixed_a'], 'b': ['fixed_b']}
        self.excluded = ['pilots/control']

    def count(self, paths, **kwargs):
        raw = b''.join(json.dumps({'run': path, 'path': '/must/not/read/payload'}).encode() + b'\n'
                       for path in paths)
        prefixes = kwargs.pop('prefixes', [{'label': 'one', 'raw': raw}])
        return count_prefixes(prefixes, kwargs.pop('registry', self.registry),
                              kwargs.pop('models', self.models),
                              kwargs.pop('frozen', self.frozen), self.excluded)

    def test_mixed_root_and_registered_scratch(self):
        result = self.count(['runs/new/a/fixed_a', 'runs/old/b/fixed_b',
                             'runs/old/b/extra_b', 'runs/extra/a/extra_a',
                             'pilots/control/pilot'])
        self.assertEqual(result['counts']['a'], {'total': 2, 'fixed_generated': 1, 'extra_corpus': 1})
        self.assertEqual(result['counts']['b'], {'total': 2, 'fixed_generated': 1, 'extra_corpus': 1})
        self.assertEqual(result['excluded_rows'], 1)
        self.assertTrue(result['lower_bound'])
        self.assertFalse(result['payloads_read'])

    def test_collision_across_indices(self):
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.count([], prefixes=[{'label': 'one', 'raw': b'{"run":"runs/old/a/same"}\n'},
                                     {'label': 'two', 'raw': b'{"run":"runs/extra/a/same"}\n'}])

    def test_duplicate_same_run(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_custody_run'):
            self.count(['runs/old/a/same', 'runs/old/a/same'])

    def test_unknown_root(self):
        with self.assertRaisesRegex(ValueError, 'unknown_custody_root'):
            self.count(['runs/unknown/a/rid'])

    def test_torn_tail_refused(self):
        with self.assertRaisesRegex(ValueError, 'complete_custody_prefix'):
            self.count([], prefixes=[{'label': 'one', 'raw': b'{"run":"runs/old/a/x"}\n{"r'}])

    def test_original_wrong_model_or_corpus_or_excluded(self):
        for path in ['runs/old/b/fixed_a', 'runs/extra/a/fixed_a', 'pilots/control/fixed_a']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.count([path])

    def test_exact_mapping_and_registry(self):
        models = dict(self.models)
        models['runs/unregistered/a'] = 'a'
        with self.assertRaisesRegex(ValueError, 'exact_production_root_mapping'):
            self.count([], models=models)
        models = dict(self.models, **{'runs/new/a': 'b'})
        with self.assertRaisesRegex(ValueError, 'fixed_root_model_changed'):
            self.count([], models=models)

    def test_duplicate_json_key_and_noncanonical_path(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_custody_object_key'):
            self.count([], prefixes=[{'label': 'one', 'raw': b'{"run":"x","run":"y"}\n'}])
        with self.assertRaisesRegex(ValueError, 'noncanonical_registry_path'):
            self.count(['runs/old/a/../x'])

    def test_empty_complete_prefix_retains_zero_roots(self):
        result = self.count([])
        self.assertEqual(result['total_production_finalized'], 0)
        self.assertEqual(set(result['by_root']), set(self.models))


if __name__ == '__main__':
    unittest.main()
