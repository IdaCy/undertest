import concurrent.futures
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from causes.campaign_source_integrity import SourceIntegrityError, verify_source_phase


class SourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.a = self.make('a', b'first source')
        self.b = self.make('b', b'second source')

    def make(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def test_union_reads_each_exact_pin_once_in_first_seen_order(self):
        calls = []
        original = Path.read_bytes
        def read(path):
            calls.append(str(path))
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            result = verify_source_phase([self.a, self.a], (), [self.b, self.a, self.b])
        self.assertEqual(calls, [self.a['path'], self.b['path']])
        self.assertEqual(result, dict(unique_references=2,
                                      verified_bytes=self.a['bytes'] + self.b['bytes']))

    def test_no_cross_phase_reuse(self):
        calls = []
        original = Path.read_bytes
        def read(path):
            calls.append(str(path))
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            verify_source_phase([self.a, self.b], [self.a])
            verify_source_phase([self.a, self.b], [self.b])
        self.assertEqual(calls, [self.a['path'], self.b['path']] * 2)

    def test_change_between_entry_and_exit_is_detected(self):
        verify_source_phase([self.a], [self.b])
        Path(self.a['path']).write_bytes(b'other source')  # Same byte length.
        with self.assertRaisesRegex(SourceIntegrityError, 'source_pin_mismatch'):
            verify_source_phase([self.a], [self.b])

    def test_change_during_work_is_caught_by_fresh_exit_phase(self):
        original = Path.read_bytes
        def read(path):
            raw = original(path)
            if str(path) == self.b['path']:
                Path(self.a['path']).write_bytes(b'other source')
            return raw
        with patch.object(Path, 'read_bytes', read):
            verify_source_phase([self.a, self.b])
        with self.assertRaisesRegex(SourceIntegrityError, 'source_pin_mismatch'):
            verify_source_phase([self.a, self.b])

    def test_conflict_fails_before_any_file_reads(self):
        conflict = dict(self.a, sha256='0' * 64)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected read')):
            with self.assertRaisesRegex(SourceIntegrityError, 'conflicting_source_pin'):
                verify_source_phase([self.a, self.b], [conflict])

    def test_bad_additional_roster_never_skips_required_file(self):
        missing = dict(self.b, path=str(self.root / 'absent'))
        with self.assertRaisesRegex(SourceIntegrityError, 'missing_regular_source'):
            verify_source_phase([self.a], [missing])

    def test_each_supplied_dependency_is_verified(self):
        Path(self.b['path']).write_bytes(b'broken')
        with self.assertRaisesRegex(SourceIntegrityError, 'source_pin_mismatch'):
            verify_source_phase([self.a], [self.b])

    def test_empty_complete_union_and_invalid_rosters_reject(self):
        for rosters in [(), ([], ()), ('abc',), ({},), (iter([self.a]),)]:
            with self.subTest(rosters=repr(rosters)):
                with self.assertRaises(SourceIntegrityError):
                    verify_source_phase(*rosters)

    def test_malformed_pins_fail_before_reading(self):
        invalid = [dict(self.a, bytes=True), dict(self.a, bytes=-1),
                   dict(self.a, sha256='f' * 63), dict(self.a, sha256='F' * 64),
                   dict(self.a, extra=True), dict(self.a, path='relative'),
                   dict(self.a, path=self.a['path'] + '\x00')]
        for reference in invalid:
            with self.subTest(reference=reference):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected read')):
                    with self.assertRaises(SourceIntegrityError):
                        verify_source_phase([self.b], [reference])

    def test_symlink_and_parent_symlink_reject(self):
        link = self.root / 'link'
        link.symlink_to(self.a['path'])
        parent = self.root / 'parent_link'
        parent.symlink_to(self.root, target_is_directory=True)
        for path in [link, parent / 'a']:
            with self.assertRaises(SourceIntegrityError):
                verify_source_phase([dict(self.a, path=str(path))])

    def test_same_bytes_at_distinct_paths_are_both_read(self):
        other = self.make('copy', b'first source')
        calls = []
        original = Path.read_bytes
        def read(path):
            calls.append(str(path))
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            verify_source_phase([self.a, other])
        self.assertEqual(calls, [self.a['path'], other['path']])

    def test_threads_have_independent_phases(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        calls = []
        original = Path.read_bytes
        def read(path):
            barrier.wait(timeout=5)
            with lock:
                calls.append((threading.get_ident(), str(path)))
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: verify_source_phase([self.a], [self.a]), range(2)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({thread for thread, _ in calls}), 2)
        self.assertEqual(results[0], results[1])

    def test_input_rosters_and_references_are_not_mutated(self):
        roster = [dict(self.a), dict(self.b)]
        before = [dict(row) for row in roster]
        verify_source_phase(roster, roster)
        self.assertEqual(roster, before)


if __name__ == '__main__':
    unittest.main()
