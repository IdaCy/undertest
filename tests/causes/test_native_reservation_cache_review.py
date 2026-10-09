"""Independent adverse cases for immutable reservation proof reuse."""

import copy
import datetime as dt
from pathlib import Path
import unittest
from unittest import mock

from causes import campaign_admission as a
from causes import campaign_census as c
from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from tests.causes import test_native_reservation_growth as fixtures


class NativeReservationCacheReviewTests(unittest.TestCase):
    setUp = fixtures.NativeReservationGrowthTests.setUp
    save = fixtures.NativeReservationGrowthTests.save
    block = fixtures.NativeReservationGrowthTests.block
    captures = fixtures.NativeReservationGrowthTests.captures
    compose = fixtures.NativeReservationGrowthTests.compose
    build = fixtures.NativeReservationGrowthTests.build
    refresh = fixtures.NativeReservationGrowthTests.refresh
    operator = fixtures.NativeReservationGrowthTests.operator

    def values(self):
        return (self.old, self.current, self.allocation, self.growth, self.config_ref)

    def check(self, cache, **kwargs):
        return a.native_reservation_foreign_blocks(*self.values(), proof_cache=cache, **kwargs)

    @staticmethod
    def changed(raw):
        return bytes([raw[0] ^ 1]) + raw[1:]

    def test_every_nested_and_capture_dependency_is_checked_on_warm_proof(self):
        self.build()
        control = h.checked(self.allocation['allocation_control'])
        references = [self.allocation[k] for k in ('allocation_control', 'jobs', 'extensions')]
        references += [control[k] for k in ('config', 'prototypes', 'catalog', 'original_jobs', 'reservation')]
        references += [self.growth['baseline_census_config'], self.config_ref]
        for value in (self.old, self.current):
            references += [value['capture_map'], *value['capture_pins']]
        self.assertEqual(len({ref['path'] for ref in references}), len(references))
        allocation_cache = s.NativeAllocationCache()
        s.native_allocation(self.allocation, cache=allocation_cache)
        for ref in references:
            with self.subTest(path=Path(ref['path']).name):
                cache = a.NativeReservationProofCache()
                self.assertIs(self.check(cache, allocation_cache=allocation_cache), True)
                path = Path(ref['path']); original = path.read_bytes()
                path.write_bytes(self.changed(original))
                try:
                    with self.assertRaises(ValueError):
                        self.check(cache, allocation_cache=allocation_cache)
                finally:
                    path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
                    self.check(cache, allocation_cache=allocation_cache)

    def test_loaded_census_alias_bytes_are_part_of_proof_dependencies(self):
        self.build()
        alias = self.root/'loaded-census.py'
        original = Path(c.__file__).read_bytes(); alias.write_bytes(original)
        self.assertNotEqual(str(alias), self.old['composition_source']['path'])
        with mock.patch.object(c, '__file__', str(alias)):
            cache = a.NativeReservationProofCache()
            self.assertIs(self.check(cache), True)
            alias.write_bytes(self.changed(original))
            with self.assertRaises(ValueError):
                self.check(cache)
            alias.write_bytes(original)
            with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
                self.check(cache)

    def test_loaded_source_canonical_failure_permanently_invalidates_warm_cache(self):
        self.build()
        alias = self.root/'canonical-census.py'
        alias.write_bytes(Path(c.__file__).read_bytes())
        with mock.patch.object(c, '__file__', str(alias)):
            cache = a.NativeReservationProofCache()
            self.assertIs(self.check(cache), True)
            backup = alias.with_suffix('.original'); alias.rename(backup)
            alias.symlink_to(backup)
            try:
                with self.assertRaises(ValueError):
                    self.check(cache)
            finally:
                alias.unlink(); backup.rename(alias)
            with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
                self.check(cache)

    def test_same_capture_bytes_through_symlink_cannot_reuse_proof(self):
        self.build()
        cache = a.NativeReservationProofCache(); self.check(cache)
        path = Path(self.current['capture_pins'][0]['path'])
        backup = path.with_suffix('.original'); path.rename(backup); path.symlink_to(backup)
        try:
            with self.assertRaises(ValueError):
                self.check(cache)
        finally:
            path.unlink(); backup.rename(path)
        with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
            self.check(cache)

    def test_first_proof_rechecks_transitive_allocation_pin_after_projection(self):
        self.build()
        catalog = Path(h.checked(self.allocation['allocation_control'])['catalog']['path'])
        original = catalog.read_bytes(); compose = c.compose_reservation
        cache = a.NativeReservationProofCache(); calls = 0

        def mutate_after_projection(*args, **kwargs):
            nonlocal calls
            value = compose(*args, **kwargs); calls += 1
            if calls == 2:
                catalog.write_bytes(self.changed(original))
            return value

        try:
            with mock.patch.object(c, 'compose_reservation', side_effect=mutate_after_projection):
                with self.assertRaises(ValueError):
                    self.check(cache)
        finally:
            catalog.write_bytes(original)
        self.assertEqual(calls, 2)
        with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
            self.check(cache)
        self.assertIs(self.check(a.NativeReservationProofCache()), True)

    def test_argument_mutation_during_first_proof_cannot_publish_cache(self):
        self.build()
        original = copy.deepcopy(self.growth); compose = c.compose_reservation
        cache = a.NativeReservationProofCache(); calls = 0

        def mutate_after_projection(*args, **kwargs):
            nonlocal calls
            value = compose(*args, **kwargs); calls += 1
            if calls == 2:
                self.growth['foreign_roots'].append(dict(host='other', root='runs/sealed/model'))
            return value

        with mock.patch.object(c, 'compose_reservation', side_effect=mutate_after_projection):
            with self.assertRaisesRegex(ValueError, 'scope_changed'):
                self.check(cache)
        self.growth = original
        with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
            self.check(cache)

    def test_all_full_input_scopes_and_waiting_mode_are_bound(self):
        self.build()
        for index in range(6):
            with self.subTest(scope=index):
                cache = a.NativeReservationProofCache(); self.check(cache)
                values = copy.deepcopy(self.values()); waiting = False
                if index == 0:
                    values[0]['run_ids'].reverse()
                elif index == 1:
                    values[1]['allocation_seeds'].append(123456789)
                elif index == 2:
                    values[2]['workers'] += 1
                elif index == 3:
                    values[3]['foreign_roots'][0]['root'] += '-changed'
                elif index == 4:
                    values[4]['path'] += '.different'
                else:
                    waiting = True
                with self.assertRaisesRegex(ValueError, 'scope_changed'):
                    cache.verify(*values, waiting=waiting)
                with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
                    self.check(cache)

    def test_reader_substitution_is_rejected_cold_and_invalidates_warm_cache(self):
        self.build()
        for field, reader in (('reader', lambda ref: h.checked(ref)),
                              ('bytes_reader', lambda ref: h.checked_bytes(ref))):
            with self.subTest(reader=field):
                cache = a.NativeReservationProofCache()
                with self.assertRaisesRegex(ValueError, 'reader_changed'):
                    cache.verify(*self.values(), **{field: reader})
                self.assertIs(self.check(cache), True)
                with self.assertRaisesRegex(ValueError, 'reader_changed'):
                    cache.verify(*self.values(), **{field: reader})
                with self.assertRaisesRegex(ValueError, 'reservation_cache_invalid'):
                    self.check(cache)

    def test_cold_and_warm_stale_calls_still_execute_real_freshness_predicate(self):
        self.build()
        actual_datetime = dt.datetime
        future = actual_datetime.now(dt.timezone.utc) + dt.timedelta(seconds=300)

        class FutureClock(actual_datetime):
            @classmethod
            def now(cls, tz=None):
                return future if tz is not None else future.replace(tzinfo=None)

        cache = a.NativeReservationProofCache()
        with mock.patch.object(c, 'compose_reservation', wraps=c.compose_reservation) as compose:
            with mock.patch.object(a.dt, 'datetime', FutureClock):
                for unused in range(2):
                    with self.assertRaisesRegex(ValueError, '^admission_observation_stale$'):
                        self.check(cache)
            self.assertEqual(compose.call_count, 2)
            self.assertIs(self.check(cache), True)
            self.assertEqual(compose.call_count, 2)

    def test_failed_semantic_proof_stays_cold_and_can_only_publish_valid_input(self):
        self.build()
        valid = copy.deepcopy(self.current); cache = a.NativeReservationProofCache()
        self.current['run_ids'].pop()
        with mock.patch.object(c, 'compose_reservation', wraps=c.compose_reservation) as compose:
            with self.assertRaises(ValueError):
                self.check(cache)
            self.current = valid
            self.assertIs(self.check(cache), True)
            self.assertEqual(compose.call_count, 4)
            self.assertIs(self.check(cache), True)
            self.assertEqual(compose.call_count, 4)

    def test_dependency_exports_are_detached_from_allocation_cache_storage(self):
        self.build()
        cache = s.NativeAllocationCache(); first = []
        s.native_allocation(self.allocation, cache=cache, dependency_sink=first.append)
        self.assertEqual(len(first), 8)
        pristine = copy.deepcopy(first)
        first[0]['path'] = '/mutated-export'
        first[1]['sha256'] = '0'*64
        second = []
        s.native_allocation(self.allocation, cache=cache, dependency_sink=second.append)
        self.assertEqual(second, pristine)
        before = copy.deepcopy(self.values())
        self.assertIs(self.check(a.NativeReservationProofCache(), allocation_cache=cache), True)
        self.assertEqual(self.values(), before)

    def test_warm_proof_does_not_cache_foundation_acceptance_or_clear_flags(self):
        self.build()
        operator, foundation = self.operator(self.old, self.current, self.allocation, self.config_ref)
        operator.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',
                               reservation_growth=self.growth, census_hosts=['fixture', 'other'])
        pair = operator.inputs['fresh_reservation']
        peer = h.checked(pair['review'])
        peer.update(accepted_dual_host_capture_custody=True, accepted_native_foreign_block_extension=True,
                    generation_reservation=operator.inputs['precreation_reservation']['evidence'],
                    reservation_growth=self.growth, capture_pins=self.current['capture_pins'])
        pair['review'] = self.save(peer)
        self.assertEqual(operator.evidence(operator.start_action), foundation)
        changed = dict(foundation, donor_histories_clear=False)
        ref = self.save(changed)
        old_pair = operator.inputs['foundation']; review = h.checked(old_pair['review'])
        review['evidence'] = ref
        operator.inputs['foundation'] = dict(evidence=ref, review=self.save(review))
        with self.assertRaisesRegex(ValueError, 'native_foundation_not_clear:donor_histories_clear'):
            operator.evidence(operator.start_action)
