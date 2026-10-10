"""Independent boundary tests for immutable segmented staging."""

import base64
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_staging as staging


def reference(path, data):
    return {'path': str(path), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}


def literal(data):
    return {'base64': base64.b64encode(data).decode('ascii'), 'bytes': len(data)}


class RangeStagingReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.source = self.base / 'source'
        self.source.write_bytes(b'0123456789')
        self.ref = reference(self.source, b'0123456789')
        self.target = self.base / 'new' / 'target'

    def ranged(self, offset=2, size=3, ref=None):
        return {'source': copy.deepcopy(self.ref if ref is None else ref),
                'offset': offset, 'bytes': size}

    def row(self, data=b'234', segments=None, target=None):
        target = target or self.target
        ref = reference(target, data)
        return {'destination': ref.pop('path'), **ref,
                'segments': [self.ranged()] if segments is None else segments}

    def packet(self, *rows):
        return {'files': list(rows), 'verify_only': []}

    def assert_uncreated(self):
        self.assertFalse(self.target.parent.exists())

    def test_range_total_is_checked_before_source_io(self):
        candidate = self.row(segments=[self.ranged(size=2)])
        with patch.object(staging, '_read_exact', side_effect=AssertionError('unexpected read')):
            with self.assertRaisesRegex(ValueError, 'segment_byte_count_mismatch'):
                staging.stage(self.packet(candidate), self.base)
        self.assert_uncreated()

    def test_invalid_complete_source_pins_never_create_parent(self):
        alterations = ({'bytes': True}, {'bytes': -1}, {'sha256': 'A' * 64},
                       {'sha256': '0' * 63}, {'path': 7}, {'extra': None})
        for alteration in alterations:
            candidate = self.row()
            candidate['segments'][0]['source'].update(alteration)
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                staging.stage(self.packet(candidate), self.base)
            self.assert_uncreated()

    def test_conflicting_full_pin_for_cached_path_refuses_whole_packet(self):
        first = self.ranged(0, 1)
        second = self.ranged(1, 1)
        second['source']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'range_source_changed'):
            staging.stage(self.packet(self.row(b'01', [first, second])), self.base)
        self.assert_uncreated()

    def test_source_and_parent_symlinks_are_not_accepted_aliases(self):
        original = self.base / 'original'
        self.source.rename(original)
        self.source.symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'noncanonical_path|symlink_path'):
            staging.stage(self.packet(self.row()), self.base)
        self.source.unlink()
        original.rename(self.source)
        directory = self.base / 'real'
        directory.mkdir()
        nested = directory / 'source'
        nested.write_bytes(b'0123456789')
        alias = self.base / 'alias'
        alias.symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'noncanonical_path|symlink_path'):
            staging.stage(self.packet(self.row(segments=[self.ranged(ref=reference(alias / 'source', b'0123456789'))])), self.base)
        self.assert_uncreated()

    def test_valid_whole_source_outside_base_is_still_refused(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name).resolve() / 'source'
        source.write_bytes(b'0123456789')
        with self.assertRaisesRegex(ValueError, 'source_outside_base'):
            staging.stage(self.packet(self.row(segments=[self.ranged(ref=reference(source, b'0123456789'))])), self.base)
        self.assert_uncreated()

    def test_same_bytes_inode_replacement_after_first_range_refuses(self):
        decode = base64.b64decode

        def replace(*args, **kwargs):
            replacement = self.base / 'replacement'
            replacement.write_bytes(b'0123456789')
            os.replace(replacement, self.source)
            return decode(*args, **kwargs)

        candidate = self.row(b'0x1', [self.ranged(0, 1), literal(b'x'), self.ranged(1, 1)])
        with patch.object(staging.base64, 'b64decode', side_effect=replace):
            with self.assertRaisesRegex(ValueError, 'range_source_changed'):
                staging.stage(self.packet(candidate), self.base)
        self.assert_uncreated()

    def test_source_parent_symlink_introduced_after_read_refuses_prewrite(self):
        directory = self.base / 'source_directory'
        directory.mkdir()
        self.source.rename(directory / 'source')
        ref = reference(directory / 'source', b'0123456789')
        decode = base64.b64decode

        def replace_parent(*args, **kwargs):
            moved = self.base / 'moved'
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
            return decode(*args, **kwargs)

        candidate = self.row(b'0x', [self.ranged(0, 1, ref), literal(b'x')])
        with patch.object(staging.base64, 'b64decode', side_effect=replace_parent):
            with self.assertRaisesRegex(ValueError, 'noncanonical_path|symlink_path'):
                staging.stage(self.packet(candidate), self.base)
        self.assert_uncreated()

    def test_late_missing_verify_entry_cannot_leave_segmented_payload(self):
        packet = self.packet(self.row())
        packet['verify_only'].append(reference(self.base / 'missing', b''))
        with self.assertRaises(FileNotFoundError):
            staging.stage(packet, self.base)
        self.assert_uncreated()

    def test_missing_or_nonregular_range_source_prevents_all_payloads(self):
        first = self.row(b'x', [literal(b'x')])
        second_target = self.base / 'other'
        self.source.unlink()
        for directory in (False, True):
            if directory:
                self.source.mkdir()
            with self.subTest(directory=directory), self.assertRaises((FileNotFoundError, ValueError)):
                staging.stage(self.packet(first, self.row(target=second_target)), self.base)
            self.assertFalse(second_target.exists())
            self.assert_uncreated()

    def test_protection_and_path_overlap_still_apply_to_segmented_rows(self):
        with self.assertRaisesRegex(ValueError, 'protected_path_must_be_verify_only'):
            staging.stage(self.packet(self.row()), self.base, [self.target.parent])
        parent = self.row(b'0', [self.ranged(0, 1)], self.target.parent)
        with self.assertRaisesRegex(ValueError, 'overlapping_file_paths'):
            staging.stage(self.packet(self.row(), parent), self.base)
        self.assert_uncreated()


if __name__ == '__main__':
    unittest.main()
