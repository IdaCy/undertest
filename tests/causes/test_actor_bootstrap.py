import copy
import ast
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/campaign_actor_bootstrap.py'
SPEC = importlib.util.spec_from_file_location('actor_bootstrap_test_source', SOURCE)
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


class ActorBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = [dict(spec={'task_id': f'task-{index}'}, allocation={
            'run_id': f'run-{index}', 'continue_to_end': False,
            'block_seed': 100, 'model_seed': 101, 'task_seed': 200 + index,
            'seed': 300 + index}) for index in range(8)]
        self.proposal = dict(workers=4, max_full=0, spares_enabled=False,
                             prefix_count=8, maximum_invocations=1,
                             outside_fixed_study=True, replacements=False,
                             proposed_native_actor_sources=['frozen-package'],
                             jobs={'path': 'frozen-jobs'}, config={'path': 'frozen-config'},
                             control_root=str(self.root/'control'), output_root=str(self.root/'runs/model'))
        self.plan = dict(schema='causes.finite_actor_bootstrap_runtime.v1',
                         workers=4, max_full=0, spares_enabled=False,
                         native_sources=['frozen-package'], automatic_retry=False,
                         global_pause=str(self.root/'global-pause'),
                         **{key:self.proposal[key] for key in ('jobs', 'config', 'control_root', 'output_root')})
        self.plan['pause_paths'] = [self.plan['global_pause'], str(self.root/'control/PAUSE'),
                                    str(self.root/'control/USER_PAUSE')]
        self.config = {'dispatcher': {'disk_floor_bytes': 112*1024**3}}

    def scope(self):
        return b.validate_fixed_scope(self.plan, self.proposal, self.jobs, self.config)

    def test_exact_prefix_scope(self):
        self.assertEqual(self.scope(), [f'run-{index}' for index in range(8)])

    def test_no_extra_job_full_trajectory_worker_or_replacement(self):
        changes = (
            lambda: self.jobs.append(copy.deepcopy(self.jobs[0])),
            lambda: self.jobs[0]['allocation'].update(continue_to_end=True),
            lambda: self.plan.update(workers=8),
            lambda: self.plan.update(max_full=1),
            lambda: self.plan.update(automatic_retry=True),
            lambda: self.proposal.update(replacements=True),
            lambda: self.jobs[1]['spec'].update(task_id='task-0'),
        )
        original = copy.deepcopy((self.plan, self.proposal, self.jobs))
        for change in changes:
            with self.subTest(change=change):
                self.plan, self.proposal, self.jobs = copy.deepcopy(original)
                change()
                with self.assertRaises(ValueError):
                    self.scope()

    def test_source_pause_and_floor_changes_refuse(self):
        self.plan['native_sources'] = ['different-package']
        with self.assertRaisesRegex(ValueError, 'source_or_retry'):
            self.scope()
        self.plan['native_sources'] = ['frozen-package']
        self.plan['pause_paths'][0] = str(self.root/'historical-pause')
        with self.assertRaisesRegex(ValueError, 'pause_or_namespace'):
            self.scope()
        self.plan['pause_paths'][0] = self.plan['global_pause']
        self.config['dispatcher']['disk_floor_bytes'] = 80*1024**3
        with self.assertRaisesRegex(ValueError, 'frozen_eight_prefixes'):
            self.scope()

    def admission(self, action='start_finite_actor_held_owner'):
        self.plan.update(_reference={'plan': 'exact'}, provider={'pid': 123, 'start': '4'}, host='current-host',
                         boot_id='boot', current_owner_exclusion={'evidence': 'closed'})
        self.plan['provider_binding'] = {'process': copy.deepcopy(self.plan['provider']), 'unit': 'fixture.service'}
        seeds = sorted({job['allocation'][key] for job in self.jobs
                        for key in ('block_seed', 'model_seed', 'task_seed', 'seed')})
        reservation = dict(external_run_ids=[], external_allocation_seeds=[],
                           excluded_owned_output=self.plan['output_root'] if action.startswith('release_') else None)
        actual = dict(schema='causes.finite_actor_bootstrap_admission.v1',
                      at=dt.datetime.now(dt.timezone.utc).isoformat(), plan=self.plan['_reference'],
                      action=action, provider=self.plan['provider'], provider_binding=self.plan['provider_binding'], boot_id='boot',
                      current_owner_exclusion=self.plan['current_owner_exclusion'],
                      source_assets_sandbox_current=True, bounded_resource_admission=True,
                      reservation='reservation', reservation_complete=True, census_pins=['capture'],
                      frozen_run_ids=sorted(job['allocation']['run_id'] for job in self.jobs),
                      frozen_allocation_seeds=seeds)
        values = {'actual': actual, 'reservation': reservation, 'closed': {'owners': [], 'external_self_owners': []}}
        reader = lambda ref: self.jobs if ref == self.plan['jobs'] else values[ref]
        self.mock_h = types.SimpleNamespace(checked=reader, checked_bytes=lambda ref:b'pinned')
        return actual, reservation, {'evidence': 'actual', 'review': 'accepted'}, action

    def check_admission(self, pair, action):
        with patch.object(b, 'h', self.mock_h), patch.object(b, 'peer'):
            return b.current_admission(self.plan, pair, action)

    def test_all_four_seed_families_excluded_from_external_reservation(self):
        actual, reservation, pair, action = self.admission()
        self.assertEqual(len(actual['frozen_allocation_seeds']), 18)
        self.check_admission(pair, action)
        for key in ('block_seed', 'model_seed', 'task_seed', 'seed'):
            with self.subTest(key=key):
                reservation['external_allocation_seeds'] = [self.jobs[0]['allocation'][key]]
                with self.assertRaisesRegex(ValueError, 'id_or_seed_collision'):
                    self.check_admission(pair, action)

    def test_external_id_collision_and_forged_seed_projection_refuse(self):
        actual, reservation, pair, action = self.admission()
        reservation['external_run_ids'] = ['run-4']
        with self.assertRaisesRegex(ValueError, 'id_or_seed_collision'):
            self.check_admission(pair, action)
        reservation['external_run_ids'] = []
        actual['frozen_allocation_seeds'] = []
        with self.assertRaisesRegex(ValueError, 'id_or_seed_collision'):
            self.check_admission(pair, action)

    def test_release_reservation_exclusion_is_only_exact_held_output(self):
        actual, reservation, pair, action = self.admission('release_finite_actor_held_owner')
        self.check_admission(pair, action)
        reservation['excluded_owned_output'] = str(self.root/'another-owner')
        with self.assertRaisesRegex(ValueError, 'id_or_seed_collision'):
            self.check_admission(pair, action)

    def test_expired_provider_changed_or_incomplete_admission_refuses(self):
        actual, reservation, pair, action = self.admission()
        original = copy.deepcopy(actual)
        changes = [dict(at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=601)).isoformat()),
                   dict(provider={'pid': 123, 'start': 'new'}), dict(provider_binding={'process': self.plan['provider'], 'unit': 'changed.service'}), dict(reservation_complete=False),
                   dict(bounded_resource_admission=False), dict(source_assets_sandbox_current=False)]
        for change in changes:
            with self.subTest(change=change):
                actual.clear(); actual.update(original); actual.update(change)
                with self.assertRaises(ValueError):
                    self.check_admission(pair, action)

    def test_loader_binds_all_six_modules_in_fresh_namespace(self):
        package = self.root/'frozen/causes'; package.mkdir(parents=True)
        refs = []
        for name in ('__init__', 'campaign_dispatcher', 'campaign_handoff', 'campaign_runner',
                     'campaign_sandbox', 'campaign_telemetry'):
            path = package/(name+'.py'); path.write_text('PINNED = '+repr(name)+'\n')
            raw = path.read_bytes()
            refs.append(dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        request = self.root/'sources.json'; request.write_text(json.dumps(refs))
        code = 'import runpy,json,sys; m=runpy.run_path(sys.argv[1]); print(sorted(m["native_modules"](json.load(open(sys.argv[2])))))'
        result = subprocess.run([sys.executable, '-B', '-c', code, str(SOURCE), str(request)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('campaign_runner', result.stdout)
        (package/'campaign_runner.py').write_text('PINNED="changed"\n')
        refused = subprocess.run([sys.executable, '-B', '-c', code, str(SOURCE), str(request)],
                                 capture_output=True, text=True, timeout=20)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('bootstrap_pin_changed', refused.stderr)

    def test_guard_preserves_claim_freshness_and_failed_state(self):
        guard = object.__new__(b.Guard)
        guard.p = self.plan
        guard.failed = False
        guard.dispatcher = types.SimpleNamespace(quarantines={})
        guard.last_good = 100
        with patch.object(b, 'PAUSES', ()), patch.object(b, 'resources', return_value={'root_free_bytes':123}), \
                patch.object(b, 'pause') as pause, patch.object(b.time, 'monotonic', return_value=144):
            self.assertEqual(guard.available(), 123)
            pause.assert_not_called()
        with patch.object(b, 'PAUSES', ()), patch.object(b, 'pause') as pause, \
                patch.object(b.time, 'monotonic', return_value=145):
            self.assertEqual(guard.available(), 0)
            self.assertTrue(guard.failed)
            pause.assert_called_once()
        guard.last_good = 145
        with patch.object(b, 'PAUSES', ()), patch.object(b, 'pause'), \
                patch.object(b.time, 'monotonic', return_value=146):
            self.assertEqual(guard.available(), 0)

    def test_full_provider_process_mismatch_refuses_before_any_probe(self):
        raw = {'pid': 123, 'process_starttime_ticks': '456', 'argv': ['python', 'server.py']}
        plan = {'provider': raw, 'provider_binding': {'process': copy.deepcopy(raw),
                'unit': 'fixture.service', 'gpu_uuid': 'fixture-gpu'}}
        b.validate_provider_binding(plan)
        for key, changed in (('pid', 124), ('process_starttime_ticks', '457'),
                             ('argv', ['python', 'other-server.py'])):
            with self.subTest(key=key):
                candidate = copy.deepcopy(plan)
                candidate['provider_binding']['process'][key] = changed
                mocked = types.SimpleNamespace(process_identity=unittest.mock.Mock())
                with patch.object(b, 'h', mocked), patch.object(Path, 'read_text') as read:
                    with self.assertRaisesRegex(ValueError, 'provider_process_binding_changed'):
                        b.resources(candidate)
                    with self.assertRaisesRegex(ValueError, 'provider_process_binding_changed'):
                        b.current_admission(candidate, {}, 'start_finite_actor_held_owner')
                mocked.process_identity.assert_not_called()
                read.assert_not_called()

    def test_provider_reader_is_bound_to_actor_endpoint_and_environment(self):
        raw = b'def http(suffix):\n    url = "http://127.0.0.1:18083" + suffix\n    return url\n'
        plan = dict(provider_read_base_url='http://127.0.0.1:18083',
                    provider_read_environment={'PATH': '/usr/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        config = {'endpoint': 'http://127.0.0.1:18083/v1/chat/completions'}
        b.validate_provider_reader(plan, config, raw)
        with self.assertRaisesRegex(ValueError, 'endpoint_changed'):
            b.validate_provider_reader(plan, {'endpoint': 'http://127.0.0.1:18084/v1/chat/completions'}, raw)
        with self.assertRaisesRegex(ValueError, 'http_base_changed'):
            b.validate_provider_reader(plan, config, raw.replace(b'18083', b'18084'))
        plan['provider_read_environment']['HTTP_PROXY'] = 'http://another-service'
        with self.assertRaisesRegex(ValueError, 'environment_changed'):
            b.validate_provider_reader(plan, config, raw)

    def test_archived_owner_or_closure_cannot_be_checked_on_current_host(self):
        plan = dict(host='current-host', boot_id='boot', provider={'pid': 123},
                    current_owner_exclusion={'evidence': 'exclusion'})
        plan['provider_binding'] = {'process': copy.deepcopy(plan['provider'])}
        row = dict(host='current-host', id='prior-self', kind='self',
                   process={'pid': 456}, output_root='/prior/output',
                   lock_paths=['/prior/output/.lock'], closure={'evidence': 'closed'})
        closed = {**row, 'owner_absent': True, 'all_locks_free': True,
                  'active_claims': 0, 'attempts_preserved': True,
                  'dispositions_preserved': True}
        b.validate_current_owner(plan, row, closed)
        for row_host, closed_host in (('archived-host', 'current-host'),
                                      ('current-host', 'archived-host'),
                                      ('archived-host', 'archived-host')):
            with self.subTest(row_host=row_host, closed_host=closed_host):
                row['host'], closed['host'] = row_host, closed_host
                values = {'exclusion': {'owners': [row]}, 'closed': closed}
                mocked = types.SimpleNamespace(checked=values.__getitem__, checked_bytes=lambda ref:b'pinned',
                    process_identity=lambda provider: provider,
                    retirement_state=unittest.mock.Mock(), lock_free=unittest.mock.Mock())
                with patch.object(b, 'h', mocked), patch.object(Path, 'read_text', return_value='boot'):
                    with self.assertRaisesRegex(ValueError, 'prior_owner_not_closed|local_partition_changed'):
                        b.resources(plan)
                mocked.retirement_state.assert_not_called()
                mocked.lock_free.assert_not_called()

    def test_external_self_contract_is_identical_and_never_probed_locally(self):
        from causes import campaign_corpus_succession as succession, campaign_handoff as handoff
        from tests.causes.test_native_stack_transition import TransitionFixture
        fixture = TransitionFixture(self)
        policy, registration = fixture.transition()
        provider_binding = {'process': {'pid': 123, 'process_starttime_ticks': '456', 'argv': ['fixture']},
                            'unit': 'fixture.service', 'gpu_uuid': 'fixture-gpu'}
        policy['provider'] = provider_binding
        value = handoff.checked(policy['stack_transition']['evidence'])
        stack = fixture.mutate(value['actor_stack'],
            lambda value: value['bindings'].update(provider=provider_binding))
        exclusion = fixture.mutate(value['current_exclusion'],
            lambda value: value.update(actor_stack=stack['evidence']))
        policy['stack_transition'] = fixture.mutate(policy['stack_transition'],
            lambda value: value.update(actor_stack=stack, current_exclusion=exclusion))
        external = fixture.external_self_transition(policy,registration)
        transition = succession.native_stack_transition(policy)
        for module in (b,succession):
            tree = ast.parse(Path(module.__file__).read_text())
            helper = next(node for node in tree.body if isinstance(node,ast.FunctionDef)
                          and node.name=='external_self_custody')
            if module is b: expected = ast.dump(helper,include_attributes=False)
            else: self.assertEqual(ast.dump(helper,include_attributes=False),expected)
        plan = dict(host='fixture', boot_id='boot', provider=policy['provider']['process'],
                    provider_binding=policy['provider'],
                    current_owner_exclusion=transition['value']['current_exclusion'])
        def read_proc(path, *args, **kwargs):
            if str(path)=='/proc/sys/kernel/random/boot_id': return 'boot'
            if str(path)=='/proc/meminfo': return 'MemAvailable: 104857600 kB\nMemTotal: 104857600 kB\n'
            raise AssertionError('unexpected local text read: '+str(path))
        with patch.object(b,'h',handoff), patch.object(Path,'read_text',read_proc), \
                patch.object(handoff,'process_identity',return_value=policy['provider']['process']) as provider_probe, \
                patch.object(handoff,'retirement_state',return_value={'absent':True}) as state, \
                patch.object(handoff,'lock_free',return_value=True) as locks, \
                patch.object(b.os,'sched_getaffinity',return_value=set(range(16))), \
                patch.object(b.shutil,'disk_usage',return_value=types.SimpleNamespace(free=200*1024**3)):
            b.resources(plan)
        provider_probe.assert_called_once_with(policy['provider']['process'])
        self.assertEqual([call.args[0] for call in state.call_args_list],
                         [row['process'] for row in registration['prior_local_owners']])
        self.assertTrue(set(call.args[0] for call in locks.call_args_list).isdisjoint(map(Path,external['lock_paths'])))

    def test_fresh_action_admission_must_join_external_executor_exclusion(self):
        actual, reservation, pair, action = self.admission()
        rows = [{'host':'execution-host','closure':'exact accepted closure'}]
        with patch.object(b,'external_self_custody',return_value={'owners':rows,'pins':[]}):
            actual.update(external_self_owners=rows, external_execution_exclusions_current=True)
            self.check_admission(pair,action)
            actual['external_execution_exclusions_current'] = False
            with self.assertRaisesRegex(ValueError,'fresh_external_self_exclusion'):
                self.check_admission(pair,action)
            actual.update(external_execution_exclusions_current=True,external_self_owners=[])
            with self.assertRaisesRegex(ValueError,'fresh_external_self_exclusion'):
                self.check_admission(pair,action)

    def test_external_self_cannot_relabel_host_or_accept_partial_or_unbound_custody(self):
        from causes import campaign_corpus_succession as succession, campaign_handoff as handoff
        from tests.causes.test_native_stack_transition import TransitionFixture
        fixture = TransitionFixture(self)
        policy, registration = fixture.transition()
        row = fixture.external_self_transition(policy,registration)
        original = copy.deepcopy(row)
        accept = lambda pair,flag,key: succession.accepted(pair,flag,key)
        changes = [('host', lambda value:value.update(host='fixture')),
                   ('kind', lambda value:value.update(kind='actor')),
                   ('partial', lambda value:value.update(closure=fixture.mutate(value['closure'],
                       lambda closed:closed.update(terminal=18)))),
                   ('executor', lambda value:value.update(execution_custody=fixture.mutate(value['execution_custody'],
                       lambda custody:custody.update(execution_host='another-host')))),
                   ('closed', lambda value:value.update(execution_custody=fixture.mutate(value['execution_custody'],
                       lambda custody:custody.update(closed_on_execution_host=False)))),
                   ('future', lambda value:value.update(execution_custody=fixture.mutate(value['execution_custody'],
                       lambda custody:custody.update(future_self_launch_excluded=False))))]
        for name,change in changes:
            with self.subTest(name=name):
                row = copy.deepcopy(original); change(row)
                with self.assertRaisesRegex(ValueError,'external_self_'):
                    b.external_self_custody(dict(owners=registration['prior_local_owners'],external_self_owners=[row]),
                        'fixture',policy['provider'],handoff.checked,handoff.checked_bytes,accept)


if __name__ == '__main__':
    unittest.main()
