"""Independent refusal and caller-isolation checks for allocation projections."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from causes import campaign_admission as admission
from causes import campaign_corpus_succession as succession
from causes import campaign_handoff as handoff
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_native_release_operator import ReleaseFixture


class AllocationProjectionReviewTests(unittest.TestCase):
    def test_uncached_alias_reader_and_full_allocator_interface_remain_available(self):
        fixture = ReleaseFixture(self)
        value = fixture.allocation()
        original = succession.native_allocation(value)
        original_jobs = value['jobs']
        value['jobs'] = dict(original_jobs, path=str(fixture.root/'remote/jobs.json'))
        reads = []

        def reader(reference):
            reads.append(copy.deepcopy(reference))
            return handoff.checked(original_jobs if reference == value['jobs'] else reference)

        with patch.object(CorpusContinuation, 'from_control',
                          wraps=CorpusContinuation.from_control) as reconstruct:
            full = succession.native_allocation(value, reader)
            components = succession.native_allocation_components(value, reader)
            self.assertTrue(succession.validate_native_allocation(value, reader))
            self.assertEqual(reconstruct.call_count, 3)
        self.assertIsInstance(full[3], CorpusContinuation)
        self.assertEqual(full[:3], original[:3])
        self.assertEqual(vars(full[3]), vars(original[3]))
        self.assertEqual(components, (*original[:3], original[3].binding))
        self.assertEqual(reads.count(value['jobs']), 3)
        self.assertFalse(Path(value['jobs']['path']).exists())
        components[3]['family'] = 'caller-change'
        self.assertEqual(full[3].binding, original[3].binding)

    def test_reader_cache_and_dependency_scope_refuse_before_reconstruction(self):
        value = ReleaseFixture(self).allocation()
        for call in (succession.native_allocation_components,
                     succession.validate_native_allocation):
            with self.subTest(call=call.__name__):
                reader = Mock(side_effect=handoff.checked)
                with patch.object(CorpusContinuation, 'from_control',
                                  side_effect=AssertionError('unexpected reconstruction')):
                    with self.assertRaisesRegex(ValueError, 'cache_reader_changed'):
                        call(value, reader, cache=succession.NativeAllocationCache())
                    with self.assertRaisesRegex(ValueError, 'cache_scope_changed'):
                        call(value, reader, cache=object())
                    with self.assertRaisesRegex(ValueError, 'dependency_cache_required'):
                        call(value, reader, dependency_sink=lambda ref: None)
                reader.assert_not_called()

    def test_cold_postread_drift_permanently_refuses_every_result_interface(self):
        for call in (succession.native_allocation_components,
                     succession.validate_native_allocation):
            for change in ('allocation', 'dependency'):
                with self.subTest(call=call.__name__, change=change):
                    value = ReleaseFixture(self).allocation()
                    cache = succession.NativeAllocationCache()
                    path = Path(handoff.checked(value['allocation_control'])['catalog']['path'])
                    before = path.read_bytes()
                    original_block = CorpusContinuation.block

                    def drift(allocator, index):
                        result = original_block(allocator, index)
                        if change == 'allocation':
                            value['workers'] += 1
                        else:
                            path.write_bytes(bytes([before[0] ^ 1]) + before[1:])
                        return result

                    try:
                        with patch.object(CorpusContinuation, 'block', autospec=True,
                                          side_effect=drift):
                            with self.assertRaises(ValueError):
                                call(value, cache=cache)
                    finally:
                        if change == 'allocation':
                            value['workers'] -= 1
                        else:
                            path.write_bytes(before)
                    for interface in (succession.native_allocation,
                                      succession.native_allocation_components,
                                      succession.validate_native_allocation):
                        with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                            interface(value, cache=cache)

    def test_validation_only_hit_rejects_same_byte_parent_symlink_permanently(self):
        value = ReleaseFixture(self).allocation()
        cache = succession.NativeAllocationCache()
        expected = succession.native_allocation_components(value, cache=cache)
        directory = Path(value['jobs']['path']).parent
        saved = directory.with_name(directory.name + '-saved')
        directory.rename(saved)
        directory.symlink_to(saved, target_is_directory=True)
        try:
            with patch.object(CorpusContinuation, 'from_control',
                              side_effect=AssertionError('unexpected reconstruction')):
                with self.assertRaises(ValueError):
                    succession.validate_native_allocation(value, cache=cache)
        finally:
            directory.unlink()
            saved.rename(directory)
        for interface in (succession.native_allocation,
                          succession.native_allocation_components,
                          succession.validate_native_allocation):
            with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                interface(value, cache=cache)
        self.assertEqual(succession.native_allocation_components(value), expected)

    def test_mixed_interfaces_export_identical_isolated_dependency_inventory(self):
        value = ReleaseFixture(self).allocation()
        control = handoff.checked(value['allocation_control'])
        expected = {r['path']: r for r in
                    [value['allocation_control'], value['jobs'], value['extensions']]
                    + [control[k] for k in ('prototypes', 'config', 'catalog',
                                           'original_jobs', 'reservation')]}
        cache = succession.NativeAllocationCache()
        for call in (succession.validate_native_allocation,
                     succession.native_allocation_components, succession.native_allocation):
            refs = []
            call(value, cache=cache, dependency_sink=refs.append)
            self.assertEqual({r['path']: r for r in refs}, expected)
            self.assertEqual(len(refs), len(expected))
            for ref in refs:
                ref['path'] = '/caller-mutated-path'
                ref['sha256'] = '0'*64
        self.assertTrue(succession.validate_native_allocation(value, cache=cache))

    def test_continuous_runtime_binding_mutation_cannot_poison_later_calls(self):
        for prefix in (False, True):
            with self.subTest(prefix=prefix):
                fixture = ReleaseFixture(self)
                value, template, common, expected = fixture.runtime(False, prefix)
                cache = succession.NativeAllocationCache()
                with patch.object(CorpusContinuation, 'from_control',
                                  wraps=CorpusContinuation.from_control) as reconstruct:
                    for _ in range(2):
                        result = admission.native_runtime(value, template, common,
                                                          allocation_cache=cache)
                        self.assertEqual(result, expected)
                        result['continuation']['binding']['family'] = 'caller-changed'
                        result['expected_queue']['corpus_continuation']['binding'].setdefault('full_selection', {})['per_task'] = 999
                    self.assertEqual(admission.native_runtime(value, template, common,
                                                             allocation_cache=cache), expected)
                    self.assertEqual(reconstruct.call_count, 1)


if __name__ == '__main__':
    unittest.main()
