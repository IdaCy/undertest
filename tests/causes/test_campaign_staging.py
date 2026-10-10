import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes.campaign_staging import stage


def file_row(path, raw):
    return {'destination': str(path), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode()}


def verify_row(path, raw):
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def source_row(destination, source, raw):
    return {'destination': str(destination), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'source': verify_row(source, raw)}


class StagingTests(unittest.TestCase):
    def test_create_verify_and_repeat_preserve_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / 'control'
            control.write_bytes(b'owned evidence')
            destination = root / 'code' / 'module.py'
            packet = {'files': [file_row(destination, b'print(1)\n')],
                      'verify_only': [verify_row(control, b'owned evidence')]}
            first = stage(packet, root, [control])
            identity = destination.stat()
            second = stage(packet, root, [control])
            self.assertFalse(first['files'][0]['existing_identical'])
            self.assertTrue(second['files'][0]['existing_identical'])
            for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                self.assertEqual(getattr(destination.stat(), field), getattr(identity, field))
            self.assertEqual(control.read_bytes(), b'owned evidence')
            self.assertFalse(second['model_or_service_actions'])

    def test_every_input_is_checked_before_any_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = root / 'new'
            existing = root / 'existing'
            existing.write_bytes(b'original')
            packets = [
                {'files': [file_row(new, b'new'), file_row(existing, b'replaced')], 'verify_only': []},
                {'files': [file_row(new, b'new')], 'verify_only': [verify_row(existing, b'changed')]},
            ]
            for packet in packets:
                with self.assertRaises(ValueError):
                    stage(packet, root)
                self.assertFalse(new.exists())
                self.assertEqual(existing.read_bytes(), b'original')

    def test_protected_duplicate_symlink_and_outside_paths_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / 'controls'
            real = root / 'real'
            real.mkdir()
            (root / 'alias').symlink_to(real, target_is_directory=True)
            cases = [
                [file_row(protected / 'barrier', b'x')],
                [file_row(root / 'same', b'x'), file_row(root / 'same', b'x')],
                [file_row(root / 'alias' / 'new', b'x')],
                [file_row(root.parent / 'outside', b'x')],
            ]
            for rows in cases:
                with self.assertRaises(ValueError):
                    stage({'files': rows, 'verify_only': []}, root, [protected])
            self.assertFalse(protected.exists())
            self.assertEqual(list(real.iterdir()), [])

    def test_corrupted_payload_and_byte_limit_refuse_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'new'
            row = file_row(destination, b'original')
            row['base64'] = base64.b64encode(b'corrupt!').decode()
            with self.assertRaisesRegex(ValueError, 'packet_pin_changed'):
                stage({'files': [row], 'verify_only': []}, root)
            with self.assertRaisesRegex(ValueError, 'byte_limit_exceeded'):
                stage({'files': [file_row(destination, b'123')], 'verify_only': []}, root, max_bytes=2)
            self.assertFalse(destination.exists())

    def test_ancestor_file_conflicts_refuse_before_any_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = root / 'new'
            blocked = root / 'blocked'
            blocked.write_bytes(b'original')
            cases = [
                [file_row(new, b'x'), file_row(new / 'child', b'y')],
                [file_row(new / 'child', b'y'), file_row(new, b'x')],
                [file_row(new, b'x'), file_row(blocked / 'child', b'y')],
            ]
            for rows in cases:
                with self.assertRaisesRegex(ValueError, 'overlapping_file_paths|existing_parent_is_not_directory'):
                    stage({'files': rows, 'verify_only': []}, root)
                self.assertFalse(new.exists())
                self.assertEqual(blocked.read_bytes(), b'original')

    def test_pinned_source_creates_alias_and_preserves_both_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'retained', root / 'copy' / 'evidence'
            source.write_bytes(b'exact evidence')
            source_stat = source.stat()
            packet = {'files': [source_row(destination, source, b'exact evidence')], 'verify_only': []}
            first = stage(packet, root, [source], max_files=1, max_bytes=14)
            destination_stat = destination.stat()
            second = stage(packet, root, [source], max_files=1, max_bytes=14)
            self.assertEqual(destination.read_bytes(), b'exact evidence')
            self.assertEqual(source.read_bytes(), b'exact evidence')
            self.assertFalse(first['files'][0]['existing_identical'])
            self.assertTrue(second['files'][0]['existing_identical'])
            self.assertFalse(second['model_or_service_actions'])
            for path, before in [(source, source_stat), (destination, destination_stat)]:
                for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                    self.assertEqual(getattr(path.stat(), field), getattr(before, field))

    def test_source_missing_changed_or_mismatched_refuses_before_any_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination, first = root / 'retained', root / 'copy', root / 'first'
            source.write_bytes(b'original')
            changed = source_row(destination, source, b'changed!')
            malformed = source_row(destination, source, b'original')
            malformed['source']['bytes'] += 1
            mixed = source_row(destination, source, b'original')
            mixed['base64'] = base64.b64encode(b'original').decode()
            cases = [source_row(destination, root / 'missing', b'original'), changed, malformed, mixed]
            for row in cases:
                with self.subTest(row=row), self.assertRaises((ValueError, FileNotFoundError)):
                    stage({'files': [file_row(first, b'first'), row], 'verify_only': []}, root)
                self.assertFalse(first.exists())
                self.assertFalse(destination.exists())
                self.assertEqual(source.read_bytes(), b'original')

    def test_source_confinement_type_and_protected_destination_remain_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'base'
            root.mkdir()
            source = root / 'retained'
            source.write_bytes(b'original')
            outside = parent / 'outside'
            outside.write_bytes(b'original')
            alias = root / 'symlink'
            alias.symlink_to(source)
            destination = root / 'new'
            cases = [source_row(destination, path, b'original') for path in (outside, alias, root)]
            for row in cases:
                with self.subTest(row=row), self.assertRaises(ValueError):
                    stage({'files': [row], 'verify_only': []}, root)
                self.assertFalse(destination.exists())
            with self.assertRaisesRegex(ValueError, 'protected_path_must_be_verify_only'):
                stage({'files': [source_row(destination, source, b'original')], 'verify_only': []}, root, [destination])
            self.assertFalse(destination.exists())

    def test_source_replacement_during_read_refuses_before_any_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, replacement, destination = root / 'source', root / 'replacement', root / 'new'
            source.write_bytes(b'original')
            replacement.write_bytes(b'original')
            original_fstat = os.fstat
            calls = []

            def replace_after_read(fd):
                calls.append(fd)
                if len(calls) == 2:
                    replacement.replace(source)
                return original_fstat(fd)

            packet = {'files': [source_row(destination, source, b'original')], 'verify_only': []}
            with patch('causes.campaign_staging.os.fstat', side_effect=replace_after_read):
                with self.assertRaisesRegex(ValueError, 'file_changed_during_read'):
                    stage(packet, root)
            self.assertFalse(destination.exists())

    def test_source_reuse_keeps_inventory_bounds_and_existing_destination_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination, first = root / 'source', root / 'existing', root / 'first'
            source.write_bytes(b'original')
            destination.write_bytes(b'different')
            packet = {'files': [file_row(first, b'first'), source_row(destination, source, b'original')], 'verify_only': []}
            for kwargs, reason in [({'max_files': 1}, 'invalid_file_inventory'),
                                   ({'max_bytes': 12}, 'byte_limit_exceeded'),
                                   ({}, 'existing_file_size_or_type')]:
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, reason):
                    stage(packet, root, **kwargs)
                self.assertFalse(first.exists())
                self.assertEqual(destination.read_bytes(), b'different')


if __name__ == '__main__':
    unittest.main()
