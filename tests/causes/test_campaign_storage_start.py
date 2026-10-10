import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from causes import campaign_handoff as h
from causes import campaign_storage_policy as policy
from causes import campaign_storage_start as start
import test_campaign_storage_policy as policy_tests


class WaitingStartTests(unittest.TestCase):
    def setUp(self):
        self.fixture = policy_tests.StoragePolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base
        self.policy = self.fixture.static()
        self.output = Path(self.policy['output_root'])
        self.output.mkdir()
        self.barrier = self.base / 'START_BARRIER'
        marker = h.save_new(self.base, self.barrier.name, {'fixture': 'held'})
        archive = h.save_new(self.base, 'ARCHIVED_BARRIER', {'fixture': 'held'})
        self.owned = self.save({'marker': marker, 'archived_marker': archive,
            'stat': h.stat_identity(self.barrier), 'output_root': str(self.output)})
        launch = self.policy['launch']
        launch['record_directory'] = str(self.base / 'owner-records')
        launch['owned_barrier'] = self.owned
        launch['options'].update(pause_file=str(self.base / 'PAUSE'),
            global_pause_file=str(self.base / 'GLOBAL_PAUSE'), start_barrier_file=str(self.barrier))
        self.operator = self.fixture.operator(self.policy)
        self.records = self.base / 'start-records'
        self.plan = {'schema': 'causes.storage_waiting_start_plan.v2',
            'source': h.pin(Path(start.__file__).resolve()), 'role': 'mi',
            'sources': [h.pin(Path(policy.__file__).resolve())],
            'policy': self.operator.policy_ref, 'readiness': self.operator.readiness_pair,
            'root_adoption': self.save({'fixture': 'start adoption'}),
            'python': h.pin(Path(sys.executable).resolve()),
            'receipt_directory': str(self.records), 'cwd': str(self.base),
            'pythonpath': str(Path(policy.__file__).resolve().parent.parent)}
        self.emit_metadata = True
        self.owner_change = None
        self.after_spawn = None
        self.process = Mock(pid=os.getpid())
        self.process.poll.return_value = None
        self.live = self.mock(policy.StorageAdmission, 'live', return_value={})
        self.identity = self.mock(h, 'process_identity', side_effect=lambda process: process)
        self.mock(start.observation, 'observe_guard', return_value={'healthy': True})
        self.real_popen = subprocess.Popen
        self.spawn = self.mock(start.subprocess, 'Popen', side_effect=self.fake_spawn)
        self.mock(start.time, 'sleep')
        self.signal = self.mock(os, 'kill', side_effect=AssertionError('no signal allowed'))

    def mock(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save(self, value):
        return self.fixture.save(value)

    def fake_spawn(self, command, **kwargs):
        self.expected = {'pid': self.process.pid, 'process_starttime_ticks':
            Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19], 'argv': command}
        if self.emit_metadata:
            meta = self.output / '.dispatcher'
            controller = meta / 'controllers/owner'
            controller.mkdir(parents=True)
            lock = (self.output / '.dispatcher.lock').open('xb')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.addCleanup(lock.close)
            queue = {'jobs_sha256': 'fixture'}
            h.save_new(meta, 'queue_identity.json', queue)
            fields = {**queue, 'workers': 8, 'max_full': 2,
                      'start_barrier_file': str(self.barrier)}
            h.save_new(controller, 'identity.json', fields)
            self.events = [{'event': 'controller_started', 'controller_id': 'owner', **queue},
                {'event': 'admission_barrier_waiting', 'controller_id': 'owner',
                 'active': 0, 'pending': 1}]
            self.write_journal()
            directory = Path(self.policy['launch']['record_directory'])
            directory.mkdir()
            owner = {'process': self.expected, 'controller_id': 'owner',
                'guard_directory': str(self.base / 'guard'), 'guard_reporting_lock': {}}
            if self.owner_change:
                self.owner_change(owner)
            h.save_new(directory, 'OWNER.json', owner)
        if self.after_spawn:
            self.after_spawn()
        return self.process

    def write_journal(self):
        (self.output / '.dispatcher/journal.jsonl').write_bytes(
            b''.join(h.encoded(event) + b'\n' for event in self.events))

    def inputs(self, authority_delta=None):
        plan = self.save(self.plan)
        issued = dt.datetime.now(dt.timezone.utc)
        authority = {'root_authorized': True, 'no_user_revocation': True,
            'action': start.runtime.START, 'issued_at': issued.isoformat(),
            'expires_at': (issued + dt.timedelta(seconds=120)).isoformat(),
            'policy': self.operator.policy_ref, 'readiness': self.operator.readiness_pair,
            'start_plan': plan, 'root_adoption': self.plan['root_adoption'],
            'start_preparation_review': self.save({
                'accepted_waiting_owner_start_preparation': True, 'plan': plan})}
        authority.update(authority_delta or {})
        return plan, self.save(authority)

    def invoke(self, **authority_delta):
        return start.start(*self.inputs(authority_delta))

    def assert_uncertain_once(self, *, spawned=True):
        self.assertEqual(self.spawn.call_count, int(spawned))
        self.signal.assert_not_called()
        for method in ('terminate', 'kill', 'send_signal', 'wait'):
            getattr(self.process, method).assert_not_called()
        failed = json.loads((self.records / 'FAILED.json').read_text())
        self.assertEqual(failed['spawned_pid'], self.process.pid if spawned else None)
        self.assertTrue(failed['no_retry'])
        self.assertTrue(failed['no_signal_sent'])
        self.assertFalse((self.records / 'RESULT.json').exists())

    def test_single_detached_start_keeps_barrier_and_exact_owner_guard_receipt(self):
        result = self.invoke()
        self.assertTrue(result['passed'])
        record = h.checked(result['result'])
        self.assertEqual(record['owner']['process'], self.expected)
        self.assertEqual(record['owner']['guard']['binding'], self.policy['launch']['storage_binding'])
        self.assertEqual(record['owner']['initial_pending'], 1)
        self.assertEqual(record['observation']['claims'], 0)
        self.assertEqual(record['observation']['persisted_blocks'], 0)
        self.assertFalse(record['barrier_released'])
        h.verify_owned_barrier(self.owned, self.barrier)
        self.spawn.assert_called_once()
        kwargs = self.spawn.call_args.kwargs
        self.assertEqual(kwargs['cwd'], self.base)
        self.assertEqual(kwargs['env']['PYTHONPATH'], self.plan['pythonpath'])
        self.assertEqual(kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(kwargs['start_new_session'])
        self.assertIn('--execute', self.spawn.call_args.args[0])
        self.identity.assert_called_with(self.expected)
        self.assertFalse((self.records / 'FAILED.json').exists())
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.spawn.assert_called_once()

    def test_wrong_or_unaccepted_authority_prevents_spawn_and_receipts(self):
        for delta in ({'root_authorized': False}, {'start_plan': self.operator.policy_ref}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.invoke(**delta)
        self.spawn.assert_not_called()
        self.assertFalse(self.records.exists())

    def test_runtime_package_must_match_loaded_validation_code(self):
        self.plan['pythonpath'] = str(self.base)
        with self.assertRaisesRegex(ValueError, 'waiting_start_runtime_scope_changed'):
            self.invoke()
        self.spawn.assert_not_called()
        self.assertFalse(self.records.exists())

    def test_child_import_resolution_cannot_be_shadowed_by_working_directory(self):
        shadow = self.base / 'causes'
        shadow.mkdir()
        (shadow / '__init__.py').write_text('')
        shadow_policy = shadow / 'campaign_storage_policy.py'
        shadow_policy.write_text('fixture = True\n')
        self.invoke()
        command = self.spawn.call_args.args[0]
        kwargs = self.spawn.call_args.kwargs
        flags = command[1:command.index('-m')]
        probe = "import importlib.util; print(importlib.util.find_spec('causes.campaign_storage_policy').origin)"
        with patch.object(subprocess, 'Popen', self.real_popen):
            unsafe = subprocess.run([command[0], *[arg for arg in flags if arg != '-P'],
                                     '-c', probe], cwd=kwargs['cwd'], env=kwargs['env'],
                                    capture_output=True, text=True, timeout=10, check=True)
            safe = subprocess.run([command[0], *flags, '-c', probe],
                                  cwd=kwargs['cwd'], env=kwargs['env'],
                                  capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(Path(unsafe.stdout.strip()), shadow_policy)
        self.assertEqual(Path(safe.stdout.strip()), Path(policy.__file__).resolve())
        h.verify_owned_barrier(self.owned, self.barrier)

    def test_missing_working_directory_refuses_without_spawning(self):
        self.plan['cwd'] = str(self.base / 'absent')
        with self.assertRaisesRegex(ValueError, 'waiting_start_runtime_scope_changed'):
            self.invoke()
        self.spawn.assert_not_called()

    def test_user_pause_between_authority_checks_keeps_intent_without_spawn(self):
        calls = 0
        def pause_second_check(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.base / 'PAUSE').write_bytes(b'user')
        self.live.side_effect = pause_second_check
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.invoke()
        self.assert_uncertain_once(spawned=False)
        self.assertTrue((self.records / 'INTENT.json').exists())
        h.verify_owned_barrier(self.owned, self.barrier)

    def test_exact_60_second_timeout_retains_uncertain_process_without_retry(self):
        self.emit_metadata = False
        with patch.object(start.time, 'monotonic', side_effect=[10, 70]):
            with self.assertRaisesRegex(ValueError, 'waiting_owner_observation_timeout_no_retry'):
                self.invoke()
        self.assert_uncertain_once()
        self.assertTrue((self.records / 'SPAWNED.json').exists())
        h.verify_owned_barrier(self.owned, self.barrier)

    def test_exit_before_acceptance_is_not_restarted(self):
        self.process.poll.return_value = 7
        with self.assertRaisesRegex(ValueError, 'waiting_owner_exited_before_acceptance'):
            self.invoke()
        self.assert_uncertain_once()

    def test_other_owner_receipt_cannot_certify_spawn(self):
        self.owner_change = lambda owner: owner.update(process={**self.expected, 'pid': 1})
        with self.assertRaisesRegex(ValueError, 'waiting_owner_spawn_identity_changed'):
            self.invoke()
        self.assert_uncertain_once()

    def test_removed_barrier_is_not_reported_as_accepted_waiting(self):
        self.after_spawn = self.barrier.unlink
        with self.assertRaises(FileNotFoundError):
            self.invoke()
        self.assert_uncertain_once()

    def test_claim_cannot_pass_waiting_acceptance(self):
        def claim():
            self.events.append({'event': 'claimed', 'controller_id': 'owner', 'run_id': 'job'})
            self.write_journal()
        self.after_spawn = claim
        with self.assertRaisesRegex(ValueError, 'waiting_storage_owner_issued_work'):
            self.invoke()
        self.assert_uncertain_once()

    def test_assignment_directory_without_claim_cannot_pass_waiting_acceptance(self):
        self.after_spawn = lambda: (self.output / 'unexpected-attempt').mkdir()
        with self.assertRaisesRegex(ValueError, 'waiting_storage_owner_created_attempt'):
            self.invoke()
        self.assert_uncertain_once()

    def test_changed_guard_is_not_reported_as_healthy(self):
        with patch.object(start.observation, 'observe_guard', return_value={'healthy': False}):
            with self.assertRaisesRegex(ValueError, 'storage_guard_observation_unhealthy'):
                self.invoke()
        self.assert_uncertain_once()

    def test_post_observation_identity_failure_preserves_uncertainty(self):
        calls = 0
        def identity(process):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValueError('fixture process changed after observation')
            return process
        self.identity.side_effect = identity
        with self.assertRaisesRegex(ValueError, 'fixture process changed'):
            self.invoke()
        self.assert_uncertain_once()


if __name__ == '__main__':
    unittest.main()
