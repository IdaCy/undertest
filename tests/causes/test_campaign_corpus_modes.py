import copy
import hashlib
import json
import unittest
from unittest import mock

from causes.campaign_corpus_modes import partition_prefixes


class CorpusModesTests(unittest.TestCase):
    def setUp(self):
        self.registry = {'schema': 'causes.output_root_registry.v1',
                         'fixed_roots': {'a': ['runs/old/a', 'runs/new/a'],
                                         'b': ['runs/old/b']},
                         'corpus_roots': ['runs/extra/a'],
                         'sync_roots': ['runs/old/a', 'runs/old/b',
                                        'runs/extra/a', 'runs/new/a']}
        self.models = {root: root.rsplit('/', 1)[1] for root in self.registry['sync_roots']}
        self.modes = {'a': {'full_done': True, 'prefix_done': False,
                            'full_admin': True, 'prefix_admin': False,
                            'full_pending': True, 'prefix_pending': False},
                      'b': {'other_full': True}}
        self.admin = {'a': ['full_admin', 'prefix_admin'], 'b': []}
        self.excluded = ['pilots/control']

    @staticmethod
    def prefix(label, rows):
        return {'label': label, 'raw': b''.join(json.dumps(row).encode() + b'\n'
                                               for row in rows)}

    def partition(self, paths=(), **changes):
        prefixes = [self.prefix('one', [{'run': path} for path in paths])]
        return partition_prefixes(changes.get('prefixes', prefixes), self.registry,
                                  changes.get('models', self.models),
                                  changes.get('modes', self.modes),
                                  changes.get('admin', self.admin), self.excluded)

    def test_full_prefix_and_three_disjoint_states(self):
        result = self.partition(['runs/new/a/full_done', 'runs/old/a/prefix_done',
                                 'runs/old/b/other_full', 'runs/old/b/extra_b',
                                 'runs/extra/a/extra_a', 'pilots/control/control'])
        self.assertEqual(result['models']['a']['finalized_assignment_modes'],
                         {'finalized_records': 3, 'selected_full': 1,
                          'selected_prefix': 1, 'mode_unknown_extra_corpus': 1})
        states = result['models']['a']['frozen_fixed_assignment_states']
        for mode, stem in [('selected_full', 'full'), ('selected_prefix', 'prefix')]:
            groups = states[mode]
            self.assertEqual({key: value['count'] for key, value in groups.items()},
                             {'assigned': 3, 'generated_custody_final': 1,
                              'administratively_unavailable': 1, 'unresolved': 1})
            for group, suffix in [('generated_custody_final', 'done'),
                                  ('administratively_unavailable', 'admin'),
                                  ('unresolved', 'pending')]:
                expected = hashlib.sha256(('[' + json.dumps(stem + '_' + suffix)
                                           + ']').encode()).hexdigest()
                self.assertEqual(groups[group]['id_set_sha256'], expected)
        self.assertEqual(result['by_root']['runs/old/b']['mode_unknown_extra_corpus'], 1)
        self.assertEqual(result['total_finalized_records'], 5)
        self.assertEqual(result['excluded_by_root'], {'pilots/control': 1})

    def test_row_mode_outcomes_and_payload_paths_never_classify_assignments(self):
        rows = [{'run': 'runs/new/a/full_done', 'continue_to_end': False,
                 'end_status': 'technical_failure', 'path': '/not/a/payload'},
                {'run': 'runs/old/a/prefix_done', 'continue_to_end': True,
                 'end_status': 'natural_completion'},
                {'run': 'runs/old/a/extra_full_looking', 'continue_to_end': True},
                {'run': 'runs/extra/a/extra_prefix_looking', 'continue_to_end': False}]
        with mock.patch('builtins.open', side_effect=AssertionError('filesystem access')), \
                mock.patch('pathlib.Path.open', side_effect=AssertionError('path access')):
            result = self.partition(prefixes=[self.prefix('opaque', rows)])
        self.assertEqual(result['models']['a']['finalized_assignment_modes'],
                         {'finalized_records': 4, 'selected_full': 1,
                          'selected_prefix': 1, 'mode_unknown_extra_corpus': 2})
        self.assertFalse(result['success_filtered'])
        self.assertFalse(result['semantic_status_inspected'])
        self.assertFalse(result['payloads_read'])

    def test_empty_rosters_and_custody_keep_zero_partitions(self):
        result = self.partition(modes={'a': {}, 'b': {}}, admin={'a': [], 'b': []})
        empty_digest = hashlib.sha256(b'[]').hexdigest()
        self.assertEqual(result['total_finalized_records'], 0)
        self.assertEqual(set(result['by_root']), set(self.models))
        for values in result['by_root'].values():
            self.assertEqual(values['run_id_set_sha256'], empty_digest)
        for model in result['models'].values():
            for mode in model['frozen_fixed_assignment_states'].values():
                self.assertTrue(all(value == {'count': 0, 'id_set_sha256': empty_digest}
                                    for value in mode.values()))

    def test_strict_boolean_modes(self):
        for invalid in [0, 1, None, 'false', [], {}]:
            modes = copy.deepcopy(self.modes)
            modes['a']['full_pending'] = invalid
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'strict_boolean'):
                self.partition(modes=modes)

    def test_admin_ids_must_be_unique_and_in_the_matching_roster(self):
        for ids, reason in [(['full_admin', 'full_admin'], 'duplicate_administrative'),
                            (['other_full'], 'not_in_model_roster'),
                            (['extra'], 'not_in_model_roster'),
                            ([1], 'ID_string'), ('full_admin', 'ID_list')]:
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, reason):
                self.partition(admin={'a': ids, 'b': []})
        with self.assertRaisesRegex(ValueError, 'exact_administrative_models'):
            self.partition(admin={'a': []})

    def test_generated_admin_overlap_refused_for_both_modes(self):
        for rid in ['full_admin', 'prefix_admin']:
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'generated_administrative'):
                self.partition(['runs/new/a/' + rid])

    def test_counter_refusals_remain_in_force(self):
        cases = [(['runs/new/a/full_done', 'runs/old/a/full_done'], 'collision'),
                 (['runs/old/b/full_done'], 'original_ID_model_changed'),
                 (['runs/extra/a/full_done'], 'original_ID_in_unregistered_root'),
                 (['pilots/control/full_done'], 'original_ID_in_excluded_root'),
                 (['runs/unknown/a/extra'], 'unknown_custody_root')]
        for paths, reason in cases:
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, reason):
                self.partition(paths)
        with self.assertRaisesRegex(ValueError, 'complete_custody_prefix'):
            self.partition(prefixes=[{'label': 'torn', 'raw': b'{"run":"runs/old/a/x"}'}])
        modes = copy.deepcopy(self.modes)
        modes['b']['full_done'] = False
        with self.assertRaisesRegex(ValueError, 'frozen_ID_shared_between_models'):
            self.partition(modes=modes)

    def test_order_independent_partitions_and_immutable_inputs(self):
        rows = [{'run': 'runs/new/a/full_done'}, {'run': 'runs/old/a/prefix_done'},
                {'run': 'runs/old/a/extra'}]
        prefixes = [self.prefix('one', rows[:1]), self.prefix('two', rows[1:])]
        before = copy.deepcopy((prefixes, self.registry, self.models, self.modes, self.admin))
        result = self.partition(prefixes=prefixes)
        reordered = self.partition(prefixes=[self.prefix('other', list(reversed(rows)))],
                                   modes={m: dict(reversed(list(v.items())))
                                          for m, v in self.modes.items()},
                                   admin={m: list(reversed(v)) for m, v in self.admin.items()})
        self.assertEqual(result['models'], reordered['models'])
        self.assertEqual(result['by_root'], reordered['by_root'])
        self.assertEqual((prefixes, self.registry, self.models, self.modes, self.admin), before)
        self.assertNotEqual(result['inputs'], reordered['inputs'])


if __name__ == '__main__':
    unittest.main()
