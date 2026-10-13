import copy
import datetime as dt
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_ordinal_actor_runtime as a


class OrdinalActorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.serial = 0
        self.instruments = ['evaluation_presence', 'evaluation_strongly_favors',
                            'evaluation_leans_towards_or_favors']
        self.global_jobs = [self.job(i) for i in range(10)]
        self.ordinals = list(range(0, 10, 2))
        self.host_jobs = self.global_jobs[::2]
        self.jobs = self.host_jobs[1:3]
        self.config = {'model': 'fixture', 'endpoint': 'http://127.0.0.1:1/v1/chat/completions',
                       'dispatcher': {'disk_floor_bytes': 80 * 1024**3}}
        modules = {'campaign_dispatcher': a.d, 'campaign_handoff': a.h,
                   'campaign_monitor': a.m, 'campaign_storage': a.s,
                   'campaign_storage_runtime': a.runtime, 'campaign_runner': a.runner,
                   'campaign_sandbox': a.sandbox, 'campaign_storage_observation': a.observation,
                   'campaign_telemetry': a.telemetry}
        config_ref = self.save(self.config)
        self.p = dict(schema='causes.ordinal_actor_plan.v1', runtime_authorized=True,
            automatic_retry=False, automatic_resume=False, role='left',
            execution_host='executor', execution_hostname=socket.gethostname(),
            provider_host='provider', provider_process={'pid': 1}, local_endpoint_process={'pid': 2},
            source=a.h.pin(a.__file__), native_sources={k: a.h.pin(v.__file__) for k, v in modules.items()},
            global_jobs=self.save(self.global_jobs), global_job_count=10,
            host_jobs=self.save(self.host_jobs), host_job_count=5,
            host_ordinals=self.save(self.ordinals), jobs=self.save(self.jobs), ordinal_range=[1, 3],
            instruments=self.instruments, config=config_ref, parent_config=config_ref,
            holds=self.save({'schema': 'caus.admission_holds.v1', 'holds': []}),
            workers=8, max_full=0, extension_prototypes=[],
            output=str(self.base / 'mount/output'), record_directory=str(self.base / 'owner'),
            storage_binding={'output': str(self.base / 'mount/output'),
                'mount_target': str(self.base / 'mount'), 'floor_bytes': 80 * 1024**3,
                'root_floor_bytes': 20 * 1024**3},
            pause_file=str(self.base / 'PAUSE'), global_pause_file=str(self.base / 'GLOBAL_PAUSE'),
            barrier_path=str(self.base / 'BARRIER'),
            pause_paths=[str(self.base / 'PAUSE'), str(self.base / 'GLOBAL_PAUSE'), str(self.base / 'REVOKED')],
            resource_binding={'workers_reserved': {'left': 8, 'right': 8}, 'cpus_per_worker': 1,
                'memory_bytes_per_worker': 4 * 1024**3, 'cpu_headroom': 4,
                'memory_headroom_bytes': 16 * 1024**3},
            predecessor_closures=[self.save({'closed': True})])
        self.p['collision_review'] = self.review('accepted_requested_actor_named_history_collision_census',
            jobs=self.p['global_jobs'], run_id_collisions=[], seed_collisions=[])
        self.parent_rows = [{'event': 'claimed', 'run_id': 'ancestor', 'job_sha256': 'old'},
                            {'event': 'episode_finished', 'run_id': 'ancestor', 'technical_failure': True},
                            {'event': 'route_failure', 'route': 'model:fixture', 'consecutive_failures': 1}]
        self.stage_rows = [self.claim(self.host_jobs[0]),
                          {'event': 'episode_finished', 'run_id': 'job0', 'technical_failure': True,
                           'probe_status': 'invalid_answer_schema'},
                          {'event': 'route_failure', 'route': 'model:fixture', 'consecutive_failures': 2}]
        self.parent_journal = self.raw(self.parent_rows)
        self.parent_history = self.history([self.parent_journal])
        self.p['route_carry_parent'] = self.save(self.parent_history)
        self.p['route_carry_parent_review'] = self.review('accepted_requested_actor_route_carry',
            actual=self.p['route_carry_parent'], config=config_ref, role='left')
        self.bind_history()

    def job(self, i):
        return {'spec': {'task_id': 'task' + str(i), 'cpus': 1, 'memory_bytes': 4 * 1024**3,
                         'pids_limit': 256}, 'variant': {'id': 'variant'},
                'allocation': {'run_id': 'job' + str(i), 'continue_to_end': False,
                               'episode_seed': i, 'instruments': self.instruments}}

    def save(self, value):
        self.serial += 1
        return a.h.save_new(self.base, str(self.serial) + '.json', value)

    def raw(self, rows, tail=b''):
        self.serial += 1
        path = self.base / (str(self.serial) + '.jsonl')
        path.write_bytes(b''.join(a.d.encoded(r) + b'\n' for r in rows) + tail)
        return a.h.pin(path)

    def review(self, flag, **joins):
        return self.save({flag: True, 'findings': [], **joins})

    def claim(self, job):
        return {'event': 'claimed', 'run_id': a.d.validate_job(job),
                'job_sha256': a.d.sha(job), 'config_sha256': a.d.sha(self.config)}

    def history(self, refs):
        replay = a.m.replay_history(b''.join(a.h.checked_bytes(r) for r in refs))
        return dict(journals=refs, combined_route_history=replay, automatic_reset=False,
                    failures=replay['nonzero_failures'], quarantines=replay['quarantines'],
                    config=self.p['config'], role='left',
                    native_replay_source=self.p['native_sources']['campaign_monitor'])

    def bind_history(self, tail=b'', mutate=None):
        history = self.history([self.parent_journal, self.raw(self.stage_rows, tail)])
        if mutate:
            mutate(history)
        self.p['route_carry'] = self.save(history)
        self.p['route_carry_review'] = self.review('accepted_requested_actor_route_carry',
            actual=self.p['route_carry'], config=self.p['config'], role='left')

    def load(self, *, refresh_qualification=True):
        keys = ('global_jobs', 'host_jobs', 'jobs', 'ordinal_range', 'config', 'workers',
                'route_carry', 'predecessor_closures', 'role', 'execution_host', 'provider_host',
                'provider_process', 'local_endpoint_process', 'native_sources')
        if refresh_qualification:
            self.p['qualification_review'] = self.review('accepted_ordinal_actor_qualification',
                                                         **{k: self.p[k] for k in keys})
        plan = self.save(self.p)
        peer = self.review('accepted_ordinal_actor_plan', plan=plan, source=self.p['source'])
        adoption = self.review('accepted_ordinal_actor_runtime_adoption', plan=plan,
            source=self.p['source'], plan_review=peer, no_replay=True, no_extra_draws=True)
        return a.load(plan, peer, adoption)

    def test_complete_load_retains_failed_terminal_and_nonzero_native_carry(self):
        p, jobs, config = self.load()
        self.assertEqual(jobs, self.jobs)
        self.assertEqual(config, self.config)
        self.assertEqual(p['_history']['combined_route_history']['lifetime_route_failures'], 2)
        self.assertEqual(p['_history']['failures'], {'model:fixture': 2})
        self.assertEqual(p['_options']['workers'], 8)
        self.assertEqual(p['_options']['extension_prototypes'], [])
        self.assertFalse((self.base / 'owner').exists())

    def test_qualification_cannot_survive_provider_forward_host_or_native_source_change(self):
        self.load()
        original = copy.deepcopy(self.p)
        for field in ('execution_host', 'provider_host', 'provider_process', 'local_endpoint_process'):
            self.p = copy.deepcopy(original)
            self.p[field] = {'pid': 99} if field.endswith('process') else 'different-host'
            with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError, 'unaccepted:accepted_ordinal_actor_qualification'):
                self.load(refresh_qualification=False)
        self.p = copy.deepcopy(original)
        peer = a.h.checked(self.p['qualification_review'])
        peer['native_sources']['campaign_runner']['sha256'] = 'wrong'
        self.p['qualification_review'] = self.save(peer)
        with self.assertRaisesRegex(ValueError, 'unaccepted:accepted_ordinal_actor_qualification'):
            self.load(refresh_qualification=False)

    def test_same_selector_accepts_next64_and_later_remaining_ranges(self):
        all_jobs = [self.job(i) for i in range(200)]
        host = all_jobs[::2]
        p = {**self.p, 'global_job_count': 200, 'host_job_count': 100}
        for bounds in ([8, 72], [72, 100]):
            with self.subTest(bounds=bounds):
                p['ordinal_range'] = bounds
                a.validate_ordinals(p, all_jobs, host, host[bounds[0]:bounds[1]], list(range(0, 200, 2)))

    def test_range_partition_and_json_distinct_job_changes_refuse(self):
        cases = [([True, 3], self.jobs, self.ordinals, 'integer_ordinal'),
                 ([0, 3], self.jobs, self.ordinals, 'continuation_range'),
                 ([1, 6], self.jobs, self.ordinals, 'continuation_range'),
                 ([1, 3], list(reversed(self.jobs)), self.ordinals, 'exact_ordinal'),
                 ([1, 3], self.jobs, [0, 2, 4, 6, 6], 'host_partition')]
        changed = copy.deepcopy(self.jobs)
        changed[0]['spec']['cpus'] = True
        cases.append(([1, 3], changed, self.ordinals, 'exact_ordinal'))
        for bounds, jobs, ordinals, reason in cases:
            with self.subTest(reason=reason, bounds=bounds), self.assertRaisesRegex(ValueError, reason):
                a.validate_ordinals({**self.p, 'ordinal_range': bounds}, self.global_jobs,
                                    self.host_jobs, jobs, ordinals)

    def test_claimed_selected_or_later_or_other_route_assignment_refuses_without_refill(self):
        original = copy.deepcopy(self.stage_rows)
        for job in (self.host_jobs[1], self.host_jobs[-1], self.global_jobs[1]):
            self.stage_rows = original + [self.claim(job), {'event': 'episode_finished',
                'run_id': job['allocation']['run_id'], 'technical_failure': True}]
            self.bind_history()
            with self.subTest(run_id=job['allocation']['run_id']), \
                 self.assertRaisesRegex(ValueError, 'claimed_assignment_or_prefix_gap'):
                self.load()

    def test_prefix_gap_and_unfinished_predecessor_refuse(self):
        self.stage_rows = [{'event': 'controller_finished'}]
        self.bind_history()
        with self.assertRaisesRegex(ValueError, 'claimed_assignment_or_prefix_gap'):
            self.load()
        self.stage_rows = [self.claim(self.host_jobs[0])]
        self.bind_history()
        with self.assertRaisesRegex(ValueError, 'unfinished_predecessor'):
            self.load()

    def test_previous_job_or_config_identity_change_refuses(self):
        for key in ('job_sha256', 'config_sha256'):
            with self.subTest(key=key):
                self.stage_rows[0] = self.claim(self.host_jobs[0])
                self.stage_rows[0][key] = 'changed'
                self.bind_history()
                with self.assertRaisesRegex(ValueError, 'predecessor_claim_identity'):
                    self.load()

    def test_duplicate_torn_unresolved_and_quarantined_histories_refuse(self):
        original = copy.deepcopy(self.stage_rows)
        cases = [(original + [self.claim(self.host_jobs[0])], b'', 'duplicate'),
                 (original, b'{', 'complete_history'),
                 (original + [{'event': 'unresolved_current_attempt', 'run_id': 'old'}], b'', 'unresolved'),
                 (original + [{'event': 'route_quarantined', 'route': 'model:fixture',
                               'quarantine_id': 'q'}], b'', 'quarantined')]
        for rows, tail, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.stage_rows = rows
                self.bind_history(tail)
                self.load()

    def test_route_prefix_counter_reset_and_bool_counter_metadata_refuse(self):
        mutations = [lambda v: v.update(journals=list(reversed(v['journals']))),
                     lambda v: v.update(automatic_reset=True),
                     lambda v: v.update(failures={}),
                     lambda v: v['combined_route_history'].update(unresolved_events=False)]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.bind_history(mutate=mutate)
                self.load()

    def test_unknown_route_and_native_counter_threshold_remain_blockers(self):
        for row, reason in [
            ({'event': 'route_failure', 'route': 'unknown', 'consecutive_failures': 1},
             'unrecognized_history_route'),
            ({'event': 'route_failure', 'route': 'model:fixture', 'consecutive_failures': 3},
             'unresolved_or_quarantined_history')]:
            with self.subTest(reason=reason):
                self.stage_rows.append(row)
                self.bind_history()
                with self.assertRaisesRegex(ValueError, reason):
                    self.load()
                self.stage_rows.pop()

    def test_real_carry_dispatcher_records_whole_history_and_obeys_all_pause_paths(self):
        p, jobs, config = self.load()
        cls = a.carry_dispatcher_class(p)
        owner = cls(jobs, config, p['output'], workers=8, max_full=0,
                    pause_file=p['pause_file'], global_pause_file=p['global_pause_file'])
        owner.meta.mkdir()
        owner.load_history()
        record = json.loads((owner.meta / 'PREDECESSOR_ROUTE_STATE.json').read_bytes())
        self.assertEqual(record['retained_route_history'], p['_history']['combined_route_history'])
        self.assertEqual(owner.failures, {'model:fixture': 2})
        self.assertFalse(owner.admission_paused())
        Path(p['pause_paths'][-1]).touch()
        self.assertTrue(owner.admission_paused())
        self.assertEqual(a.runtime._custom_dispatcher_binding(cls, p['source'])['source'], p['source'])
        self.assertFalse((owner.meta / 'journal.jsonl').exists())

    def test_existing_claims_or_even_empty_journal_prevent_carry_reinitialization(self):
        p, jobs, config = self.load()
        owner = a.carry_dispatcher_class(p)(jobs, config, p['output'], workers=8, max_full=0)
        owner.meta.mkdir()
        (owner.meta / 'journal.jsonl').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'fresh_owner_route_state'):
            owner.load_history()
        self.assertFalse((owner.meta / 'PREDECESSOR_ROUTE_STATE.json').exists())

    def test_joint_resource_caps_and_current_capacity_are_enforced(self):
        actual = {'cpu_affinity': list(range(20)), 'memory_available_bytes': 80 * 1024**3}
        a.validate_resources(self.p, self.jobs, actual)
        for mutation in (lambda v: v.update(cpu_affinity=list(range(19))),
                         lambda v: v.update(memory_available_bytes=80 * 1024**3 - 1),
                         lambda v: v.update(cpu_affinity=[0] * 20)):
            bad = copy.deepcopy(actual); mutation(bad)
            with self.assertRaises(ValueError):
                a.validate_resources(self.p, self.jobs, bad)
        jobs = copy.deepcopy(self.jobs); jobs[0]['spec']['memory_bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'sandbox_resource_budget'):
            a.validate_resources(self.p, jobs)
        bad = copy.deepcopy(self.p); bad['resource_binding']['workers_reserved']['right'] = True
        with self.assertRaisesRegex(ValueError, 'joint_worker'):
            a.validate_resources(bad, self.jobs)

    def admission(self, p, mutate=None):
        now = dt.datetime.now(dt.timezone.utc)
        value = dict(finished_at=now.isoformat(), errors=[], provider_idle=True,
            all_prior_actor_owners_retired=True, supplementary_active_count=0,
            supplementary_unstarted_preserved=True, model_calls=0,
            resource_workers_reserved=p['resource_binding']['workers_reserved'],
            execution_host=p['execution_host'], provider_host=p['provider_host'],
            provider_process=p['provider_process'], local_endpoint_process=p['local_endpoint_process'],
            cpu_affinity=list(range(20)), memory_available_bytes=80 * 1024**3)
        if mutate:
            mutate(value)
        ref = self.save(value)
        review = self.review('accepted_ordinal_actor_current_admission', actual=ref,
                             plan=p['_reference'], source=p['source'])
        return self.save(dict(root_authorized=True, no_user_revocation=True, action=a.runtime.START,
            issued_at=now.isoformat(), expires_at=(now+dt.timedelta(seconds=120)).isoformat(),
            plan=p['_reference'], plan_review=p['_review'], source=p['source'],
            root_adoption=p['_adoption'], admission=ref, admission_review=review))

    def test_authority_checks_fresh_provider_resources_and_native_process_identity(self):
        p, _, _ = self.load()
        with patch.object(a.h, 'process_identity') as identity:
            a.authority(p, self.admission(p)).check()
            identity.assert_called_once_with(p['local_endpoint_process'])
        for mutation in (lambda v: v.update(provider_process={'pid': 99}),
                         lambda v: v.update(provider_idle=False),
                         lambda v: v.update(memory_available_bytes=1),
                         lambda v: v.update(finished_at=(dt.datetime.now(dt.timezone.utc)
                                                            -dt.timedelta(seconds=181)).isoformat())):
            with self.subTest(mutation=mutation), patch.object(a.h, 'process_identity') as identity:
                with self.assertRaises(ValueError):
                    a.authority(p, self.admission(p, mutation)).check()
                identity.assert_not_called()

    def test_static_scope_source_and_control_widening_refuse(self):
        original = copy.deepcopy(self.p)
        mutations = [lambda p: p.update(runtime_authorized=False),
                     lambda p: p.update(max_full=1),
                     lambda p: p.update(extension_prototypes=[self.job(99)]),
                     lambda p: p.update(automatic_retry=True),
                     lambda p: p.update(pause_paths=[p['pause_file']]),
                     lambda p: p['native_sources'].pop('campaign_runner')]
        for mutation in mutations:
            self.p = copy.deepcopy(original); mutation(self.p)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.load()


if __name__ == '__main__':
    unittest.main()
