import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes.campaign_dispatcher import sha
from causes.campaign_storage import (StorageGuard, drain_progress, mount_rows,
    partition_fixed_jobs, validate_mixed_phase, validate_pause_routing, guarded_dispatcher,
    fixed_custody_join, GuardJournal, GuardReportingError, observe_guard)


def job(rid):
    return {'spec': {'task_id': rid}, 'variant': {'id': 'baseline'},
            'allocation': {'run_id': rid, 'continue_to_end': False, 'seed': 17}}


def partition_fixture():
    jobs = [job(rid) for rid in ('unstarted_b', 'completed', 'partial', 'held', 'unstarted_a')]
    config = {'model': 'fixture'}
    histories = [{'root': '/retained/original', 'retired': True, 'active_claims': 0,
                  'output_lock_free': True, 'run_directories': ['completed', 'partial'],
                  'generated_ids': ['completed'],
                  'journal': [{'event': 'claimed', 'run_id': 'completed',
                               'job_sha256': sha(jobs[1]), 'config_sha256': sha(config)}]}]
    return jobs, config, ['/retained/original'], histories, ['held']


def storage_fixture():
    mount = {'mount_id': '48', 'device': '8:32', 'root': '/new',
             'target': '/campaign/new', 'fstype': 'ext4'}
    binding = {'schema': 'causes.prospective_storage_binding.v1',
               'backing': '/scratch/new', 'mount_target': '/campaign/new',
               'output': '/campaign/new/model', 'root_volume': '/campaign',
               'scratch_device': '2080', 'backing_inode': '100', 'output_inode': '101',
               'root_device': '2048',
               'floor_bytes': 80, 'mount_identity': mount}
    observation = {'mount': copy.deepcopy(mount), 'paths': {
        'backing': {'device': '2080', 'inode': '100'},
        'mount_target': {'device': '2080', 'inode': '100'},
        'output': {'device': '2080', 'inode': '101'},
        'root_volume': {'device': '2048', 'inode': '11'}},
        'output_free_bytes': 2000, 'root_free_bytes': 140}
    return binding, observation


def mixed_fixture():
    registration = {'schema': 'causes.storage_controller_phase.v1',
                    'phase': 'texas_running_mi_draining', 'owners': {}}
    observations = {}
    for index, role in enumerate(('old_mi', 'old_texas', 'new_texas'), 1):
        process = {'pid': index, 'process_starttime_ticks': str(index * 100),
                   'argv': ['python3', role]}
        owner = {'process': process, 'output_root': '/campaign/' + role,
                 'queue_identity': {'jobs_sha256': role}, 'controller_id': role}
        registration['owners'][role] = owner
        observations[role] = {'process': copy.deepcopy(process), 'output_root': owner['output_root'],
                              'queue_identity': owner['queue_identity'], 'live': True,
                              'lock_owner': index, 'active_claims': 0,
                              'barrier_present': False, 'claims': 0, 'persisted_blocks': 0}
    observations['old_texas'].update(live=False, lock_owner=None)
    observations['old_mi'].update(active_claims=1, journal=[
        {'controller_id': 'old_mi', 'event': 'claimed', 'run_id': 'long'},
        {'controller_id': 'old_mi', 'event': 'pause_seen', 'admission_stopped': True}])
    return registration, observations


