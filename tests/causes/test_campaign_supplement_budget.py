import errno
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_supplement as core
from causes import campaign_supplement_budget as budget
from shared import bounded_timing as timing


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


    def test_exact_paths_identities_sizes_and_empty_directories(self):
        paths = ['plain', '.hidden/journal', 'nested/deeper/sp ace', 'nested/évidence']
        expected = {}
        for index, relative in enumerate(paths):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * index)
            value = path.stat()
            expected[str(path)] = dict(device=value.st_dev, inode=value.st_ino, bytes=index)
        (self.root / 'empty').mkdir()
        self.assertEqual(budget.inventory(self.root),
                         dict(root=str(self.root), bytes=6, files=expected))

    def test_every_call_observes_growth_and_new_files(self):
        path = self.root / 'journal'
        path.write_bytes(b'x')
        guard = self.make()
        self.assertEqual(guard.before_admission(0)['retained_bytes'], 1)
        with path.open('ab') as stream:
            stream.write(b'yy')
        (self.root / 'new').write_bytes(b'zzzz')
        self.assertEqual(guard.before_admission(1)['retained_bytes'], 7)
        self.assertEqual(guard.before_admission(2)['retained_bytes'], 7)

    def test_disappearance_refuses_and_keeps_last_accepted_inventory(self):
        path = self.root / 'retained'
        path.write_bytes(b'x')
        guard = self.make()
        guard.before_admission(0)
        previous = guard.previous
        path.unlink()
        with self.assertRaisesRegex(core.IntegrityError, 'file_disappeared'):
            guard.before_admission(1)
        self.assertIs(guard.previous, previous)

    def test_same_size_replacement_refuses(self):
        path = self.root / 'retained'
        path.write_bytes(b'x')
        guard = self.make()
        guard.before_admission(0)
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'y')
        replacement.replace(path)
        with self.assertRaisesRegex(core.IntegrityError, 'replaced_or_shrunk'):
            guard.before_admission(1)
        self.assertEqual(path.read_bytes(), b'y')

    def test_hardlink_fifo_and_broken_link_refuse(self):
        path = self.root / 'retained'
        path.write_bytes(b'x')
        link = self.root / 'link'
        os.link(path, link)
        with self.assertRaisesRegex(core.IntegrityError, 'file_link_or_device'):
            budget.inventory(self.root)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaisesRegex(core.IntegrityError, 'file_link_or_device'):
            budget.inventory(self.root)
        link.unlink()
        link.symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(core.IntegrityError, 'file_link_or_device'):
            budget.inventory(self.root)

    def test_missing_file_root_and_noncanonical_alias_refuse(self):
        with self.assertRaisesRegex(core.IntegrityError, 'output_missing'):
            budget.inventory(self.root / 'absent')
        path = self.root / 'file'
        path.write_bytes(b'x')
        with self.assertRaisesRegex(core.IntegrityError, 'output_missing'):
            budget.inventory(path)
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(core.IntegrityError):
            budget.inventory(alias)

    def test_directory_classification_race_still_refuses(self):
        directory = self.root / 'directory'
        directory.mkdir()
        (directory / 'retained').write_bytes(b'x')
        original = os.lstat
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            swapped = False
            def substitute(path, *args, **kwargs):
                nonlocal swapped
                if str(path) == str(directory) and not swapped:
                    swapped = True
                    directory.rename(external / 'original-directory')
                    directory.symlink_to(external, target_is_directory=True)
                return original(path, *args, **kwargs)
            with patch.object(os, 'lstat', side_effect=substitute):
                with self.assertRaisesRegex(core.IntegrityError, 'directory_link'):
                    budget.inventory(self.root)
            directory.unlink()

    def test_file_stat_errors_and_device_types_still_refuse(self):
        path = self.root / 'retained'
        path.write_bytes(b'x')
        original = os.lstat
        for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK, None):
            with self.subTest(mode=mode):
                def altered(name, *args, **kwargs):
                    value = original(name, *args, **kwargs)
                    if str(name) == str(path):
                        if mode is None:
                            raise PermissionError('fixture stat denied')
                        fields = list(value)
                        fields[0] = mode | 0o600
                        return os.stat_result(fields)
                    return value
                with patch.object(os, 'lstat', side_effect=altered):
                    if mode is None:
                        with self.assertRaises(PermissionError):
                            budget.inventory(self.root)
                    else:
                        with self.assertRaisesRegex(core.IntegrityError, 'file_link_or_device'):
                            budget.inventory(self.root)

    def test_deep_tree_under_low_recursion_and_descriptor_limits(self):
        leaf = self.root
        for _ in range(48):
            leaf /= 'd'
            leaf.mkdir()
        (leaf / 'retained').write_bytes(b'x')
        source = '''
import json,resource,sys
from causes.campaign_supplement_budget import inventory
if sys.argv[2]=='recursion':sys.setrecursionlimit(40)
else:
 soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
 resource.setrlimit(resource.RLIMIT_NOFILE,(32,hard))
result=inventory(sys.argv[1])
assert result['bytes']==1 and len(result['files'])==1
print(json.dumps(dict(files=1,bytes=1)))
'''
        for limit in ('recursion', 'descriptors'):
            with self.subTest(limit=limit):
                result = subprocess.run([sys.executable, '-B', '-c', source, str(self.root), limit],
                                        check=True, text=True, capture_output=True)
                self.assertEqual(json.loads(result.stdout), dict(files=1, bytes=1))


