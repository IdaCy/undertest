import errno
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_supplement as core
from causes import campaign_supplement_budget as budget


class AdmissionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.real_getrlimit = resource.getrlimit
        self.limit = patch.object(budget.resource, 'getrlimit', return_value=(100, 100))
        self.limit.start()
        self.addCleanup(self.limit.stop)

    def make(self, **changes):
        values = dict(maximum_claims=64, maximum_retained_bytes=1000,
                      maximum_file_bytes=100, maximum_written_files=3, reserve_bytes=300)
        values.update(changes)
        return budget.AdmissionBudget(self.root, **values)

    def test_fixed_count_is_independent_of_outcomes(self):
        guard = self.make()
        self.assertEqual(guard.before_admission(63)['claimed'], 63)
        with self.assertRaisesRegex(core.AdmissionHold, 'fixed_admission_tranche_complete'):
            guard.before_admission(64)

    def test_joint_role_allowances_require_room_for_inflight_writes(self):
        guard = self.make(maximum_retained_bytes=400)
        (self.root / 'one').write_bytes(b'x' * 100)
        guard.before_admission(0)
        (self.root / 'two').write_bytes(b'y')
        with self.assertRaisesRegex(core.AdmissionHold, 'retained_evidence_budget_reserved'):
            guard.before_admission(1)
        self.assertEqual((self.root / 'two').read_bytes(), b'y')

    def test_whole_nested_output_counted(self):
        (self.root / '.control').mkdir()
        (self.root / '.control' / 'journal').write_bytes(b'x' * 40)
        (self.root / 'capture').mkdir()
        (self.root / 'capture' / 'response.raw').write_bytes(b'y' * 60)
        self.assertEqual(self.make().before_admission(0)['retained_bytes'], 100)

    def test_unknown_new_file_fanout_refuses_without_deletion(self):
        guard = self.make()
        guard.before_admission(0)
        for i in range(4):
            (self.root / str(i)).write_text('x')
        with self.assertRaisesRegex(core.IntegrityError, 'write_window_exceeded'):
            guard.before_admission(1)
        self.assertEqual(len(list(self.root.iterdir())), 4)

    def test_existing_file_identity_and_prefix_size_preserved(self):
        path = self.root / 'journal'
        path.write_text('old')
        guard = self.make()
        guard.before_admission(0)
        path.write_text('x')
        with self.assertRaisesRegex(core.IntegrityError, 'replaced_or_shrunk'):
            guard.before_admission(1)

    def test_links_and_changed_process_limits_refuse(self):
        path = self.root / 'real'
        path.write_text('x')
        (self.root / 'link').symlink_to(path)
        with self.assertRaisesRegex(core.IntegrityError, 'file_link_or_device'):
            self.make().before_admission(0)
        (self.root / 'link').unlink()
        with patch.object(budget.resource, 'getrlimit', return_value=(101, 101)):
            with self.assertRaisesRegex(core.IntegrityError, 'file_limit_changed'):
                self.make().before_admission(0)

    def test_directory_link_and_oversized_file_refuse(self):
        (self.root / 'dir').mkdir()
        (self.root / 'alias').symlink_to(self.root / 'dir', target_is_directory=True)
        with self.assertRaisesRegex(core.IntegrityError, 'directory_link'):
            self.make().before_admission(0)
        (self.root / 'alias').unlink()
        (self.root / 'large').write_bytes(b'x' * 101)
        with self.assertRaisesRegex(core.IntegrityError, 'oversized_file'):
            self.make().before_admission(0)

    def test_invalid_reservation_and_boolean_limits_refuse(self):
        for change in [dict(maximum_claims=True), dict(reserve_bytes=299),
                       dict(maximum_retained_bytes=299)]:
            with self.assertRaises(core.IntegrityError):
                self.make(**change)

    def test_unreadable_nested_subtree_refuses(self):
        nested = self.root / 'unreadable'
        nested.mkdir()
        (nested / 'retained').write_bytes(b'x' * 900)
        original = os.scandir
        def denied(path):
            if Path(path) == nested:
                raise PermissionError('fixture denied nested scan')
            return original(path)
        with patch.object(os, 'scandir', side_effect=denied):
            with self.assertRaises(PermissionError):
                self.make().before_admission(0)

    def test_stricter_existing_soft_limit_is_never_raised(self):
        with patch.object(budget.resource, 'getrlimit', return_value=(64, 1024)):
            with patch.object(budget.resource, 'setrlimit') as setter:
                with self.assertRaisesRegex(core.IntegrityError, 'soft_limit_too_small'):
                    budget.install_file_limit(128)
                setter.assert_not_called()

    def test_actual_subprocess_limit_preserves_partial_without_affecting_parent(self):
        before = self.real_getrlimit(resource.RLIMIT_FSIZE)
        source = '''
import errno,json,pathlib,resource,sys
from causes.campaign_supplement_budget import install_file_limit
p=pathlib.Path(sys.argv[1]);record=install_file_limit(128)
try:
 with p.open('xb',buffering=0) as stream:
  stream.write(b'a'*100);stream.write(b'b'*100);stream.write(b'c')
except OSError as error:
 assert error.errno==errno.EFBIG
else:raise AssertionError('write unexpectedly unbounded')
assert p.stat().st_size==128
print(json.dumps(dict(record=record,bytes=p.stat().st_size)))
'''
        result = subprocess.run([sys.executable, '-c', source, str(self.root / 'partial')],
                                check=True, text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout)['bytes'], 128)
        self.assertEqual(self.real_getrlimit(resource.RLIMIT_FSIZE), before)
        self.assertEqual((self.root / 'partial').read_bytes(), b'a' * 100 + b'b' * 28)


if __name__ == '__main__':
    unittest.main()