class FixedPartitionTests(unittest.TestCase):
    def test_preserves_order_inputs_and_uncaptured_partial_attempt(self):
        fixture = partition_fixture()
        before = copy.deepcopy(fixture)
        result = partition_fixed_jobs(*fixture)
        self.assertEqual([j['allocation']['run_id'] for j in result['jobs']],
                         ['unstarted_b', 'unstarted_a'])
        self.assertEqual(result['partition']['prior_attempted_unresolved'], ['partial'])
        self.assertEqual(fixture, before)
        self.assertFalse(result['replay_authorized'])

    def test_missing_scope_live_owner_and_changed_claim_are_rejected(self):
        for mutation in ('scope', 'live', 'claim'):
            with self.subTest(mutation=mutation):
                jobs, config, roots, histories, held = partition_fixture()
                if mutation == 'scope':
                    roots.append('/retained/second')
                elif mutation == 'live':
                    histories[0]['active_claims'] = 1
                else:
                    histories[0]['journal'][0]['job_sha256'] = 'changed'
                with self.assertRaises(ValueError):
                    partition_fixed_jobs(jobs, config, roots, histories, held)

    def test_cross_root_attempt_and_hold_overlap_fail(self):
        jobs, config, roots, histories, held = partition_fixture()
        roots.append('/retained/second')
        duplicate = copy.deepcopy(histories[0])
        duplicate['root'] = roots[-1]
        with self.assertRaisesRegex(ValueError, 'multiple_roots'):
            partition_fixed_jobs(jobs, config, roots, histories + [duplicate], held)
        with self.assertRaisesRegex(ValueError, 'overlap'):
            partition_fixed_jobs(jobs, config, roots[:1], histories, ['partial'])

    def test_terminal_without_claim_or_directory_is_never_replayed(self):
        jobs, config, roots, histories, held = partition_fixture()
        histories[0]['journal'].append({'event': 'unresolved_prior_attempt',
                                        'run_id': 'unstarted_a'})
        result = partition_fixed_jobs(jobs, config, roots, histories, held)
        self.assertEqual(result['partition']['never_claimed'], ['unstarted_b'])
        self.assertIn('unstarted_a', result['partition']['prior_attempted_unresolved'])
        self.assertEqual(result['history_scopes'][0]['terminal_without_claim_or_directory'],
                         ['unstarted_a'])

    def test_additive_custody_registry_keeps_corpus_separate(self):
        roots = {'model': ['runs/original/model', 'runs/storage1/model']}
        rows = [{'run': 'runs/original/model/old', 'summary_sha256': 'one'},
                {'run': 'runs/storage1/model/new', 'summary_sha256': 'two'},
                {'run': 'runs/storage1/model/extra', 'summary_sha256': 'three'}]
        joined = fixed_custody_join(rows, {'model': ['old', 'new']}, roots)
        self.assertEqual(set(joined['model']), {'old', 'new'})
        self.assertEqual(joined['model']['old'], rows[0])

    def test_unregistered_or_duplicated_fixed_ID_fails(self):
        roots = {'model': ['runs/original/model', 'runs/storage1/model']}
        with self.assertRaisesRegex(ValueError, 'unregistered_root'):
            fixed_custody_join([{'run': 'runs/other/model/id'}], {'model': ['id']}, roots)
        with self.assertRaisesRegex(ValueError, 'multiple_custody'):
            fixed_custody_join([{'run': 'runs/original/model/id'},
                                {'run': 'runs/storage1/model/id'}], {'model': ['id']}, roots)


