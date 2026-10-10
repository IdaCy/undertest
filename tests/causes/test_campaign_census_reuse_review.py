import copy
import unittest

from causes import campaign_census as census
from tests.causes import test_campaign_census as fixtures


class CompositionReuseReviewTests(unittest.TestCase):
    setUp = fixtures.CensusTests.setUp
    write = fixtures.CensusTests.write
    captures = fixtures.CensusTests.captures
    freeze = fixtures.CensusTests.freeze

    def test_every_repeated_seed_field_keeps_strict_type_and_range(self):
        class Integer(int):
            pass
        for field in census.FIELDS[1:]:
            for value in (True, 1.0, '1', -1, census.SEED_MODULUS, Integer(1)):
                with self.subTest(field=field, value=value):
                    first = fixtures.allocation('known')
                    second = dict(first); second[field] = value
                    frozen = self.freeze([first, second])
                    with self.assertRaisesRegex(ValueError, 'invalid_typed_allocation_seed'):
                        census.compose_reservation(self.config, self.captures(), frozen)

    def test_repeated_run_id_still_rejects_string_subclass(self):
        class Text(str):
            pass
        frozen = self.freeze([fixtures.allocation('known'), fixtures.allocation(Text('known'))])
        with self.assertRaisesRegex(ValueError, 'invalid_allocation_run_id'):
            census.compose_reservation(self.config, self.captures(), frozen)
        with self.assertRaisesRegex(ValueError, 'invalid_allocation_run_id'):
            census.identity(fixtures.allocation(Text('known')))

    def test_frozen_known_id_does_not_bypass_capture_host_root_or_basename(self):
        self.write(0, 'runs/finite/model/known/allocation.json', fixtures.allocation('known'))
        frozen = self.freeze([fixtures.allocation('known')], [['one', 'runs/finite/model']])
        captured = self.captures()
        mutations = [lambda f: f.update(path=f['path'].replace('/one/', '/two/')),
                     lambda f: f.update(path=f['path'].replace('/known/', '/another/')),
                     lambda f: f.update(root='runs/unknown/model')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                bad = copy.deepcopy(captured); mutate(bad['one']['value']['files'][0])
                with self.assertRaises(ValueError): census.compose_reservation(self.config, bad, frozen)

    def test_precomputed_block_path_does_not_accept_neighbor_or_cross_host(self):
        self.write(0, 'runs/finite/model/.dispatcher/blocks/0001.json',
                   {'jobs': [{'allocation': fixtures.allocation('known')}]})
        frozen = self.freeze([fixtures.allocation('known')])
        captured = self.captures()
        for replacement in ('/two/', '/one/runs/finite/model/.dispatcher/blocks/../'):
            bad = copy.deepcopy(captured)
            bad['one']['value']['files'][0]['path'] = bad['one']['value']['files'][0]['path'].replace('/one/', replacement)
            with self.subTest(path=replacement), self.assertRaises(ValueError):
                census.compose_reservation(self.config, bad, frozen)
        bad = copy.deepcopy(captured)
        bad['one']['value']['files'][0]['path'] = bad['one']['value']['files'][0]['path'].replace('/blocks/', '/blocks-next/')
        with self.assertRaisesRegex(ValueError, 'persisted_block_path_scope'):
            census.compose_reservation(self.config, bad, frozen)

    def test_normalized_nulls_unicode_and_max_seed_keep_complete_bytes(self):
        run_id = 'café_λ'
        first = dict(run_id=run_id, seed=0, model_seed=census.SEED_MODULUS-1)
        second = dict(model_seed=census.SEED_MODULUS-1, task_seed=None, seed=0,
                      block_seed=None, run_id=run_id, ignored={'not': 'identity'})
        frozen = self.freeze([first, second])
        captures = self.captures()
        result = census.compose_reservation(self.config, captures, frozen)
        self.assertEqual(result['allocation_identities'], [census.identity(first)])
        self.assertEqual(result['allocation_seeds'], [census.SEED_MODULUS-1])
        frozen[0]['value'][:] = [second, first]
        self.assertEqual(census.encoded(result), census.encoded(census.compose_reservation(self.config, captures, frozen)))

    def test_result_mutation_and_failed_composition_do_not_poison_next_call(self):
        frozen = self.freeze([fixtures.allocation('known')])
        captures = self.captures()
        first = census.compose_reservation(self.config, captures, frozen)
        baseline = census.encoded(first)
        first['allocation_identities'][0]['seed'] = 99
        self.assertEqual(census.encoded(census.compose_reservation(self.config, captures, frozen)), baseline)
        frozen[0]['value'].append(fixtures.allocation('known', 77))
        self.config['frozen_inputs'][0]['expected_count'] += 1
        with self.assertRaisesRegex(ValueError, 'allocation_identity_conflict'):
            census.compose_reservation(self.config, captures, frozen)
        frozen[0]['value'].pop(); self.config['frozen_inputs'][0]['expected_count'] -= 1
        self.assertEqual(census.encoded(census.compose_reservation(self.config, captures, frozen)), baseline)

    def test_host_inventory_revalidated_after_prior_success(self):
        frozen = self.freeze([fixtures.allocation('known')]); captures = self.captures()
        census.compose_reservation(self.config, captures, frozen)
        altered = copy.deepcopy(self.config)
        altered['hosts'][0]['roots'].append(altered['hosts'][0]['roots'][0])
        with self.assertRaisesRegex(ValueError, 'distinct_inventory_required'):
            census.compose_reservation(altered, captures, frozen)
        altered = copy.deepcopy(self.config); altered['hosts'][0]['base'] += '/..'
        with self.assertRaisesRegex(ValueError, 'noncanonical_base'):
            census.compose_reservation(altered, captures, frozen)


if __name__ == '__main__':
    unittest.main()