class TimedAdmissionBudgetTests(AdmissionBudgetTests):
    """Run the same semantic matrix with the optional hook enabled."""
    def make(self, **changes):
        guard = super().make(**changes)
        before_admission = guard.before_admission
        sampler = timing.Sampler(enabled=True)
        def measured(claimed):
            return before_admission(claimed, timing=sampler.begin())
        guard.before_admission = measured
        return guard


class BudgetTimingBoundaryTests(unittest.TestCase):
    # Only the explicit boundaries below; the complete matrix runs twice above.
    setUp = AdmissionBudgetTests.setUp
    make = AdmissionBudgetTests.make

    def test_timing_preserves_inventory_and_validation_order(self):
        guard = self.make()
        events = []
        def clock():
            events.append('clock')
            return len(events)
        def limit(_):
            events.append('limit')
            return (100, 100)
        actual_inventory = budget.inventory
        def inventory(root):
            events.append('inventory')
            return actual_inventory(root)
        sample = timing.Sampler(enabled=True, clock=clock).begin()
        with patch.object(budget.resource, 'getrlimit', side_effect=limit):
            with patch.object(budget, 'inventory', side_effect=inventory) as observed:
                result = guard.before_admission(0, timing=sample)
        observed.assert_called_once_with(self.root)
        self.assertEqual(events, ['clock', 'limit', 'clock', 'clock', 'inventory',
                                  'clock', 'clock', 'clock'])
        self.assertEqual(result['retained_bytes'], 0)
        self.assertEqual([row['code'] for row in timing.annotation(sample)['spans']],
                         [budget.TIMING_PRECHECK, budget.TIMING_INVENTORY, budget.TIMING_VALIDATION])

    def test_timer_failure_keeps_exact_inventory_exception_and_previous(self):
        guard = self.make()
        guard.before_admission(0)
        previous = guard.previous
        error = PermissionError('fixture inventory refused')
        sample = timing.Sampler(enabled=True, clock=lambda: (_ for _ in ()).throw(OSError())).begin()
        with patch.object(budget, 'inventory', side_effect=error) as observed:
            with self.assertRaises(PermissionError) as caught:
                guard.before_admission(1, timing=sample)
        observed.assert_called_once_with(self.root)
        self.assertIs(caught.exception, error)
        self.assertIs(guard.previous, previous)

    def test_claim_hold_keeps_previous_and_is_not_replaced_by_clock_failure(self):
        guard = self.make()
        guard.before_admission(0)
        previous = guard.previous
        calls = iter((1, 2, 3, 4, 5))
        sample = timing.Sampler(enabled=True, clock=lambda: next(calls)).begin()
        with self.assertRaisesRegex(core.AdmissionHold, 'fixed_admission_tranche_complete'):
            guard.before_admission(64, timing=sample)
        self.assertIs(guard.previous, previous)
        self.assertEqual(timing.annotation(sample)['missing'], ['clock_error'])

    def test_default_hook_has_no_clock_calls(self):
        with patch.object(timing.time, 'monotonic_ns', side_effect=AssertionError('not called')):
            with patch.object(timing.json, 'dumps', side_effect=AssertionError('not called')):
                self.assertEqual(self.make().before_admission(0)['claimed'], 0)


if __name__ == '__main__':
    unittest.main()