class DrainAndPhaseTests(unittest.TestCase):
    def test_texas_can_run_while_original_mi_finishes(self):
        registration, observations = mixed_fixture()
        result = validate_mixed_phase(registration, observations)
        self.assertEqual(result['roles']['old_mi'], 'draining')
        self.assertEqual(result['roles']['new_texas'], 'running')
        self.assertFalse(result['runtime_actions'])

    def test_no_mi_claim_after_pause(self):
        registration, observations = mixed_fixture()
        observations['old_mi']['journal'].append(
            {'controller_id': 'old_mi', 'event': 'claimed', 'run_id': 'late'})
        with self.assertRaisesRegex(ValueError, 'claim_after_pause'):
            validate_mixed_phase(registration, observations)

    def test_terminal_before_claim_cannot_hide_an_active_attempt(self):
        events = [{'controller_id': 'owner', 'event': 'episode_finished', 'run_id': 'id'},
                  {'controller_id': 'owner', 'event': 'claimed', 'run_id': 'id'},
                  {'controller_id': 'owner', 'event': 'pause_seen', 'admission_stopped': True}]
        with self.assertRaisesRegex(ValueError, 'claim_after_terminal'):
            drain_progress(events, 'owner')

    def test_waiting_successor_cannot_have_claims_or_blocks(self):
        for key in ('claims', 'persisted_blocks'):
            registration, observations = mixed_fixture()
            registration['phase'] = 'texas_waiting'
            observations['new_texas']['barrier_present'] = True
            observations['new_texas'][key] = 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'issued_work'):
                validate_mixed_phase(registration, observations)

    def test_wrong_process_or_lock_or_nonretired_texas_rejected(self):
        for mutation in ('pid', 'lock', 'retirement'):
            registration, observations = mixed_fixture()
            if mutation == 'pid':
                observations['old_mi']['process']['pid'] += 10
            elif mutation == 'lock':
                observations['new_texas']['lock_owner'] = 99
            else:
                observations['old_texas']['live'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_mixed_phase(registration, observations)

    def test_drain_completion_does_not_require_killing_active_work(self):
        _, observations = mixed_fixture()
        events = observations['old_mi']['journal']
        self.assertEqual(drain_progress(events, 'old_mi')['active_claims'], 1)
        events.append({'controller_id': 'old_mi', 'event': 'episode_finished', 'run_id': 'long'})
        self.assertEqual(drain_progress(events, 'old_mi')['active_claims'], 0)

    def test_texas_remains_registered_when_mi_finishes_before_successor_start(self):
        registration, observations = mixed_fixture()
        registration['phase'] = 'texas_running_mi_retired'
        observations['old_mi'].update(live=False, lock_owner=None, active_claims=0)
        self.assertEqual(validate_mixed_phase(registration, observations)['roles']['new_texas'],
                         'running')


class PauseRoutingTests(unittest.TestCase):
    def fixture(self):
        pin = {'path': '/campaign/PAUSE', 'bytes': 1, 'sha256': 'a' * 64}
        routing = {'schema': 'causes.storage_pause_routing.v1', 'root_accepted': True,
                   'transition_id': 'storage1', 'drain_marker': pin,
                   'successor_global_pause': '/campaign/controls/STOP_ALL',
                   'successor_local_pauses': {'mi': '/campaign/controls/mi/PAUSE',
                                              'texas': '/campaign/controls/texas/PAUSE'},
                   'user_pause_fanout': ['/campaign/PAUSE', '/campaign/controls/STOP_ALL',
                                        '/campaign/controls/mi/PAUSE', '/campaign/controls/texas/PAUSE']}
        marker = {'origin': 'storage_transition', 'transition_id': 'storage1', 'pin': pin}
        present = {p: p == '/campaign/PAUSE' for p in routing['user_pause_fanout']}
        return routing, marker, present

    def test_explicit_mixed_phase_control_coverage(self):
        result = validate_pause_routing(*self.fixture(), admitting_role='texas')
        self.assertTrue(result['old_owner_drain_marker_preserved'])

    def test_user_pause_and_unowned_marker_block(self):
        routing, marker, present = self.fixture()
        marker['origin'] = 'user'
        with self.assertRaisesRegex(ValueError, 'owned_transition_marker'):
            validate_pause_routing(routing, marker, present, admitting_role='mi')
        routing, marker, present = self.fixture()
        present[routing['successor_global_pause']] = True
        with self.assertRaisesRegex(ValueError, 'pause_blocks'):
            validate_pause_routing(routing, marker, present, admitting_role='texas')
        routing, marker, present = self.fixture()
        routing['user_pause_fanout'].pop()
        with self.assertRaisesRegex(ValueError, 'coverage'):
            validate_pause_routing(routing, marker, present, admitting_role='texas')

    def test_mi_local_pause_does_not_stop_texas(self):
        routing, marker, present = self.fixture()
        present[routing['successor_local_pauses']['mi']] = True
        validate_pause_routing(routing, marker, present, admitting_role='texas')
        with self.assertRaisesRegex(ValueError, 'pause_blocks'):
            validate_pause_routing(routing, marker, present, admitting_role='mi')


class StorageGuardTests(unittest.TestCase):
    def test_independent_root_reserve_and_scratch_floor_boundaries(self):
        binding, observation = storage_fixture()
        binding['root_floor_bytes'] = 20
        events = []
        guard = StorageGuard(binding, lambda _: copy.deepcopy(observation), events.append)
        for root, scratch, ready in [(76, 2000, True), (20, 80, True),
                                     (19, 2000, False), (76, 79, False)]:
            observation.update(root_free_bytes=root, output_free_bytes=scratch)
            with self.subTest(root=root, scratch=scratch):
                self.assertEqual(guard.check() >= binding['floor_bytes'], ready)
                self.assertEqual(events[-1]['state'], 'ready' if ready else 'floor_wait')
                self.assertEqual(events[-1]['observation'], observation)

    def test_independent_root_reserve_rejects_invalid_values(self):
        for value in (True, 0, -1, 81, '20', 20.0):
            binding, _ = storage_fixture()
            binding['root_floor_bytes'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'root_floor'):
                StorageGuard(binding)

    def test_both_volumes_guard_admission(self):
        binding, observation = storage_fixture()
        guard = StorageGuard(binding, lambda _: copy.deepcopy(observation))
        self.assertEqual(guard(), 140)
        observation['root_free_bytes'] = 79
        self.assertEqual(guard(), 79)
        observation['output_free_bytes'] = 50
        self.assertEqual(guard(), 50)

    def test_missing_replaced_or_wrong_backing_mount_fails_closed(self):
        for mutation in ('mount_id', 'backing', 'output_device', 'output_inode'):
            binding, observation = storage_fixture()
            guard = StorageGuard(binding, lambda _: copy.deepcopy(observation))
            self.assertEqual(guard(), 140)
            if mutation == 'mount_id':
                observation['mount']['mount_id'] = '49'
            elif mutation == 'backing':
                observation['paths']['mount_target']['inode'] = 'other'
            elif mutation == 'output_device':
                observation['paths']['output']['device'] = binding['root_device']
            else:
                observation['paths']['output']['inode'] = '102'
            with self.subTest(mutation=mutation):
                self.assertEqual(guard(), 0)
                self.assertIsNone(guard.last_observation)
                self.assertIsNotNone(guard.last_error)

    def test_observation_failure_keeps_admission_closed(self):
        binding, _ = storage_fixture()
        def failed(_):
            raise FileNotFoundError('fixture mount missing')
        guard = StorageGuard(binding, failed)
        self.assertEqual(guard(), 0)

    def test_device_identifiers_do_not_round_through_float(self):
        binding, observation = storage_fixture()
        exact = '17215639802098790451'
        binding['scratch_device'] = exact
        for name in ('backing', 'mount_target', 'output'):
            observation['paths'][name]['device'] = exact
        self.assertEqual(StorageGuard(binding, lambda _: observation)(), 140)
        binding['scratch_device'] = float(exact)
        with self.assertRaisesRegex(ValueError, 'exact_decimal'):
            StorageGuard(binding)

    def test_mountinfo_escaped_paths(self):
        raw = '48 24 8:32 /new /campaign/new\\040space rw - ext4 /dev/example rw\n'
        self.assertEqual(mount_rows(raw)[0]['target'], '/campaign/new space')

    def test_malformed_mountinfo_is_a_reportable_guard_fault(self):
        binding, _ = storage_fixture()
        events = []
        def malformed(_):
            return mount_rows('-')
        guard = StorageGuard(binding, malformed, events.append)
        self.assertEqual(guard(), 0)
        self.assertEqual(events[0]['state'], 'guard_fault')

    def test_launcher_retains_the_guard_and_refuses_floor_bypass(self):
        binding, observation = storage_fixture()
        guard = StorageGuard(binding, lambda _: observation)
        called = []
        def dispatcher(**kwargs):
            called.append(kwargs)
            return kwargs
        config = {'dispatcher': {'disk_floor_bytes': 80}}
        with patch('causes.campaign_storage.StorageGuard', return_value=guard):
            instance, actual_guard = guarded_dispatcher(dispatcher, storage_binding=binding,
                config=config, output=binding['output'], jobs=[])
            self.assertIs(instance['free_bytes'], actual_guard)
            with self.assertRaisesRegex(ValueError, 'cannot_be_overridden'):
                guarded_dispatcher(dispatcher, storage_binding=binding, config=config,
                    output=binding['output'], free_bytes=lambda: 99999)
            observation['root_free_bytes'] = 79
            with self.assertRaisesRegex(ValueError, 'floor_not_met'):
                guarded_dispatcher(dispatcher, storage_binding=binding, config=config,
                    output=binding['output'])
        self.assertEqual(len(called), 1)


class GuardReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.binding, self.observation = storage_fixture()
        directory = Path('/proc') / str(os.getpid())
        ticks = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        argv = [v.decode() for v in (directory / 'cmdline').read_bytes().split(b'\0') if v]
        self.owner = {'pid': os.getpid(), 'process_starttime_ticks': ticks, 'argv': argv}
        self.journal = GuardJournal(str(Path(self.temporary.name) / 'guard'),
                                    self.binding, self.owner)
        self.guard = StorageGuard(self.binding, lambda _: copy.deepcopy(self.observation),
                                  self.journal)

    def tearDown(self):
        self.journal.close()
        self.temporary.cleanup()

    def health(self):
        return observe_guard(self.journal.directory, self.binding, self.owner,
            self.journal.lock_identity, observe_storage=lambda _: copy.deepcopy(self.observation))

    def test_mount_failure_is_durable_visible_and_recovers(self):
        self.assertEqual(self.guard(), 140)
        self.assertTrue(self.health()['healthy'])
        self.observation['mount']['mount_id'] = 'replaced'
        self.assertEqual(self.guard(), 0)
        result = self.health()
        self.assertFalse(result['healthy'])
        self.assertTrue(result['alarms']['guard_not_ready'])
        self.assertTrue(result['alarms']['storage_binding_invalid'])
        record = json.loads((self.journal.directory / '00000001.json').read_text())
        self.assertEqual(record['state'], 'guard_fault')
        self.observation['mount']['mount_id'] = '48'
        self.assertEqual(self.guard(), 140)
        self.assertTrue(self.health()['healthy'])
        self.assertEqual(len(list(self.journal.directory.glob('*.json'))), 3)

    def test_fresh_observer_detects_mount_failure_without_a_new_admission_check(self):
        self.guard()
        self.observation['paths']['output']['inode'] = 'replaced'
        result = self.health()
        self.assertFalse(result['healthy'])
        self.assertTrue(result['alarms']['storage_binding_invalid'])

    def test_old_valid_ready_transition_does_not_need_artificial_heartbeat(self):
        with patch('causes.campaign_handoff.now', return_value='2001-01-01T00:00:00+00:00'):
            self.guard()
        self.assertTrue(self.health()['healthy'])
        self.guard()
        self.assertEqual(len(list(self.journal.directory.glob('*.json'))), 1)

    def test_journal_write_failure_unlocks_health_and_latches_admission_closed(self):
        self.guard()
        self.observation['root_free_bytes'] = 79
        with patch('causes.campaign_handoff.save_new', side_effect=OSError('fixture write failure')):
            with self.assertRaises(GuardReportingError):
                self.guard()
        self.observation['root_free_bytes'] = 140
        result = self.health()
        self.assertFalse(result['healthy'])
        self.assertTrue(result['alarms']['reporting_lock_invalid'])
        with self.assertRaisesRegex(GuardReportingError, 'previously_failed'):
            self.guard()

    def test_missing_or_malformed_guard_receipt_never_reports_healthy(self):
        self.assertTrue(self.health()['alarms']['guard_record_invalid'])
        self.guard()
        path = self.journal.directory / '00000000.json'
        path.write_text('{}')
        self.assertTrue(self.health()['alarms']['guard_record_invalid'])

    def test_floor_wait_and_recovery_are_recorded(self):
        self.guard()
        self.observation['root_free_bytes'] = 79
        self.assertEqual(self.guard(), 79)
        result = self.health()
        self.assertFalse(result['healthy'])
        self.assertTrue(result['alarms']['volume_below_floor'])
        self.observation['root_free_bytes'] = 140
        self.guard()
        self.assertTrue(self.health()['healthy'])

    def test_symlink_guard_directory_cannot_produce_healthy_status(self):
        self.guard()
        alias = Path(self.temporary.name) / 'alias'
        alias.symlink_to(self.journal.directory, target_is_directory=True)
        result = observe_guard(str(alias), self.binding, self.owner, self.journal.lock_identity,
            observe_storage=lambda _: copy.deepcopy(self.observation))
        self.assertFalse(result['healthy'])
        self.assertTrue(result['alarms']['reporting_lock_invalid'])
        self.assertTrue(result['alarms']['guard_record_invalid'])

    def test_loaded_source_identity_cannot_be_replaced_by_current_file_hash(self):
        self.guard()
        with patch('causes.campaign_storage._SOURCE_SHA', 'different_loaded_source'):
            self.assertTrue(self.health()['alarms']['guard_record_invalid'])
            with self.assertRaisesRegex(GuardReportingError, 'reporting_failed'):
                self.guard()
            with self.assertRaisesRegex(ValueError, 'loaded_storage_source_changed'):
                GuardJournal(str(Path(self.temporary.name) / 'another'), self.binding, self.owner)


if __name__ == '__main__':
    unittest.main()
