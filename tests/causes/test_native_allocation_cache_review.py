"""Independent adverse cases for native allocation validation reuse."""

import copy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes import test_native_corpus_succession as fixtures


class NativeAllocationCacheReviewTests(unittest.TestCase):
    setUp = fixtures.NativeAllocationTests.setUp
    save = fixtures.NativeAllocationTests.save
    allocation = fixtures.NativeAllocationTests.allocation

    @staticmethod
    def changed_bytes(raw):
        # Preserve length so size-only reuse cannot pass this challenge.
        return bytes([raw[0] ^ 1]) + raw[1:]

    def test_each_transitive_dependency_drift_is_rejected_and_cache_stays_invalid(self):
        for name in ('allocation_control', 'jobs', 'extensions', 'config', 'prototypes',
                     'catalog', 'original_jobs', 'reservation'):
            with self.subTest(dependency=name):
                value = self.allocation()
                control = h.checked(value['allocation_control'])
                ref = value[name] if name in ('allocation_control', 'jobs', 'extensions') else control[name]
                cache = s.NativeAllocationCache()
                s.native_allocation(value, cache=cache)
                path = Path(ref['path']); original = path.read_bytes()
                path.write_bytes(self.changed_bytes(original))
                try:
                    with self.assertRaises(ValueError):
                        s.native_allocation(value, cache=cache)
                finally:
                    path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                    s.native_allocation(value, cache=cache)
                # A cache refusal must not change the unchanged uncached API.
                self.assertEqual(len(s.native_allocation(value)[0]), 5)

    def test_same_bytes_at_a_symlink_are_not_the_original_canonical_dependency(self):
        for use_parent in (False, True):
            with self.subTest(parent_symlink=use_parent):
                value = self.allocation()
                cache = s.NativeAllocationCache()
                s.native_allocation(value, cache=cache)
                path = Path(value['jobs']['path'])
                moved = path.parent if use_parent else path
                backup = moved.with_name(moved.name + '.original')
                moved.rename(backup)
                moved.symlink_to(backup, target_is_directory=use_parent)
                try:
                    with self.assertRaises(ValueError):
                        s.native_allocation(value, cache=cache)
                finally:
                    moved.unlink()
                    backup.rename(moved)
                with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                    s.native_allocation(value, cache=cache)

    def test_failed_first_validation_never_supplies_a_reusable_result(self):
        value = self.allocation()
        invalid = copy.deepcopy(value)
        invalid['selection']['run_ids'].reverse()
        cache = s.NativeAllocationCache()
        original_block = CorpusContinuation.block
        with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                               side_effect=original_block) as block:
            with self.assertRaisesRegex(ValueError, 'selection_changed'):
                s.native_allocation(invalid, cache=cache)
            restored = s.native_allocation(value, cache=cache)
            self.assertEqual(len(restored[0]), 5)
            self.assertEqual(block.call_count, 2)
            s.native_allocation(value, cache=cache)
            self.assertEqual(block.call_count, 2)

    def test_first_validation_rechecks_a_dependency_changed_after_it_was_read(self):
        value = self.allocation()
        catalog = Path(h.checked(value['allocation_control'])['catalog']['path'])
        original = catalog.read_bytes()
        cache = s.NativeAllocationCache()
        original_block = CorpusContinuation.block

        def change_after_reconstruction(allocator, index):
            block = original_block(allocator, index)
            catalog.write_bytes(self.changed_bytes(original))
            return block

        try:
            with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                                   side_effect=change_after_reconstruction):
                with self.assertRaises(ValueError):
                    s.native_allocation(value, cache=cache)
        finally:
            catalog.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'cache_invalid'):
            s.native_allocation(value, cache=cache)
        with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                               side_effect=original_block) as block:
            self.assertEqual(len(s.native_allocation(value, cache=s.NativeAllocationCache())[0]), 5)
            self.assertEqual(block.call_count, 1)

    def test_allocation_object_change_during_first_validation_cannot_be_cached(self):
        value = self.allocation()
        cache = s.NativeAllocationCache()
        original_block = CorpusContinuation.block

        def change_value(allocator, index):
            block = original_block(allocator, index)
            value['workers'] += 1
            return block

        with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                               side_effect=change_value):
            with self.assertRaises(ValueError):
                s.native_allocation(value, cache=cache)
        value['workers'] -= 1
        with self.assertRaisesRegex(ValueError, 'cache_invalid'):
            s.native_allocation(value, cache=cache)
        with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                               side_effect=original_block) as block:
            s.native_allocation(value, cache=s.NativeAllocationCache())
            self.assertEqual(block.call_count, 1)

    def test_conflicting_pins_for_one_path_refuse_before_publishing_cache(self):
        value = self.allocation()
        extension = value['extensions']
        value['extensions'] = dict(path=value['jobs']['path'], bytes=2,
                                   sha256=hashlib.sha256(b'[]').hexdigest())
        cache = s.NativeAllocationCache()
        with self.assertRaisesRegex(ValueError, 'conflicting_pin'):
            s.native_allocation(value, cache=cache)
        value['extensions'] = extension
        self.assertEqual(len(s.native_allocation(value, cache=cache)[0]), 5)

    def test_returned_nested_data_and_mutable_allocator_do_not_poison_later_calls(self):
        value = self.allocation()
        cache = s.NativeAllocationCache()
        expected = s.native_allocation(value)
        first = s.native_allocation(value, cache=cache)
        first[0][0]['allocation']['seed'] += 1
        first[1]['model'] = 'mutated'
        first[2].append({'mutated': True})
        first[3].index = 999
        first[3].used_seeds.clear()
        first[3]._pending['jobs'][0]['allocation']['run_id'] = 'mutated'
        second = s.native_allocation(value, cache=cache)
        self.assertEqual(second[:3], expected[:3])
        self.assertEqual(vars(second[3]), vars(expected[3]))
        second[0].clear()
        second[3].cells.clear()
        third = s.native_allocation(value, cache=cache)
        self.assertEqual(third[:3], expected[:3])
        self.assertEqual(vars(third[3]), vars(expected[3]))

    def test_custom_reader_is_never_invoked_to_seed_a_cache(self):
        value = self.allocation()
        reader = mock.Mock(side_effect=h.checked)
        with self.assertRaisesRegex(ValueError, 'reader_changed'):
            s.native_allocation(value, reader=reader, cache=s.NativeAllocationCache())
        reader.assert_not_called()

    def test_reader_and_allocation_scope_changes_cannot_refresh_an_existing_cache(self):
        for change_reader in (False, True):
            with self.subTest(reader=change_reader):
                value = self.allocation()
                cache = s.NativeAllocationCache()
                s.native_allocation(value, cache=cache)
                if change_reader:
                    with self.assertRaises(ValueError):
                        s.native_allocation(value, reader=lambda ref: h.checked(ref), cache=cache)
                else:
                    changed = copy.deepcopy(value)
                    changed['selection']['run_ids'].reverse()
                    with self.assertRaises(ValueError):
                        s.native_allocation(changed, cache=cache)
                with self.assertRaisesRegex(ValueError, 'cache_invalid'):
                    s.native_allocation(value, cache=cache)

    def test_independent_cache_instances_each_validate_their_own_first_call(self):
        value = self.allocation()
        original_block = CorpusContinuation.block
        with mock.patch.object(CorpusContinuation, 'block', autospec=True,
                               side_effect=original_block) as block:
            for cache in (s.NativeAllocationCache(), s.NativeAllocationCache()):
                s.native_allocation(value, cache=cache)
                s.native_allocation(value, cache=cache)
            self.assertEqual(block.call_count, 2)


if __name__ == '__main__':
    unittest.main()
