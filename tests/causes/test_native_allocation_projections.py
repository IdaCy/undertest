"""Reduced allocation results retain full proof and caller isolation."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_corpus_succession as s, campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_native_release_operator import ReleaseFixture
from tests.causes.test_native_release_lineage import TwoLinkFixture


class AllocationProjectionTests(unittest.TestCase):
    def fixture(self):
        return ReleaseFixture(self)

    def test_cold_and_warm_components_match_full_finite_and_continuous_results(self):
        for finite, prefix in ((True, False), (False, False), (False, True)):
            with self.subTest(finite=finite, prefix=prefix):
                value = self.fixture().allocation(finite, prefix)
                full = s.native_allocation(value)
                expected = (*full[:3], full[3].binding)
                cache = s.NativeAllocationCache()
                with patch.object(CorpusContinuation, 'from_control',
                                  wraps=CorpusContinuation.from_control) as build:
                    self.assertEqual(s.native_allocation_components(value, cache=cache), expected)
                    self.assertEqual(s.native_allocation_components(value, cache=cache), expected)
                    self.assertTrue(s.validate_native_allocation(value, cache=cache))
                    self.assertEqual(build.call_count, 1)
                self.assertEqual(s.native_allocation_components(value), expected)

    def test_nested_projection_mutations_never_escape_into_cached_snapshot(self):
        for finite in (True, False):
            with self.subTest(finite=finite):
                value = self.fixture().allocation(finite)
                cache = s.NativeAllocationCache()
                first = s.native_allocation_components(value, cache=cache)
                expected = copy.deepcopy(first)
                for result in (first, s.native_allocation_components(value, cache=cache)):
                    selected = result[0] if finite else result[2]
                    selected[0]['allocation']['run_id'] = 'mutated-return'
                    result[1]['nested-return'] = {'values': [1, 2]}
                    result[3]['full_selection']['per_task'] = 999
                    self.assertEqual(s.native_allocation_components(value, cache=cache), expected)
                    self.assertTrue(s.validate_native_allocation(value, cache=cache))

    def test_validation_cold_then_full_return_keeps_original_independence(self):
        value = self.fixture().allocation()
        expected = s.native_allocation(value)
        cache = s.NativeAllocationCache()
        self.assertTrue(s.validate_native_allocation(value, cache=cache))
        for _ in range(2):
            result = s.native_allocation(value, cache=cache)
            self.assertEqual(result[:3], expected[:3])
            self.assertEqual(result[3].__dict__, expected[3].__dict__)
            result[0][0]['allocation']['run_id'] = 'mutated-full-return'
            result[3].used_ids.add('mutated-allocator')
            result[3]._pending['jobs'][0]['spec']['mutated'] = True
        self.assertTrue(s.validate_native_allocation(value, cache=cache))

    def test_cold_full_result_cannot_poison_later_reduced_returns(self):
        value = self.fixture().allocation()
        cache = s.NativeAllocationCache()
        full = s.native_allocation(value, cache=cache)
        expected = copy.deepcopy((*full[:3], full[3].binding))
        full[0][0]['allocation']['run_id'] = 'cold-return-mutated'
        full[3].binding['family'] = 'cold-allocator-mutated'
        self.assertEqual(s.native_allocation_components(value, cache=cache), expected)
        self.assertTrue(s.validate_native_allocation(value, cache=cache))

    def test_warm_reduced_calls_do_not_copy_discarded_allocator_state(self):
        value = self.fixture().allocation()
        cache = s.NativeAllocationCache()
        expected = s.native_allocation_components(value, cache=cache)
        def forbidden_copy(*args):
            raise AssertionError('discarded allocator copied')
        with patch.object(CorpusContinuation, '__deepcopy__', forbidden_copy, create=True):
            self.assertEqual(s.native_allocation_components(value, cache=cache), expected)
            self.assertTrue(s.validate_native_allocation(value, cache=cache))
            with self.assertRaisesRegex(AssertionError, 'discarded allocator copied'):
                s.native_allocation(value, cache=cache)

    def test_missing_or_replaced_dependency_permanently_invalidates_all_returns(self):
        for function in (s.native_allocation_components, s.validate_native_allocation):
            for missing in (False, True):
                with self.subTest(function=function.__name__, missing=missing):
                    value = self.fixture().allocation()
                    cache = s.NativeAllocationCache()
                    function(value, cache=cache)
                    path = Path(h.checked(value['allocation_control'])['reservation']['path'])
                    raw = path.read_bytes()
                    if missing:
                        path.unlink()
                    else:
                        replacement = path.with_suffix('.replacement')
                        replacement.write_bytes(raw + b' ')
                        replacement.replace(path)
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        function(value, cache=cache)
                    path.write_bytes(raw)
                    for call in (s.native_allocation, s.native_allocation_components,
                                 s.validate_native_allocation):
                        with self.assertRaisesRegex(ValueError, 'native_allocation_cache_invalid'):
                            call(value, cache=cache)

    def test_changed_reader_or_mutable_scope_invalidates_warm_reduced_calls(self):
        for function in (s.native_allocation_components, s.validate_native_allocation):
            for kind in ('reader', 'scope'):
                with self.subTest(function=function.__name__, kind=kind):
                    value = self.fixture().allocation()
                    cache = s.NativeAllocationCache()
                    function(value, cache=cache)
                    changed = copy.deepcopy(value)
                    reader = h.checked
                    if kind == 'scope':
                        changed['selection']['run_ids'][0] = 'changed-scope'
                    else:
                        reader = lambda ref: h.checked(ref)
                    with self.assertRaisesRegex(ValueError, 'native_allocation_cache_.*changed'):
                        function(changed, reader, cache=cache)
                    with self.assertRaisesRegex(ValueError, 'native_allocation_cache_invalid'):
                        s.native_allocation(value, cache=cache)

    def test_dependency_exports_are_complete_and_isolated_on_cold_and_warm_paths(self):
        value = self.fixture().allocation()
        cache = s.NativeAllocationCache()
        cold, warm = [], []
        s.native_allocation_components(value, cache=cache, dependency_sink=cold.append)
        expected = copy.deepcopy(cold)
        cold[0]['sha256'] = '0' * 64
        s.validate_native_allocation(value, cache=cache, dependency_sink=warm.append)
        self.assertEqual(warm, expected)
        warm[0]['bytes'] += 1
        self.assertTrue(s.validate_native_allocation(value, cache=cache))

    def test_full_operator_warm_passes_no_longer_copy_any_allocator(self):
        operator, _, _, foundation = self.fixture().operator()
        expected = operator.static()
        self.assertEqual(operator.evidence(operator.release_action), foundation)
        lineage_operator, _, _, lineage_foundation = TwoLinkFixture(self).operator()
        self.assertEqual(lineage_operator.evidence(lineage_operator.release_action), lineage_foundation)
        def forbidden_copy(*args):
            raise AssertionError('discarded allocator copied')
        with patch.object(CorpusContinuation, '__deepcopy__', forbidden_copy, create=True):
            self.assertEqual(operator.static(), expected)
            self.assertEqual(operator.evidence(operator.release_action), foundation)
            self.assertEqual(lineage_operator.evidence(lineage_operator.release_action), lineage_foundation)


if __name__ == '__main__':
    unittest.main()
