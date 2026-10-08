import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_registry as r


def original():
    return {'schema': 'causes.output_root_registry.v1',
            'fixed_roots': {'a': ['runs/original/a', 'runs/recovery/a'],
                            'b': ['runs/original/b']},
            'corpus_roots': ['runs/corpus/b'],
            'sync_roots': ['pilots/first', 'runs/original/a', 'runs/original/b',
                           'runs/recovery/a', 'runs/corpus/b']}


class RegistryTests(unittest.TestCase):
    def test_additive_registry_preserves_previous_and_separates_corpus(self):
        previous = original()
        result = r.extend_registry(previous, {'a': ['runs/scratch/a']}, ['runs/scratch_corpus/b'])
        self.assertEqual(previous, original())
        self.assertEqual(result['sync_roots'][:len(previous['sync_roots'])], previous['sync_roots'])
        self.assertEqual(result['fixed_roots']['a'], ['runs/original/a', 'runs/recovery/a', 'runs/scratch/a'])
        self.assertNotIn('runs/scratch_corpus/b', sum(result['fixed_roots'].values(), []))

    def test_actual_rows_join_across_roots_and_ignore_disjoint_corpus(self):
        registry = r.extend_registry(original(), {'a': ['runs/scratch/a']}, ['runs/scratch_corpus/b'])
        rows = [{'run': 'runs/original/a/a0', 'summary_sha256': 'first'},
                {'run': 'runs/scratch/a/a1', 'summary_sha256': 'second'},
                {'run': 'runs/scratch_corpus/b/extra', 'summary_sha256': 'third'}]
        joined = r.fixed_rows(rows, {'a': ['a0', 'a1'], 'b': ['b0']}, registry)
        self.assertEqual(set(joined['a']), {'a0', 'a1'})
        self.assertEqual(joined['b'], {})
        joined['a']['a0']['summary_sha256'] = 'mutated'
        self.assertEqual(rows[0]['summary_sha256'], 'first')

    def test_original_ID_in_unknown_corpus_or_other_model_root_is_rejected(self):
        for path in ('runs/unknown/a/a0', 'runs/corpus/b/a0', 'runs/original/b/a0'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'unregistered_root'):
                r.fixed_rows([{'run': path}], {'a': ['a0'], 'b': ['b0']}, original())

    def test_cross_root_duplicates_and_identical_duplicates_are_rejected(self):
        first = {'run': 'runs/original/a/a0', 'summary_sha256': 'same'}
        for second in (copy.deepcopy(first), {'run': 'runs/recovery/a/a0', 'summary_sha256': 'same'}):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'multiple_custody'):
                r.fixed_rows([first, second], {'a': ['a0'], 'b': []}, original())

    def test_reader_discovery_keeps_frozen_ID_and_stage_owner(self):
        registry = r.extend_registry(original(), {'a': ['runs/scratch/a']}, [])
        selection = {'a0': {'model': 'a', 'job': {'allocation': {'run_id': 'a0', 'reader_stages': ['early', 'end']}}}}
        rows = [{'run': 'runs/scratch/a/a0'}, {'run': 'runs/corpus/b/extra'}]
        self.assertEqual(r.reader_candidates(rows, selection, registry), rows[:1])
        bad = copy.deepcopy(selection)
        bad['a0']['job']['allocation']['run_id'] = 'different'
        with self.assertRaisesRegex(ValueError, 'selection_ID'):
            r.reader_candidates(rows, bad, registry)

    def test_registry_path_and_model_changes_fail_closed(self):
        mutations = [lambda x: x['fixed_roots']['a'].append('runs/original/b'),
                     lambda x: x['sync_roots'].remove('runs/original/a'),
                     lambda x: x['corpus_roots'].append('runs/original/a'),
                     lambda x: x['sync_roots'].append('runs/../outside'),
                     lambda x: x['sync_roots'].append('/absolute')]
        for mutation in mutations:
            bad = original()
            mutation(bad)
            with self.subTest(value=bad), self.assertRaises(ValueError):
                r.validate_registry(bad)
        with self.assertRaises(ValueError):
            r.extend_registry(original(), {'unfrozen': ['runs/scratch/unfrozen']}, [])
        with self.assertRaises(ValueError):
            r.extend_registry(original(), {'a': ['runs/original/a']}, [])

    def test_frozen_ID_scope_is_exact(self):
        for ids in ({'a': ['a0']}, {'a': ['same'], 'b': ['same']},
                    {'a': ['a0', 'a0'], 'b': []}, {'a': ['../unsafe'], 'b': []}):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                r.fixed_rows([], ids, original())

    def test_registry_file_pin_duplicate_keys_and_alias_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'registry.json'
            raw = json.dumps(original()).encode()
            path.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            self.assertEqual(r.checked_registry(path, sha), original())
            alias = root / 'alias.json'
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'noncanonical'):
                r.checked_registry(alias, sha)
            path.write_bytes(raw + b' ')
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                r.checked_registry(path, sha)
            raw = raw[:-1] + b',"schema":"causes.output_root_registry.v1"}'
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'duplicate_registry_key'):
                r.checked_registry(path, hashlib.sha256(raw).hexdigest())


if __name__ == '__main__':
    unittest.main()
