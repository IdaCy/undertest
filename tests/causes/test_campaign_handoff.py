import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from causes.campaign_handoff import (
    Authority, checked, create_owned_barrier, pin, record_and_exec,
    release_owned_barrier, retire_exact_idle_owner, verify_owned_barrier,
)


OWNER = '''
import fcntl, json, os, sys, time
from pathlib import Path
root=Path(sys.argv[1])
lock=(root/'.dispatcher.lock').open('a+')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
(root/'READY.json').write_text(json.dumps({'pid':os.getpid(),'process_starttime_ticks':ticks,'argv':[sys.executable,'-c',sys.argv[2],str(root),sys.argv[2]]}))
while True:time.sleep(1)
'''


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.pause = self.base / 'PAUSE'
        self.sequence = 0

    def authority(self, action='fixture_action', policy=lambda value: True, expired=False):
        self.sequence += 1
        current = dt.datetime.now(dt.timezone.utc)
        path = self.base / f'AUTHORITY-{self.sequence}.json'
        path.write_text(json.dumps(dict(root_authorized=True, no_user_revocation=True, action=action,
                        issued_at=(current - dt.timedelta(seconds=100 if expired else 1)).isoformat(),
                        expires_at=(current + dt.timedelta(seconds=-1 if expired else 120)).isoformat())))
        return Authority(pin(path), action, (self.pause,), policy)

    def owner(self):
        root = self.base / 'owner'
        (root / '.dispatcher').mkdir(parents=True)
        journal = root / '.dispatcher/journal.jsonl'
        journal.write_text('{"event":"synthetic_complete_idle"}\n')
        argv = [sys.executable, '-c', OWNER, str(root), OWNER]
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            child.stderr.close()
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (root / 'READY.json').exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail(child.stderr.read().decode())
            time.sleep(.01)
        self.assertTrue((root / 'READY.json').exists())
        process = json.loads((root / 'READY.json').read_text())
        self.assertEqual(process['argv'], argv)
        return child, root, process, pin(journal)

    def test_exact_owned_idle_process_receives_one_signal_and_retires_with_free_lock(self):
        child, root, process, journal = self.owner()
        with patch('causes.campaign_handoff.os.kill', wraps=os.kill) as kill:
            reference = retire_exact_idle_owner(process, root, journal, self.base / 'retirement', self.authority())
        kill.assert_called_once_with(child.pid, signal.SIGTERM)
        result = checked(reference)
        self.assertTrue(result['retired'])
        self.assertTrue(result['output_lock_free'])
        self.assertEqual(result['process'], process)
        self.assertTrue(result['observations'][-1]['output_lock_free'])
        child.wait(timeout=5)
        with patch('causes.campaign_handoff.os.kill') as kill, self.assertRaises((ValueError, FileNotFoundError)):
            retire_exact_idle_owner(process, root, journal, self.base / 'retirement', self.authority())
        kill.assert_not_called()

    def test_changed_start_ticks_or_journal_cannot_signal_the_owner(self):
        child, root, process, journal = self.owner()
        altered = {**process, 'process_starttime_ticks': str(int(process['process_starttime_ticks']) + 1)}
        with patch('causes.campaign_handoff.os.kill') as kill:
            with self.assertRaisesRegex(ValueError, 'process_identity_changed'):
                retire_exact_idle_owner(altered, root, journal, self.base / 'wrong_process', self.authority())
            Path(journal['path']).write_text('{"event":"unexpected_new_event"}\n')
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                retire_exact_idle_owner(process, root, journal, self.base / 'wrong_journal', self.authority())
        kill.assert_not_called()
        self.assertIsNone(child.poll())

    def make_barrier(self):
        path = self.base / 'ADMISSION_BARRIER'
        reference = create_owned_barrier(path, self.base / 'new_output', 'fixture', self.base / 'creation', self.authority())
        return path, reference

    def test_only_exact_owned_barrier_is_released_and_receipts_remain(self):
        path, reference = self.make_barrier()
        self.assertEqual(verify_owned_barrier(reference, path)['label'], 'fixture')
        result = release_owned_barrier(reference, path, self.base / 'release', self.authority(), lambda owned: True)
        self.assertFalse(path.exists())
        self.assertTrue(checked(result)['no_pause_marker_removed'])
        self.assertTrue(Path(checked(reference)['archived_marker']['path']).is_file())
        self.assertFalse(self.pause.exists())

    def test_same_byte_barrier_replacement_is_refused(self):
        path, reference = self.make_barrier()
        original = path.read_bytes()
        path.rename(self.base / 'BARRIER.original')
        path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            release_owned_barrier(reference, path, self.base / 'release', self.authority(), lambda owned: True)
        self.assertEqual(path.read_bytes(), original)

    def test_waiting_check_pause_cannot_be_removed_or_bypassed(self):
        path, reference = self.make_barrier()
        def pause_during_check(owned):
            self.pause.write_bytes(b'')
            return True
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            release_owned_barrier(reference, path, self.base / 'release', self.authority(), pause_during_check)
        self.assertTrue(path.is_file())
        self.assertTrue(self.pause.is_file())

    def test_final_waiting_check_replacement_leaves_failed_attempt_and_preserves_marker(self):
        path, reference = self.make_barrier()
        calls = 0
        def replace_during_final_check(owned):
            nonlocal calls
            calls += 1
            if calls == 2:
                raw = path.read_bytes()
                path.rename(self.base / 'BARRIER.before_replacement')
                path.write_bytes(raw)
            return True
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            release_owned_barrier(reference, path, self.base / 'release', self.authority(), replace_during_final_check)
        self.assertTrue(path.is_file())
        failure = json.loads((self.base / 'release/FAILED.json').read_bytes())
        self.assertFalse(failure['barrier_removed'])
        self.assertTrue(failure['manual_reconciliation_required'])

    def test_expired_or_denied_authority_and_reserved_pause_name_refuse_creation(self):
        for authority in (self.authority(expired=True), self.authority(policy=lambda value: False)):
            with self.assertRaises(ValueError):
                create_owned_barrier(self.base / 'ADMISSION_BARRIER', self.base / 'new_output', 'fixture',
                                     self.base / 'creation', authority)
        with self.assertRaisesRegex(ValueError, 'pause_marker'):
            create_owned_barrier(self.pause, self.base / 'new_output', 'fixture', self.base / 'creation', self.authority())
        self.assertFalse((self.base / 'creation').exists())
        self.assertFalse(self.pause.exists())

    def test_exec_receipt_survives_exec_with_same_pid_and_intent(self):
        result = self.base / 'EXEC_PID'
        argv = [sys.executable, '-c', 'import os,sys;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()))', str(result)]
        intent = self.base / 'INTENT.json'
        intent.write_text(json.dumps({'argv': argv}))
        reference = pin(intent)
        receipt = self.base / 'PROCESS.json'
        command = [sys.executable, '-m', 'causes.campaign_handoff', str(receipt), json.dumps(argv), str(intent), reference['sha256']]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr.decode())
        actual = json.loads(receipt.read_bytes())
        self.assertEqual(actual['pid'], child.pid)
        self.assertEqual(int(result.read_text()), child.pid)
        self.assertEqual(actual['argv'], argv)
        self.assertEqual(actual['intent_sha256'], reference['sha256'])
        with patch('causes.campaign_handoff.os.execvp') as execute, self.assertRaisesRegex(ValueError, 'argv_changed'):
            record_and_exec(self.base / 'wrong.json', [sys.executable, '-c', 'pass'], reference)
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
