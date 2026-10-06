import collections
import copy
import unittest
from causes.corpus_allocation import FAMILY, SeedLedger, admissible_cells, chosen_alias, digest, key, ordered_cells, run_id


def cell(task, number, held=False):
    return {'task_id': task, 'stimulus_sha256': digest([task, number]),
            'hold_ids': ['h'] if held else [], 'currently_admissible': not held,
            'source_aliases': [{'source_id': 'old:' + task, 'variant_id': 'F01L01', 'alias_of': None}],
            'earliest_accepted_canonical_source': 'old:' + task,
            'earliest_accepted_canonical_variant_id': 'F01L01'}


class SchemeTests(unittest.TestCase):
    def test_held_ledger_retained_but_not_admitted(self):
        rows = [cell('a', 1), cell('a', 2, True)]
        before = copy.deepcopy(rows)
        self.assertEqual(admissible_cells({'physical_cells': rows}), rows[:1])
        self.assertEqual(rows, before)
        with self.assertRaisesRegex(ValueError, 'held_cell'):
            ordered_cells(rows, 'mi27')

    def test_balance_with_unequal_task_counts(self):
        rows = [cell(task, number) for task, count in [('a', 3), ('b', 5), ('c', 4)] for number in range(count)]
        ordered = ordered_cells(rows, 'mi27')
        self.assertEqual({key(row['cell']) for row in ordered}, {key(row) for row in rows})
        self.assertEqual(len(ordered), len(rows))
        counts = collections.Counter()
        limits = collections.Counter(row['task_id'] for row in rows)
        for row in ordered:
            counts[row['cell']['task_id']] += 1
            unfinished = [counts[task] for task in limits if counts[task] < limits[task]]
            self.assertLessEqual(max(unfinished, default=0) - min(unfinished, default=0), 1)
        self.assertEqual(ordered, ordered_cells(list(reversed(rows)), 'mi27'))

    def test_models_have_separate_identity_and_order(self):
        rows = [cell(task, number) for task in ('a', 'b', 'c') for number in range(5)]
        self.assertNotEqual(ordered_cells(rows, 'mi27'), ordered_cells(rows, 'tex9'))
        self.assertEqual(len({run_id(row, model) for model in ('mi27', 'tex9') for row in rows}), 30)

    def test_slug_truncation_does_not_merge_identity(self):
        a, b = cell('x' * 100 + 'a', 0), cell('x' * 100 + 'b', 0)
        self.assertNotEqual(run_id(a, 'mi27'), run_id(b, 'mi27'))
        self.assertLessEqual(len(run_id(a, 'mi27')), 180)

    def test_cross_version_reuse_keeps_canonical_and_all_aliases(self):
        value = cell('bbc', 0)
        value['source_aliases'].append({'source_id': 'correction:bbc', 'variant_id': 'F01L01', 'alias_of': None})
        before = copy.deepcopy(value)
        self.assertEqual(chosen_alias(value)['source_id'], 'old:bbc')
        self.assertEqual(value, before)

    def test_seed_collision_skip_reproducible_and_memoized(self):
        candidate = int(digest([FAMILY, 'episode', 0])[:16], 16) % (2**31 - 1)
        left, right = SeedLedger({candidate}), SeedLedger({candidate})
        seed = left.fresh('episode')
        self.assertNotEqual(seed, candidate)
        self.assertEqual(seed, left.fresh('episode'))
        self.assertEqual(seed, right.fresh('episode'))
        self.assertEqual(next(iter(left.records.values()))['collision_counter'], 1)
        self.assertNotEqual(seed, left.fresh('another_episode'))

    def test_duplicate_or_contradictory_cell_rejected(self):
        value = cell('a', 0)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            admissible_cells({'physical_cells': [value, value]})
        value['hold_ids'] = ['h']
        with self.assertRaisesRegex(ValueError, 'hold_admissibility'):
            admissible_cells({'physical_cells': [value]})


if __name__ == '__main__':
    unittest.main()
