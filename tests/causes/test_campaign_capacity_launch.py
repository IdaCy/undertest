from contextlib import ExitStack
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from causes import campaign_capacity_launch as launch
from causes import campaign_handoff as h


class CapacityLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.campaign = Path(self.temp.name)
        self.deployment = self.campaign / 'deployment'
        self.runtime = self.deployment / 'runtime'
        self.runtime.mkdir(parents=True)
        self.records = self.deployment / 'launch'
        self.pause = self.campaign / 'PAUSE'
        self.barrier = self.campaign / 'ADMISSION_BARRIER'
        self.barrier.write_text('held')
        self.sequence = 0
        self.python = h.pin(Path(sys.executable).resolve())
        self.trial_ref = self.save({'fixture': 'trial'})
        self.command = [self.python['path'], '-B', '-s', '-m',
                        'causes.campaign_capacity_trial', '--plan', self.trial_ref['path']]
        self.binding = {'guard_argv': self.command,
                        'owned_startup_barrier': {'fixture': 'owned barrier'},
                        'lease_path': str(self.deployment / 'guard' / 'LEASE.json')}
        self.owner = {'observation_state': 'waiting',
                      'process': {'pid': 42, 'process_starttime_ticks': '123', 'argv': ['owner']},
                      'guard': {'binding': {'fixture': 'storage'}},
                      'owned_barrier': self.binding['owned_startup_barrier'],
                      'barrier_path': str(self.barrier)}
        self.value = {'owner_process': self.owner['process'], 'capacity_guard': self.binding,
                      'sampling': {'guard': self.owner['guard'], 'controls_absent': [str(self.pause)]},
                      'output': str(self.deployment / 'trial-output')}
        registration = self.save({'owners': {'new_mi': self.owner}})
        waiting_review = self.save({'accepted_actual_waiting_owner': True,
                                   'accepted_storage_phase_registration': True,
                                   'registration': registration})
        launcher = h.pin(Path(launch.__file__).resolve())
        adoption = self.save({'authorized_candidate_guard_supervision': True,
                              'trial_plan': self.trial_ref, 'guard_launch_source': launcher})
        self.plan = {'schema': 'causes.capacity_guard_launch_plan.v1', 'launcher': launcher,
                     'root_adoption': adoption, 'trial_plan': self.trial_ref,
                     'python': self.python, 'pythonpath': str(self.runtime),
                     'cwd': str(self.campaign), 'record_directory': str(self.records),
                     'waiting_registration': {'evidence': registration, 'review': waiting_review}}
        self.expected_process = {'pid': os.getpid(), 'process_starttime_ticks':
            Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19], 'argv': self.command}
        self.exit_code = None
        self.make_lease = True
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(launch.trial, 'load_trial',
                                               return_value=(self.value, None, None, None)))
        self.observe = self.patches.enter_context(patch.object(launch.observation, 'observe_owner'))
        self.patches.enter_context(patch.object(launch.h, 'verify_owned_barrier'))
        self.patches.enter_context(patch.object(launch.h, 'process_identity'))
        self.patches.enter_context(patch.object(launch.time, 'sleep'))
        self.lease = self.patches.enter_context(patch.object(launch.storage, 'validate_capacity_guard',
                                               return_value={'guard': self.expected_process}))
        self.spawn = self.patches.enter_context(patch.object(launch.subprocess, 'Popen',
                                                             side_effect=self.fake_spawn))

    def save(self, value):
        self.sequence += 1
        return h.save_new(self.campaign, f'input-{self.sequence}.json', value)

    def fake_spawn(self, *args, **kwargs):
        if self.make_lease:
            path = Path(self.binding['lease_path'])
            path.parent.mkdir()
            path.write_text('{}')
        process = Mock(pid=os.getpid())
        process.poll.return_value = self.exit_code
        return process

    def invoke(self):
        plan = self.save(self.plan)
        review = self.save({'accepted_capacity_guard_launch_preparation': True, 'plan': plan})
        issued = dt.datetime.now(dt.timezone.utc)
        authority = self.save({'root_authorized': True, 'no_user_revocation': True,
            'action': 'start_capacity_guard', 'issued_at': issued.isoformat(),
            'expires_at': (issued + dt.timedelta(seconds=120)).isoformat(),
            'plan': plan, 'independent_review': review, 'root_adoption': self.plan['root_adoption']})
        return launch.launch(plan, review, authority, deployment_directory=self.deployment,
                             runtime_directory=self.runtime, campaign_directory=self.campaign)

    def test_success_retains_one_spawn_and_healthy_lease_without_releasing_barrier(self):
        result = self.invoke()
        self.assertTrue(result['passed'])
        record = h.checked(result['result'])
        self.assertEqual(record['process'], self.expected_process)
        self.assertFalse(record['barrier_release'])
        self.assertEqual(self.barrier.read_text(), 'held')
        self.spawn.assert_called_once()
        self.assertEqual(self.spawn.call_args.args, (self.command,))
        self.assertTrue(self.spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(self.spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.lease.assert_called_once_with(self.binding, self.owner['process'], require_healthy=True)
        self.assertTrue((self.records / 'INTENT.json').exists())
        self.assertTrue((self.records / 'SPAWN.json').exists())
        self.assertFalse((self.records / 'FAILED.json').exists())

    def test_existing_user_pause_blocks_before_any_launch_record(self):
        self.pause.touch()
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.invoke()
        self.spawn.assert_not_called()
        self.assertFalse(self.records.exists())

    def test_user_pause_during_final_owner_check_preserves_intent_without_spawn(self):
        calls = 0
        def pause_on_second_check(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.pause.touch()
        self.observe.side_effect = pause_on_second_check
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.invoke()
        self.spawn.assert_not_called()
        self.assertTrue((self.records / 'INTENT.json').exists())
        self.assertTrue((self.records / 'FAILED.json').exists())

    def test_wrong_working_directory_refuses_before_spawn(self):
        self.plan['cwd'] = str(self.deployment)
        with self.assertRaisesRegex(ValueError, 'launch_command_changed'):
            self.invoke()
        self.spawn.assert_not_called()
        self.assertFalse(self.records.exists())

    def test_preexisting_trial_output_is_not_reused(self):
        Path(self.value['output']).mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh_capacity_guard_launch_paths_required'):
            self.invoke()
        self.spawn.assert_not_called()

    def test_early_exit_retains_actual_return_code_and_never_retries(self):
        self.exit_code = 3
        with self.assertRaisesRegex(ValueError, 'exited_reconcile_no_retry'):
            self.invoke()
        self.spawn.assert_called_once()
        record = json.loads((self.records / 'SPAWN.json').read_text())
        self.assertEqual(record['returncode_if_exited'], 3)
        self.assertFalse(record['live_after_launch'])
        self.assertTrue((self.records / 'FAILED.json').exists())
        self.assertFalse((self.records / 'RESULT.json').exists())

    def test_initial_lease_deadline_retains_uncertain_process_without_retry(self):
        self.make_lease = False
        with patch.object(launch.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(ValueError, 'initial_healthy_lease_unproven'):
                self.invoke()
        self.spawn.assert_called_once()
        failed = json.loads((self.records / 'FAILED.json').read_text())
        self.assertTrue(failed['partial_or_detached_state_must_be_reconciled'])
        self.assertFalse(failed['automatic_retry'])
        self.assertEqual(self.barrier.read_text(), 'held')

    def test_lease_from_other_guard_does_not_prove_startup(self):
        self.lease.return_value = {'guard': {**self.expected_process, 'pid': 1}}
        with self.assertRaisesRegex(ValueError, 'initial_healthy_lease_unproven'):
            self.invoke()
        self.spawn.assert_called_once()
        self.assertTrue((self.records / 'FAILED.json').exists())


if __name__ == '__main__':
    unittest.main()
