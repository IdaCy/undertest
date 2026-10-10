import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage as s
from causes import campaign_storage_runtime as runtime
from causes.campaign_dispatcher import sha


class RecordedStorageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.mount = self.base / 'mount'
        self.output = self.mount / 'model'
        self.output.mkdir(parents=True)
        self.records = self.base / 'owner_records'
        self.options = {'workers': 8, 'max_full': 2,
                        'pause_file': str(self.base / 'ROLE_PAUSE'),
                        'global_pause_file': str(self.base / 'GLOBAL_PAUSE'),
                        'start_barrier_file': str(self.base / 'ADMISSION_BARRIER'),
                        'admission_holds': str(self.base / 'holds.json'),
                        'admission_holds_sha256': 'fixture'}
        marker = h.save_new(self.base, 'ADMISSION_BARRIER', {'output_root': str(self.output)})
        archive = h.save_new(self.base, 'ARCHIVE', {'output_root': str(self.output)})
        owned = h.save_new(self.base, 'OWNERSHIP.json', {'output_root': str(self.output),
            'marker': marker, 'archived_marker': archive,
            'stat': h.stat_identity(Path(marker['path']))})
        mount = {'mount_id': '48', 'device': '8:32', 'root': '/new',
                 'target': str(self.mount), 'fstype': 'ext4'}
        self.binding = {'schema': 'causes.prospective_storage_binding.v1',
            'backing': str(self.base / 'scratch'), 'mount_target': str(self.mount),
            'output': str(self.output), 'root_volume': str(self.base), 'scratch_device': '2080',
            'backing_inode': '100', 'output_inode': '101', 'root_device': '2048',
            'floor_bytes': 80, 'mount_identity': mount}
        self.observation = {'mount': mount, 'paths': {
            'backing': {'device': '2080', 'inode': '100'},
            'mount_target': {'device': '2080', 'inode': '100'},
            'output': {'device': '2080', 'inode': '101'},
            'root_volume': {'device': '2048', 'inode': '11'}},
            'output_free_bytes': 2000, 'root_free_bytes': 140}
        self.kwargs = {'jobs': [], 'config': {'dispatcher': {'disk_floor_bytes': 80}},
                       'output': str(self.output), 'storage_binding': self.binding,
                       'record_directory': str(self.records), 'owned_barrier': owned,
                       'options': self.options}
        self.policy_calls = 0
        self.after_policy_check = None
        self.started = []
        self.constructed = []

    def authority(self, *, binding_sha=None):
        now = dt.datetime.now(dt.timezone.utc)
        ref = h.save_new(self.base, 'AUTHORITY.json', {'root_authorized': True,
            'no_user_revocation': True, 'action': runtime.START, 'issued_at': now.isoformat(),
            'expires_at': (now + dt.timedelta(seconds=120)).isoformat(),
            'storage_launch_binding_sha256': binding_sha or sha(runtime.launch_binding(**self.kwargs))})
        def validate(_):
            self.policy_calls += 1
            if self.after_policy_check:
                self.after_policy_check(self.policy_calls)
            return True
        return h.Authority(ref, runtime.START,
            tuple(Path(self.options[key]) for key in ('pause_file', 'global_pause_file')), validate)

    def run_fake(self, authority):
        real_guard = s.StorageGuard
        def observed_guard(binding, *, report):
            self.assertIsInstance(report, s.GuardJournal)
            return real_guard(binding, lambda _: self.observation, report)
        testcase = self
        class FakeDispatcher:
            controller_id = 'fixture_owner'
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                testcase.constructed.append(kwargs)
            def run(self):
                testcase.started.append(self.controller_id)
                testcase.assertTrue(Path(self.kwargs['start_barrier_file']).is_file())
                testcase.assertGreaterEqual(self.kwargs['free_bytes'](), 80)
                return {'status': 'paused', 'claims': 0}
        with patch.object(s, 'StorageGuard', side_effect=observed_guard), \
             patch.object(runtime.d, 'Dispatcher', FakeDispatcher):
            return runtime.run_recorded_dispatcher(**self.kwargs, authority=authority)

    def test_mandatory_reporter_owner_receipt_and_barrier_are_retained(self):
        result = self.run_fake(self.authority())
        self.assertEqual(self.started, ['fixture_owner'])
        owner = json.loads((self.records / 'OWNER.json').read_bytes())
        transition = json.loads((self.records / 'guard/00000000.json').read_bytes())
        self.assertEqual(transition['state'], 'ready')
        self.assertEqual(owner['process'], transition['owner'])
        self.assertEqual(owner['controller_id'], 'fixture_owner')
        self.assertFalse(owner['barrier_released'])
        self.assertTrue(Path(self.options['start_barrier_file']).exists())
        self.assertTrue(h.lock_free(self.records / 'guard/reporting.lock'))
        self.assertFalse(h.checked(result)['automatic_restart'])

    def test_wrong_binding_never_creates_launch_records(self):
        with self.assertRaisesRegex(ValueError, 'authority_binding_changed'):
            self.run_fake(self.authority(binding_sha='wrong'))
        self.assertFalse(self.records.exists())
        self.assertFalse(self.constructed)

    def test_user_pause_after_constructor_prevents_dispatcher_start(self):
        def pause(count):
            if count == 2:
                Path(self.options['global_pause_file']).write_text('user pause')
        self.after_policy_check = pause
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.run_fake(self.authority())
        self.assertFalse(self.started)
        failed = json.loads((self.records / 'FAILED.json').read_bytes())
        self.assertFalse(failed['dispatcher_run_entered'])
        self.assertTrue((self.records / 'OWNER.json').is_file())
        self.assertTrue(h.lock_free(self.records / 'guard/reporting.lock'))

    def test_guard_journal_failure_prevents_construction_and_admission(self):
        real_save = h.save_new
        def fail(directory, name, value):
            if name == '00000000.json':
                raise OSError('fixture journal unavailable')
            return real_save(directory, name, value)
        auth = self.authority()
        with patch.object(h, 'save_new', side_effect=fail):
            with self.assertRaises(s.GuardReportingError):
                self.run_fake(auth)
        self.assertFalse(self.constructed)
        self.assertFalse(self.started)
        self.assertTrue(h.lock_free(self.records / 'guard/reporting.lock'))
        self.assertTrue((self.records / 'FAILED.json').is_file())

    def test_existing_output_and_unprotected_guard_records_are_rejected(self):
        auth = self.authority()
        self.kwargs['record_directory'] = str(self.output / 'hidden_guard')
        with self.assertRaisesRegex(ValueError, 'must_survive_output_unmount'):
            self.run_fake(auth)
        self.kwargs['record_directory'] = str(self.records)
        (self.output / 'retained_attempt').mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh_empty_storage_output_required'):
            self.run_fake(auth)
        self.assertTrue((self.output / 'retained_attempt').is_dir())
        self.assertFalse(self.started)

    def test_free_space_callback_or_runner_cannot_override_guards(self):
        auth = self.authority()
        for option in ('free_bytes', 'runner'):
            with self.subTest(option=option):
                self.options[option] = 'override'
                with self.assertRaisesRegex(ValueError, 'exact_storage_dispatcher_options'):
                    self.run_fake(auth)
                self.options.pop(option)
        self.assertFalse(self.started)

    def test_prompt_and_prefix_options_are_copied_and_bound_to_owner(self):
        prompts = {'fixed': 'intervention'}
        policy = {'namespace': 'extra', 'system_prompt': 'baseline'}
        self.options.update(system_prompts_by_run_id=prompts, prefix_extension=policy)
        self.run_fake(self.authority())
        forwarded = self.constructed[0]
        self.assertEqual(forwarded['system_prompts_by_run_id'], prompts)
        self.assertEqual(forwarded['prefix_extension'], policy)
        self.assertIsNot(forwarded['system_prompts_by_run_id'], prompts)
        self.assertIsNot(forwarded['prefix_extension'], policy)
        owner = json.loads((self.records / 'OWNER.json').read_bytes())
        self.assertEqual(owner['binding']['dispatcher_options'], self.options)
        self.assertEqual((forwarded['workers'], forwarded['max_full']), (8, 2))
        self.assertIsInstance(forwarded['free_bytes'], s.StorageGuard)

    def test_changed_prompt_or_prefix_policy_refuses_before_construction(self):
        self.options.update(system_prompts_by_run_id={'fixed': 'intervention'},
                            prefix_extension={'namespace': 'extra', 'system_prompt': 'baseline'})
        authority = self.authority()
        for option, key in (('system_prompts_by_run_id', 'fixed'),
                            ('prefix_extension', 'system_prompt')):
            with self.subTest(option=option):
                original = self.options[option][key]
                self.options[option][key] = 'changed after authority'
                with self.assertRaisesRegex(ValueError, 'authority_binding_changed'):
                    self.run_fake(authority)
                self.options[option][key] = original
        self.assertFalse(self.records.exists())
        self.assertFalse(self.constructed)

    def test_invalid_prompt_map_still_rejected_by_native_dispatcher(self):
        self.kwargs['jobs'] = [{'spec': {}, 'variant': {},
                               'allocation': {'run_id': 'fixed', 'continue_to_end': False}}]
        self.options['system_prompts_by_run_id'] = {'unassigned': 'wrong membership'}
        real_guard = s.StorageGuard
        def observed_guard(binding, *, report):
            return real_guard(binding, lambda _: self.observation, report)
        with patch.object(s, 'StorageGuard', side_effect=observed_guard):
            with self.assertRaisesRegex(ValueError, 'system_prompts_require_exact_fixed_queue'):
                runtime.run_recorded_dispatcher(**self.kwargs, authority=self.authority())
        self.assertFalse((self.output / '.dispatcher').exists())
        self.assertFalse(json.loads((self.records / 'FAILED.json').read_bytes())['dispatcher_run_entered'])

    def test_real_dispatcher_waits_without_claims_and_exits_on_role_pause(self):
        self.assert_real_dispatcher_waiting()

    def test_real_prompt_and_prefix_owner_preserves_barrier_guard_and_identity(self):
        self.assert_real_dispatcher_waiting(prompt_and_prefix=True)

    def assert_real_dispatcher_waiting(self, *, prompt_and_prefix=False):
        holds = h.save_new(self.base, 'holds.json', {'schema': 'caus.admission_holds.v1', 'holds': []})
        self.options['admission_holds_sha256'] = holds['sha256']
        self.kwargs['jobs'] = [{'spec': {'task_id': 'fixture'}, 'variant': {'id': 'baseline'},
                               'allocation': {'run_id': 'never_called', 'continue_to_end': True}}]
        if prompt_and_prefix:
            self.options.update(system_prompts_by_run_id={'never_called': 'fixed intervention'},
                prefix_extension={'namespace': 'extra', 'system_prompt': 'baseline'},
                extension_prototypes=[{'spec': {'task_id': 'fixture'}, 'variant': {'id': 'baseline'},
                    'allocation': {'run_id': 'prototype', 'continue_to_end': False}}])
        authority = self.authority()
        real_guard = s.StorageGuard
        results, failures = [], []
        def observed_guard(binding, *, report):
            return real_guard(binding, lambda _: self.observation, report)
        def run():
            try:
                results.append(runtime.run_recorded_dispatcher(**self.kwargs, authority=authority))
            except BaseException as error:
                failures.append(error)
        with patch.object(s, 'StorageGuard', side_effect=observed_guard), \
             patch('causes.campaign_runner.run_episode', side_effect=AssertionError('model work forbidden')) as runner:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                journal = self.output / '.dispatcher/journal.jsonl'
                deadline = time.monotonic() + 5
                events = []
                while time.monotonic() < deadline:
                    if journal.is_file():
                        raw = journal.read_bytes()
                        if raw.endswith(b'\n'):
                            events = [json.loads(line) for line in raw.splitlines()]
                    if any(row['event'] == 'admission_barrier_waiting' for row in events) or failures:
                        break
                    time.sleep(.01)
                self.assertFalse(failures)
                self.assertTrue(any(row['event'] == 'admission_barrier_waiting'
                                    and row['pending'] == 1 and row['active'] == 0 for row in events))
                self.assertFalse(any(row['event'] in ('claimed', 'extension_block_created') for row in events))
                owner = json.loads((self.records / 'OWNER.json').read_bytes())
                identity = json.loads((self.output / '.dispatcher/controllers' /
                                       owner['controller_id'] / 'identity.json').read_bytes())
                self.assertEqual(identity['jobs_sha256'], sha(self.kwargs['jobs']))
                self.assertEqual(identity['start_barrier_file'], self.options['start_barrier_file'])
                self.assertEqual((identity['workers'], identity['max_full']), (8, 2))
                queue = json.loads((self.output / '.dispatcher/queue_identity.json').read_bytes())
                if prompt_and_prefix:
                    for key, option in (('system_prompts_sha256', 'system_prompts_by_run_id'),
                                        ('prefix_extension_sha256', 'prefix_extension'),
                                        ('prototypes_sha256', 'extension_prototypes')):
                        self.assertEqual(identity[key], sha(self.options[option]))
                        self.assertEqual(queue[key], identity[key])
                else:
                    self.assertNotIn('system_prompts_sha256', queue)
                    self.assertNotIn('prefix_extension_sha256', queue)
                self.assertTrue(h.lock_owned_by(self.output / '.dispatcher.lock', owner['process']['pid']))
                self.assertTrue(h.lock_owned_by(self.records / 'guard/reporting.lock', owner['process']['pid']))
                self.assertFalse((self.output / 'never_called').exists())
                self.assertFalse(list((self.output / '.dispatcher/blocks').glob('*')))
                runner.assert_not_called()
            finally:
                Path(self.options['pause_file']).write_text('fixture role pause')
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertEqual(h.checked(results[0])['result']['status'], 'paused')
            runner.assert_not_called()
        final = [json.loads(line) for line in journal.read_bytes().splitlines()]
        self.assertEqual([row['event'] for row in final][-2:], ['pause_seen', 'controller_paused'])
        self.assertFalse(any(row['event'] == 'claimed' for row in final))
        self.assertTrue(Path(self.options['start_barrier_file']).exists())
        self.assertTrue(h.lock_free(self.output / '.dispatcher.lock'))
        self.assertTrue(h.lock_free(self.records / 'guard/reporting.lock'))


if __name__ == '__main__':
    unittest.main()
