import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from causes import campaign_supplement as core
from causes import campaign_supplement_launch as native
from causes import campaign_supplement_co_residency as co
from causes.campaign_monitor import replay_history


class CoResidencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.at = '2026-01-01T00:01:00+00:00'
        process = lambda pid: {'pid': pid, 'process_starttime_ticks': str(pid * 10),
                               'argv': ['/python', 'fixed.py']}
        self.provider = process(30)
        self.plan = {'execution_host': 'cpu-host', 'provider_host': 'gpu-host',
                     'workers': 8, 'output': '/scratch/actor', 'provider_process': self.provider}
        binding = {'schema': 'causes.prospective_storage_binding.v1', 'output': '/scratch/actor', 'backing': '/backing', 'mount_target': '/scratch',
                   'root_volume': '/root-volume', 'scratch_device': '2', 'root_device': '1',
                   'output_inode': '5', 'backing_inode': '4', 'floor_bytes': 80*1024**3,
                   'root_floor_bytes': 20*1024**3,
                   'mount_identity': {'mount_id': '2', 'device': '0:2', 'root': '/',
                                      'target': '/scratch', 'fstype': 'ext4'}}
        self.plan['storage_binding'] = binding
        self.plan_ref = self.save('plan.json', self.plan)
        from causes import campaign_storage
        guard_source = core.pin(Path(campaign_storage.__file__).resolve())
        guard_binding = {'binding': binding, 'directory': '/controls/guard', 'reporter_source': guard_source,
                         'reporting_lock': {'st_dev': 1, 'st_ino': 9}}
        guard = {'healthy': True, 'at': self.at, 'reporter_source': guard_source,
                 'alarms': {key: False for key in ('owner_identity_invalid', 'reporting_lock_invalid',
                    'guard_record_invalid', 'guard_not_ready', 'storage_binding_invalid', 'volume_below_floor')},
                 'latest_transition': self.plan_ref,
                 'fresh_storage': {'mount': binding['mount_identity'],
                     'paths': {'backing': {'device': '2', 'inode': '4'},
                               'mount_target': {'device': '2', 'inode': '4'},
                               'output': {'device': '2', 'inode': '5'},
                               'root_volume': {'device': '1', 'inode': '1'}},
                     'output_free_bytes': 100*1024**3, 'root_free_bytes': 30*1024**3}}
        other = dict(self.plan, provider_host='other-gpu', output='/scratch/other-actor')
        self.other_plan_ref = self.save('other_plan.json', other)
        self.baseline = self.root/'baseline.jsonl'
        self.baseline.write_text('{"event":"controller_started"}\n')
        identity = {'execution_host': 'cpu-host', 'output': '/scratch/actor', 'process': process(10)}
        scope = {'id': 'actor', 'identity': identity, 'baseline': core.pin(self.baseline),
                 'mode': 'live_prefix', 'model_aliases': ['model:route'],
                 'accepted_state': {k: replay_history(self.baseline.read_bytes())[k] for k in native.HISTORY_FIELDS}}
        self.policy = {'max_clients': 1, 'runtime_identity': {'host': 'gpu-host', 'process': self.provider},
                       'remote_pause_paths': ['/scratch/actor/PAUSE'], 'donors': [scope],
                       'initial_failures': 0}
        actor = {'id': 'actor', 'plan': self.plan_ref, **self.plan, 'process': process(10),
                 'guard_binding': guard_binding, 'lock': {'path': '/scratch/actor/owner.lock', 'device': 2, 'inode': 5},
                 'journal_scope_id': 'actor', 'journal_identity': identity}
        actor.pop('provider_process'); actor.pop('storage_binding')
        inventory = [{'id': 'actor', 'state': 'active_actor', **identity}]
        self.contract = {'schema': co.SCHEMA, 'supplement_clients': 1,
                         'provider_identity': self.policy['runtime_identity'], 'provider_process': self.provider,
                         'provider_host': 'gpu-host', 'server_max_sequences': 16,
                         'maximum_observation_age_seconds': 30, 'baseline_preemptions': 0,
                         'actors': [actor], 'owner_inventory': inventory, 'required_pins': [self.plan_ref, self.other_plan_ref, guard_source],
                         'execution_resources': [{'host': 'cpu-host', 'reserved_actor_workers': 16,
                             'actor_plans': [self.plan_ref, self.other_plan_ref],
                             'cpu_per_worker': 1, 'cpu_headroom': 4, 'memory_per_worker_bytes': 4*1024**3,
                             'memory_headroom_bytes': 16*1024**3}],
                         'client_resources': {'host': 'client-host', 'minimum_free_cpu_count': 1,
                                              'minimum_available_memory_bytes': 2*1024**3}}
        self.observation = {'schema': 'causes.supplement_admission_observation.v1',
                            'observed_from': self.at, 'observed_until': self.at,
                            'identity': self.policy['runtime_identity'],
                            'checks': {key: True for key in native.CHECKS},
                            'controls': {'/scratch/actor/PAUSE': False},
                            'resources': {'local_free_bytes': 100*1024**3, 'remote_root_free_bytes': 30*1024**3,
                                          'remote_scratch_free_bytes': 100*1024**3},
                            'evidence': [self.plan_ref],
                            'donors': [{'id': 'actor', 'identity': identity, 'capture': core.pin(self.baseline)}],
                            'co_residency': {'schema': co.OBSERVATION_SCHEMA,
                                'observed_from': self.at, 'observed_until': self.at,
                                'checks': {key: True for key in co.CHECKS}, 'evidence': [self.plan_ref],
                                'owner_inventory': inventory,
                                'provider': {'identity': self.policy['runtime_identity'], 'process': self.provider,
                                    'host': 'gpu-host', 'server_max_sequences': 16,
                                    'running_requests': 8, 'waiting_requests': 0, 'preemptions': 0},
                                'actors': [{'id': 'actor', 'identity': actor, 'observed_host': 'cpu-host',
                                    'process_state': 'S', 'lock_held': True, 'guard': guard, 'active': 8}],
                                'execution_resources': [{'host': 'cpu-host', 'cpu_count': 20,
                                                         'available_memory_bytes': 80*1024**3}],
                                'client_resources': {'host': 'client-host', 'free_cpu_count': 2.5,
                                                     'available_memory_bytes': 4*1024**3}}}

    def save(self, name, value):
        path = self.root/name; core.save(path, value); return core.pin(path)

    def check(self, observation=None, contract=None, policy=None):
        return co.qualify_observation(observation or self.observation, policy or self.policy,
                                      contract or self.contract, replay_history, at=self.at)

    def test_valid_sharing_returns_same_complete_observation_and_preserves_inputs(self):
        before = copy.deepcopy((self.observation, self.policy, self.contract))
        self.assertIs(self.check(), self.observation)
        self.assertEqual(before, (self.observation, self.policy, self.contract))

    def test_no_implicit_two_client_promotion_or_boolean_cap(self):
        for clients in [2, True, 1.0]:
            c = copy.deepcopy(self.contract); p = copy.deepcopy(self.policy)
            c['supplement_clients'] = p['max_clients'] = clients
            with self.subTest(clients=clients), self.assertRaises(core.IntegrityError):
                self.check(contract=c, policy=p)

    def test_plan_worker_and_provider_pin_drift_refuse(self):
        for key, value in [('workers', 8.0), ('provider_host', 'other-host'),
                           ('provider_process', {**self.provider, 'pid': 31})]:
            old = self.plan_ref['path']; Path(old).unlink()
            changed = dict(self.plan, **{key: value}); ref = self.save('plan.json', changed)
            c = copy.deepcopy(self.contract); c['required_pins'] = [ref]; c['actors'][0]['plan'] = ref
            with self.subTest(key=key), self.assertRaises(core.IntegrityError): self.check(contract=c)

    def test_modified_pinned_plan_is_rejected_before_use(self):
        Path(self.plan_ref['path']).write_text('{}')
        with self.assertRaises(core.IntegrityError): self.check()

    def test_wrong_execution_host_never_infers_local_process_absence(self):
        value = copy.deepcopy(self.observation); value['co_residency']['actors'][0]['observed_host'] = 'gpu-host'
        with self.assertRaisesRegex(core.IntegrityError, 'wrong_host'): self.check(value)

    def test_pid_start_argv_and_lock_identity_changes_refuse(self):
        for key, value in [('pid', 11), ('process_starttime_ticks', '101'), ('argv', ['/other'])]:
            obs = copy.deepcopy(self.observation)
            obs['co_residency']['actors'][0]['identity']['process'][key] = value
            with self.subTest(key=key), self.assertRaises(core.IntegrityError): self.check(obs)
        obs = copy.deepcopy(self.observation); obs['co_residency']['actors'][0]['identity']['lock']['inode'] = 6
        with self.assertRaises(core.IntegrityError): self.check(obs)

    def test_missing_unknown_or_unhealthy_owner_refuses(self):
        for key, value in [('lock_held', False), ('process_state', 'Z'), ('process_state', 'T'), ('active', 9)]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['actors'][0][key] = value
            with self.subTest(key=key), self.assertRaises(core.IntegrityError): self.check(obs)
        for rows in [[], [*self.contract['owner_inventory'], {'id': 'unknown'}]]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['owner_inventory'] = rows
            with self.assertRaises(core.IntegrityError): self.check(obs)

    def test_provider_restart_and_sequence_cap_drift_refuse(self):
        for key, value in [('process', {**self.provider, 'pid': 31}), ('host', 'other'),
                           ('server_max_sequences', 16.0)]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['provider'][key] = value
            with self.subTest(key=key), self.assertRaises(core.IntegrityError): self.check(obs)
        c = copy.deepcopy(self.contract); c['server_max_sequences'] = 8
        with self.assertRaisesRegex(core.IntegrityError, 'reservation_exceeded'): self.check(contract=c)

    def test_waiting_preemptions_and_foreign_request_load_hold_new_claims(self):
        for key, value in [('waiting_requests', 1), ('preemptions', 1), ('running_requests', 10)]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['provider'][key] = value
            with self.subTest(key=key), self.assertRaises(core.AdmissionHold): self.check(obs)

    def test_shared_execution_reservations_include_other_provider_workers(self):
        for key, value in [('cpu_count', 19), ('available_memory_bytes', 80*1024**3-1)]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['execution_resources'][0][key] = value
            with self.subTest(key=key), self.assertRaises(core.AdmissionHold): self.check(obs)
        c = copy.deepcopy(self.contract); c['execution_resources'][0]['reserved_actor_workers'] = 7
        with self.assertRaises(core.IntegrityError): self.check(contract=c)

    def test_client_resource_floors_and_distinct_host(self):
        for key, value in [('free_cpu_count', .5), ('available_memory_bytes', 1)]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['client_resources'][key] = value
            with self.subTest(key=key), self.assertRaises(core.AdmissionHold): self.check(obs)
        for value in [True, float('nan'), float('inf')]:
            obs = copy.deepcopy(self.observation); obs['co_residency']['client_resources']['free_cpu_count'] = value
            with self.assertRaises(core.IntegrityError): self.check(obs)
        c = copy.deepcopy(self.contract); c['client_resources']['host'] = 'cpu-host'
        with self.assertRaises(core.IntegrityError): self.check(contract=c)

    def test_stale_or_disconnected_live_evidence_refuses(self):
        obs = copy.deepcopy(self.observation); obs['co_residency']['actors'][0]['guard']['at'] = '2026-01-01T00:00:00+00:00'
        with self.assertRaises(core.AdmissionHold): self.check(obs)
        obs = copy.deepcopy(self.observation); obs['co_residency']['observed_from'] = '2026-01-01T00:00:59+00:00'
        with self.assertRaises(core.IntegrityError): self.check(obs)
        obs = copy.deepcopy(self.observation); obs['co_residency']['evidence'] = []
        with self.assertRaises(core.IntegrityError): self.check(obs)

    def test_native_pause_storage_and_failure_history_remain_enforced(self):
        obs = copy.deepcopy(self.observation); obs['controls']['/scratch/actor/PAUSE'] = True
        with self.assertRaises(core.IntegrityError): self.check(obs)
        obs = copy.deepcopy(self.observation); obs['resources']['remote_root_free_bytes'] = 1
        with self.assertRaises(core.AdmissionHold): self.check(obs)
        path = self.root/'later.jsonl'
        path.write_bytes(self.baseline.read_bytes() + b'{"event":"route_failure","route":"model:route","consecutive_failures":1}\n')
        obs = copy.deepcopy(self.observation); obs['donors'][0]['capture'] = core.pin(path)
        with self.assertRaises(core.AdmissionHold): self.check(obs)
        path.write_bytes(path.read_bytes() + b'{"event":"route_success","routes":["model:route"]}\n')
        obs['donors'][0]['capture'] = core.pin(path)
        with self.assertRaises(core.AdmissionHold): self.check(obs)

    def test_actor_fault_cannot_be_scoped_away_by_lifetime_progression(self):
        policy = copy.deepcopy(self.policy); policy['donors'][0]['history_progression'] = 'append_only_lifetime_v1'
        with self.assertRaises(core.IntegrityError): self.check(policy=policy)
        contract = copy.deepcopy(self.contract); contract['actors'][0]['journal_identity']['output'] = '/other'
        with self.assertRaises(core.IntegrityError): self.check(contract=contract)


    def test_native_guard_storage_replay_and_alarm_failures(self):
        for mutate in [lambda g: g.update(healthy=False),
                       lambda g: g['alarms'].update(guard_record_invalid=True),
                       lambda g: g['fresh_storage']['paths']['output'].update(inode='99'),
                       lambda g: g['fresh_storage'].update(output_free_bytes=1)]:
            obs = copy.deepcopy(self.observation); mutate(obs['co_residency']['actors'][0]['guard'])
            with self.assertRaises((core.IntegrityError, core.AdmissionHold)): self.check(obs)

    def test_real_native_queue_holds_second_claim_after_priority_change(self):
        output = self.root/'queue'; core.mkdir(output/'.control')
        jobs = [{'family': 'finite', 'model': 'm', 'run_id': 'run'+str(i), 'stage': 'early',
                 'question_id': 'q', 'measurement_id': 'finite/m/run'+str(i)+'/early/q'} for i in range(2)]
        calls = []
        def worker(job, destination):
            calls.append(job['measurement_id'])
            self.observation['co_residency']['provider']['waiting_requests'] = 1
            return {'technical_status': 'complete'}
        with mock.patch.object(core.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=100*1024**3)):
            result = core.run_queue(output, jobs, worker, self.check, lambda job: None, [output/'PAUSE'],
                                    family='finite', model='m', max_clients=1)
        self.assertEqual(calls, [jobs[0]['measurement_id']])
        self.assertEqual(result['new_final'], 1); self.assertEqual(result['remaining_unclaimed'], 1)
        self.assertEqual(result['active'], 0)
        self.assertIn('co_resident_actor_priority_metrics', result['stop_reason'])
        self.assertFalse(core.measurement_path(output, jobs[1], family='finite', model='m').exists())
        final = core.load(core.measurement_path(output, jobs[0], family='finite', model='m')/'FINAL.json')
        self.assertIs(final['retry'], False)


if __name__ == '__main__': unittest.main()
