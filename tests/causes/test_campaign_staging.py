import base64
import hashlib
from pathlib import Path
import tempfile
import unittest

from causes.campaign_staging import stage


def file_row(path, raw):
    return {'destination': str(path), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode()}


def verify_row(path, raw):
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


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


if __name__ == '__main__':
    unittest.main()
