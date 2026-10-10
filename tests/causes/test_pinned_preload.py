import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes.pinned_preload import PinnedPreload


class PinnedPreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'input.json'
        self.path.write_bytes(b'{"nested":[1,2]}')
        self.ref = self.pin(self.path)

    @staticmethod
    def pin(path):
        raw = path.read_bytes()
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def prepared(self):
        value = PinnedPreload()
        value.prepare(lambda read: json.loads(read(self.ref)))
        return value

    def test_single_use_output_and_reference_ownership(self):
        value = self.prepared()
        exposed = value.references
        exposed[0]['sha256'] = '0' * 64
        result = value.take()
        result['nested'].append(3)
        self.assertEqual(json.loads(self.path.read_bytes()), {'nested': [1, 2]})
        value.finish()
        for operation in (value.take, value.finish, lambda: value.prepare(lambda read: None)):
            with self.assertRaises(ValueError):
                operation()

    def test_changed_content_permanently_invalidates(self):
        value = self.prepared()
        self.path.write_bytes(b'{"nested":[2,1]}')
        with self.assertRaises(ValueError):
            value.take()
        self.path.write_bytes(b'{"nested":[1,2]}')
        with self.assertRaises(ValueError):
            value.take()

    def test_equal_byte_replacement_and_missing_file_refuse(self):
        for mode in ('replace', 'missing'):
            with self.subTest(mode=mode):
                value = self.prepared()
                if mode == 'replace':
                    replacement = self.root / 'replacement'
                    replacement.write_bytes(self.path.read_bytes())
                    replacement.replace(self.path)
                else:
                    self.path.unlink()
                with self.assertRaises((ValueError, FileNotFoundError)):
                    value.take()
                self.path.write_bytes(b'{"nested":[1,2]}')

    def test_parent_symlink_and_noncanonical_reference_refuse(self):
        folder = self.root / 'folder'
        folder.mkdir()
        path = folder / 'input'
        path.write_bytes(b'good')
        ref = self.pin(path)
        value = PinnedPreload()
        value.prepare(lambda read: read(ref))
        other = self.root / 'other'
        folder.rename(other)
        folder.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            value.take()
        with self.assertRaises(ValueError):
            PinnedPreload().prepare(lambda read: read(dict(self.ref, path=str(self.root) + '/./input.json')))

    def test_conflicting_and_malformed_pins_refuse(self):
        def conflict(read):
            read(self.ref)
            return read(dict(self.ref, sha256='0' * 64))
        for build in (conflict, lambda read: read(dict(self.ref, bytes=True)),
                      lambda read: read(dict(self.ref, extra=1))):
            with self.assertRaises(ValueError):
                PinnedPreload().prepare(build)

    def test_builder_failure_and_postread_change_refuse(self):
        value = PinnedPreload()
        def build(read):
            result = read(self.ref)
            self.path.write_bytes(b'{"nested":[2,1]}')
            return result
        with self.assertRaises(ValueError):
            value.prepare(build)
        with self.assertRaises(ValueError):
            value.take()

    def test_builder_cannot_suppress_failed_dependency_read(self):
        value = PinnedPreload()
        def build(read):
            try:
                read(dict(self.ref, sha256='0' * 64))
            except ValueError:
                pass
            return {'accepted': True}
        with self.assertRaises(ValueError):
            value.prepare(build)
        with self.assertRaises(ValueError):
            value.take()

    def test_finish_checks_all_dependencies_again(self):
        guard = self.root / 'source.py'
        guard.write_bytes(b'pass\n')
        value = PinnedPreload()
        value.prepare(lambda read: json.loads(read(self.ref)), guard_references=[self.pin(guard)])
        value.take()
        guard.write_bytes(b'fail\n')
        with self.assertRaises(ValueError):
            value.finish()

    def test_change_during_read_and_reader_outside_build_refuse(self):
        value = PinnedPreload()
        original = os.fstat
        calls = []
        def changed(fd):
            result = original(fd)
            calls.append(fd)
            if len(calls) == 2:
                self.path.write_bytes(b'{"nested":[2,1]}')
                return original(fd)
            return result
        with mock.patch('causes.pinned_preload.os.fstat', side_effect=changed):
            with self.assertRaises(ValueError):
                value.prepare(lambda read: read(self.ref))
        with self.assertRaises(ValueError):
            value._read(self.ref)


if __name__ == '__main__':
    unittest.main()
