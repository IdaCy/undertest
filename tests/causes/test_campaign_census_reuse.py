import copy
import unittest
from unittest.mock import patch

from causes import campaign_census as census
from tests.causes import test_campaign_census as fixtures


class CompositionReuseTests(unittest.TestCase):
    setUp = fixtures.CensusTests.setUp
    write = fixtures.CensusTests.write
    captures = fixtures.CensusTests.captures
    freeze = fixtures.CensusTests.freeze

    def test_repeated_ids_validate_seeds_again(self):
        for bad in (True, 1.0, '1', -1, census.SEED_MODULUS):
            first = fixtures.allocation('same')
            second = fixtures.allocation('same', bad)
            frozen = self.freeze([first, second])
            with self.subTest(seed=bad), self.assertRaisesRegex(ValueError, 'invalid_typed_allocation_seed'):
                census.compose_reservation(self.config, self.captures(), frozen)

    def test_identity_objects_are_fresh_and_input_mutation_is_seen(self):
        value = fixtures.allocation('same')
        one = census._identity(value, {})
        known = {'same': one}
        two = census._identity(value, known)
        self.assertEqual(one, two)
        self.assertIsNot(one, two)
        value['seed'] = 11
        self.assertEqual(one['seed'], 1)
        self.assertEqual(census._identity(value, known)['seed'], 11)
        two['seed'] = True
        self.assertEqual(census._identity(value, known)['seed'], 11)

    def test_normalized_equality_and_conflict_keep_typed_semantics(self):
        minimal = dict(run_id='same')
        explicit = dict(run_id='same', seed=None, block_seed=None,
                        task_seed=None, model_seed=None, unrelated='ignored')
        frozen = self.freeze([minimal, explicit])
        result = census.compose_reservation(self.config, self.captures(), frozen)
        self.assertEqual(result['allocation_identities'], [census.identity(minimal)])
        explicit['seed'] = 0
        with self.assertRaisesRegex(ValueError, 'allocation_identity_conflict'):
            census.compose_reservation(self.config, self.captures(), frozen)

    def test_path_validation_reuse_is_local_to_one_composition(self):
        frozen = self.freeze([fixtures.allocation('same')] * 40)
        captures = self.captures()
        original = census.relative
        seen = []
        def checked(value):
            if value == 'same':
                seen.append(value)
            return original(value)
        with patch.object(census, 'relative', side_effect=checked):
            first = census.compose_reservation(self.config, captures, frozen)
            second = census.compose_reservation(self.config, captures, frozen)
        self.assertEqual(seen, ['same', 'same'])
        self.assertEqual(census.encoded(first), census.encoded(second))

    def test_later_noncanonical_id_and_wrong_root_still_refuse(self):
        for bad in ('same/', '../same', './same', 'same/child', 'same\x00'):
            frozen = self.freeze([fixtures.allocation('same'), fixtures.allocation(bad)])
            with self.subTest(run_id=bad), self.assertRaises(ValueError):
                census.compose_reservation(self.config, self.captures(), frozen)
        self.config['frozen_inputs'] = []
        self.write(0, 'runs/finite/model/same/allocation.json', fixtures.allocation('same'))
        captures = self.captures()
        altered = copy.deepcopy(captures)
        altered['one']['value']['files'][0]['path'] = str(self.base/'one/runs/other/model/same/allocation.json')
        with self.assertRaisesRegex(ValueError, 'actual_allocation_path_scope'):
            census.compose_reservation(self.config, altered, [])

    def test_unissued_block_and_duplicate_order_keep_exact_result(self):
        self.write(0, 'runs/finite/model/issued/allocation.json', fixtures.allocation('issued'))
        self.write(0, 'runs/finite/model/.dispatcher/blocks/0001.json',
                   dict(jobs=[dict(allocation=fixtures.allocation('issued')),
                              dict(allocation=fixtures.allocation('future', 99))]))
        frozen = self.freeze([fixtures.allocation('issued'), fixtures.allocation('future', 99)])
        captures = self.captures()
        one = census.compose_reservation(self.config, captures, frozen)
        frozen[0]['value'].reverse()
        two = census.compose_reservation(self.config, captures, frozen)
        self.assertEqual(census.encoded(one), census.encoded(two))
        self.assertEqual(one['run_ids'], ['future', 'issued'])
        self.assertEqual(one['allocation_seeds'], [1, 2, 3, 99])
        self.assertEqual(one['persisted_full_blocks'][0]['allocation_count'], 2)


if __name__ == '__main__':
    unittest.main()

