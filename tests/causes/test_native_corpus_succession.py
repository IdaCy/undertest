"""Synthetic native-owner lineage and exact closed-issued-history checks."""

import copy
import fcntl
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_corpus_continuation import fixture


class ClosedIssuedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def save(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(h.encoded(value))
        return h.pin(path)

    def history(self):
        args = fixture()
        block = CorpusContinuation(**args).block(0)
        first = block['jobs'][0]
        rid = first['allocation']['run_id']
        summary = self.save(rid + '/summary.json', {'fixture': 'closed'})
        rows = [
            {'event': 'extension_block_created', 'block_index': 0,
             'block_sha256': s.digest(block)},
            {'event': 'claimed', 'run_id': rid, 'job_sha256': s.digest(first),
             'config_sha256': s.digest(args['config'])},
            {'event': 'pause_seen', 'admission_stopped': True},
            {'event': 'episode_finished', 'run_id': rid,
             'job_sha256': s.digest(first), 'summary_sha256': summary['sha256'],
             'technical_failure': False, 'replayed': False},
            {'event': 'controller_paused', 'pending': len(block['jobs']) - 1},
        ]
        pending = sorted(j['allocation']['run_id'] for j in block['jobs'][1:])
        custody = {'completed_ids': [rid], 'never_claimed_ids': pending,
                   'summaries': {rid: summary}}
        return args, block, rows, custody

    def check(self, args, block, rows, custody):
        raw = b''.join(h.encoded(row) + b'\n' for row in rows)
        return s.closed_issued_history([], args['config'], raw, custody,
                                       [block], CorpusContinuation(**args))

    def test_natural_pause_preserves_unclaimed_block_without_fabricated_finals(self):
        args, block, rows, custody = self.history()
        result = self.check(args, block, rows, custody)
        self.assertEqual(result['issued'], 1)
        self.assertEqual(result['finals'], 1)
        self.assertEqual(result['assigned'], len(block['jobs']))
        self.assertEqual(result['never_claimed_ids'], custody['never_claimed_ids'])
        self.assertEqual(result['persisted_block_hashes'], {0: s.digest(block)})

    def test_dropped_or_reclassified_pending_and_unclosed_claim_refuse(self):
        args, block, rows, custody = self.history()
        bad = copy.deepcopy(custody)
        bad['never_claimed_ids'].pop()
        with self.assertRaises(ValueError):
            self.check(args, block, rows, bad)
        bad = copy.deepcopy(custody)
        bad['completed_ids'].append(bad['never_claimed_ids'].pop())
        with self.assertRaises(ValueError):
            self.check(args, block, rows, bad)
        for changed in (rows[:3] + rows[4:], rows + [rows[1]],
                        rows[:3] + [{'event': 'unresolved_current_attempt'}] + rows[3:]):
            with self.subTest(rows=changed), self.assertRaises(ValueError):
                self.check(args, block, changed, custody)

    def test_block_claim_summary_and_replay_tamper_refuse(self):
        args, block, rows, custody = self.history()
        changed = copy.deepcopy(block)
        changed['jobs'][0]['allocation']['seed'] += 1
        with self.assertRaises(ValueError):
            self.check(args, changed, rows, custody)
        for field, value in (('job_sha256', 'changed'), ('summary_sha256', 'changed'),
                             ('technical_failure', None), ('replayed', True)):
            bad = copy.deepcopy(rows)
            bad[3][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(args, block, bad, custody)
        Path(next(iter(custody['summaries'].values()))['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.check(args, block, rows, custody)

    def closure(self):
        output = self.root / 'output'
        (output / '.dispatcher').mkdir(parents=True, exist_ok=True)
        lock = output / '.dispatcher.lock'
        lock.touch()
        journal = self.save('output/.dispatcher/journal.jsonl', {'fixture': 'journal'})
        pending = self.save('never_claimed.json', ['pending'])
        process = {'pid': 2147483647, 'process_starttime_ticks': '1', 'argv': ['fixture']}
        launch = self.save('launch.json', {'process': process})
        pause = self.save('PAUSE', {'owner': process})
        stat = h.stat_identity(pause['path'])
        ownership = self.save('pause_ownership.json',
                              {'process': process, 'marker': pause, 'marker_stat': stat})
        value = {'schema': 'causes.closed_issued_corpus_history.v1',
                 'output_root': str(output), 'process': process, 'launch': launch,
                 'claims': 0, 'finals': 0, 'active_claims': 0, 'ambiguous_ids': [],
                 'pending_count': 1, 'never_claimed': pending, 'owner_absent': True,
                 'output_lock_free': True, 'journal': journal, 'owned_pause': pause,
                 'owned_pause_stat': stat, 'pause_ownership': ownership}
        evidence = self.save('closure.json', value)
        review = self.save('review.json', {'accepted_closed_issued_corpus_history': True,
                                          'closure': evidence})
        return {'output_root': str(output), 'process': process, 'launch': launch,
                'pause_path': pause['path'], 'closure': {'evidence': evidence, 'review': review}}, lock

    def test_live_boundary_requires_free_real_lock_and_same_owned_pause(self):
        row, lock = self.closure()
        self.assertEqual(s.live_closed_predecessor(row)['pending_count'], 1)
        with lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'still_live_or_locked'):
                s.live_closed_predecessor(row)
        pause = Path(row['pause_path'])
        raw = pause.read_bytes()
        pause.unlink()
        pause.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'pause_replaced'):
            s.live_closed_predecessor(row)


class NativeAllocationTests(unittest.TestCase):
    setUp = ClosedIssuedTests.setUp
    save = ClosedIssuedTests.save
    closure = ClosedIssuedTests.closure

    def allocation(self, finite=True, prefix=False):
        data = fixture()
        refs = {key: self.save('allocation/'+key+'.json', data[key])
                for key in ('catalog', 'prototypes', 'original_jobs', 'config', 'reservation')}
        control = dict(schema='causes.corpus_continuation_control.v1' if prefix else 'causes.corpus_continuation_control.v2',
                       **refs, **{key: data[key] for key in ('family', 'model_key', 'task_host', 'provider_host')},
                       repetition_start=1)
        if not prefix: control['full_selection'] = {'method':'one_cell_per_task_rotating_v1'}
        cref = self.save('allocation/control.json', control)
        allocator = CorpusContinuation.from_control(control, data['prototypes'], data['config'])
        jobs = allocator.block(0)['jobs'] if finite else []
        ref = self.save('allocation/jobs.json', jobs)
        value = dict(schema='causes.native_corpus_allocation.v1', origin='native_successor',
                     no_fixed_study_pooling=True, target_mode='finite_qualification' if finite else 'continuous_corpus',
                     workload='prefix_only' if prefix else 'mixed', workers=8 if prefix else 4,
                     max_full=0 if prefix else 1, family=data['family'], model_key=data['model_key'],
                     controller_host=data['task_host'], jobs=ref, config=refs['config'],
                     extensions=self.save('allocation/empty.json', []) if finite else refs['prototypes'],
                     allocation_control=cref, control=None if finite else cref,
                     selection=dict(block_index=0,run_ids=[job['allocation']['run_id'] for job in jobs]) if finite else None,
                     output_root=str(self.root/'new'))
        return value

    def test_native_finite_and_prefix_fallback_keep_distinct_explicit_workloads(self):
        mixed = self.allocation()
        jobs, config, extensions, _ = s.native_allocation(mixed)
        self.assertEqual(len(jobs), 5)
        self.assertEqual(sum(job['allocation']['continue_to_end'] for job in jobs), 2)
        self.assertEqual(extensions, [])
        prefix = self.allocation(finite=False, prefix=True)
        jobs, _, extensions, allocator = s.native_allocation(prefix)
        self.assertEqual(jobs, [])
        self.assertTrue(extensions)
        self.assertEqual(allocator.binding['schema'], 'causes.corpus_continuation_binding.v1')
        self.assertFalse(any(job['allocation']['continue_to_end'] for job in allocator.block(0)['jobs']))

    def test_native_selected_jobs_seeds_modes_and_full_cap_cannot_drift(self):
        value = self.allocation()
        for change in (lambda v:v.update(max_full=0), lambda v:v.update(workload='prefix_only'),
                       lambda v:v.update(no_fixed_study_pooling=False),
                       lambda v:v['selection']['run_ids'].reverse(),
                       lambda v:v.update(control=v['allocation_control']),
                       lambda v:v['selection'].update(block_index=1)):
            changed = copy.deepcopy(value); change(changed)
            with self.subTest(change=change), self.assertRaises(ValueError): s.native_allocation(changed)
        jobs = h.checked(value['jobs']); jobs[0]['allocation']['seed'] += 1
        changed = copy.deepcopy(value); changed['jobs'] = self.save('changed-jobs.json', jobs)
        with self.assertRaisesRegex(ValueError, 'selection_changed'): s.native_allocation(changed)
        prefix = self.allocation(finite=False, prefix=True)
        prefix['max_full'] = 1
        with self.assertRaisesRegex(ValueError, 'workload_changed'): s.native_allocation(prefix)

    def runtime(self, finite=True, prefix=False):
        from causes import campaign_admission as a
        value = self.allocation(finite, prefix)
        names = ('campaign_dispatcher.py', 'campaign_runner.py', 'campaign_sandbox.py',
                 'corpus_allocation.py', 'corpus_continuation.py')
        files = [dict(name=name, **self.save('code/'+name, {'source': name})) for name in names]
        bundle = self.save('bundle.json', {'path': str(self.root/'code'), 'files': files})
        common = dict(python='/usr/bin/python3', pythonpath=str(self.root/'code'),
                      dedicated_pause=str(self.root/'new-pause'), global_pause=str(self.root/'global-pause'),
                      barrier=str(self.root/'new-barrier'), holds=self.save('holds.json', {}),
                      allocator_package=self.save('allocator-package.json', {}),
                      allocator_review=self.save('allocator-review.json', {}))
        flags = {'--jobs':value['jobs']['path'], '--config':value['config']['path'], '--output':value['output_root'],
                 '--workers':str(value['workers']), '--max-full':str(value['max_full']),
                 '--extension-prototypes':value['extensions']['path'], '--pause-file':common['dedicated_pause'],
                 '--global-pause-file':common['global_pause'], '--start-barrier-file':common['barrier'],
                 '--admission-holds':common['holds']['path'], '--admission-holds-sha256':common['holds']['sha256']}
        if not finite:
            flags.update({'--corpus-continuation':value['control']['path'],
                          '--corpus-continuation-sha256':value['control']['sha256']})
        argv = [common['python'], '-B', '-m', 'causes.campaign_dispatcher']
        argv += [part for pair in flags.items() for part in pair]
        template = dict(phase='native', jobs=value['jobs'], config=value['config'],
                        runtime_cohort=value['family'], output_root=value['output_root'], proposed_workers=value['workers'],
                        bundle=bundle, argv_template_at_proposed_cap=['env', 'PYTHONPATH='+common['pythonpath'], *argv])
        template['empty_extensions' if finite else 'prototypes'] = value['extensions']
        common['controller_template'] = self.save('controller-template.json', {'templates':[template]})
        return value, template, common, a.native_runtime(value, template, common)

    def test_runtime_queue_and_exact_caps_match_finite_and_prefix_dispatcher_contracts(self):
        from causes import campaign_admission as a
        for finite, prefix in ((True,False),(False,False),(False,True)):
            with self.subTest(finite=finite,prefix=prefix):
                value, template, common, runtime = self.runtime(finite,prefix)
                self.assertEqual(runtime['expected_controller_fields']['max_full'], value['max_full'])
                self.assertEqual(runtime['expected_controller_fields']['workers'], value['workers'])
                self.assertEqual('corpus_continuation' in runtime['expected_queue'], not finite)
                self.assertEqual(runtime['continuation'] is None, finite)
                bad = copy.deepcopy(template)
                argv = bad['argv_template_at_proposed_cap']
                argv[argv.index('--max-full')+1] = '2'
                with self.assertRaisesRegex(ValueError,'command_scope'): a.native_runtime(value,bad,common)
                bad = copy.deepcopy(template);bad['proposed_workers'] += 1
                with self.assertRaisesRegex(ValueError,'template_allocation'): a.native_runtime(value,bad,common)

    def test_native_binding_is_pure_and_exact_fixed_dynamic_scope(self):
        from causes import campaign_admission as a
        value, template, common, runtime = self.runtime()
        fixed = {**common, 'schema':s.ADMISSION_SCHEMA}
        fixed.pop('controller_template')
        dynamic = ['allocation','controller_template','normalized_template','donor_histories',*runtime]
        tref = self.save('policy-template.json',dict(schema=s.TEMPLATE_SCHEMA,fixed=fixed,dynamic_keys=dynamic))
        alloc_ref = self.save('allocation.json',value)
        before = sorted(str(path) for path in self.root.rglob('*'))
        result = a.bind_native_policy(tref,alloc_ref,common['controller_template'],[])
        self.assertEqual(result['expected_queue'], runtime['expected_queue'])
        self.assertEqual(sorted(str(path) for path in self.root.rglob('*')), before)
        self.assertIsNone(result['continuation'])
        bad = h.checked(tref);bad['dynamic_keys'].remove('argv')
        with self.assertRaisesRegex(ValueError,'field_scope'):
            a.bind_native_policy(self.save('bad-template.json',bad),alloc_ref,common['controller_template'],[])


class NativeAdmissionTests(unittest.TestCase):
    setUp = ClosedIssuedTests.setUp
    save = ClosedIssuedTests.save
    closure = ClosedIssuedTests.closure
    allocation = NativeAllocationTests.allocation
    runtime = NativeAllocationTests.runtime

    def operator(self, prefix=False):
        from causes import campaign_admission as a, campaign_monitor as m, corpus_allocation as ca
        import sys
        value, template, common, runtime = self.runtime(finite=not prefix, prefix=prefix)
        predecessor, _ = self.closure()
        predecessor['phase'] = 'previous'
        config = h.checked(value['config'])
        aliases = ['model:'+s.digest([config['model'], config['endpoint']])]
        jobs = h.checked(value['jobs']) or h.checked(value['extensions'])
        routes = sorted({'task:'+s.digest([job['spec'].get('task_id'),job['spec'].get('image')]) for job in jobs})
        history = dict(donor_scopes=[dict(host='fixture',output_root=predecessor['output_root'],mode='sealed',recorder_relevant=True)],
                       model_configs=[value['config']], model_aliases=aliases, task_routes=routes)
        base = dict(provider={'fixture':'provider'}, model='fixture',model_key='mi27',controller_host='fixture',provider_port=8000,
                    max_model_len=100,host_transfer={'target_output_roots':['original-transferred-only']},holds=common['holds'],
                    phases=[dict({k:predecessor[k] for k in ('output_root','process')},controller_config=value['config'],runtime_cohort='old-family')],history={'donor_scopes':[],'model_aliases':aliases})
        bref = self.save('base.json',base)
        bpair = dict(evidence=bref,review=self.save('base-review.json',dict(accepted_native_predecessor_monitor=True,policy=bref)))
        allocref = self.save('allocation.json',value)
        allocpair = dict(evidence=allocref,review=self.save('allocation-review.json',dict(accepted_native_corpus_allocation=True,allocation=allocref)))
        adoption = self.save('allocation-adoption.json',dict(root_accepted=True,allocation=allocref,allocation_review=allocpair['review']))
        modules = ['causes.campaign_admission','causes.campaign_corpus_succession','causes.campaign_monitor',
                   'causes.campaign_handoff','causes.corpus_allocation','causes.corpus_continuation']
        p = dict(common, schema=s.ADMISSION_SCHEMA, origin='native_successor', allocation_adoption=adoption,
                 **{key:value[key] for key in ('family','output_root','workers','max_full','target_mode','workload')},
                 source_sha256=h.pin(a.__file__)['sha256'],source_pins=[],static_pins=[],
                 operator_module_hashes={name:h.pin(sys.modules[name].__file__)['sha256'] for name in modules},
                 runtime_source_pins=[{key:row[key] for key in ('path','bytes','sha256')} for row in h.checked(template['bundle'])['files']],
                 initial_jobs=value['jobs'],resident_config=value['config'],exec_wrapper=h.pin(h.__file__),
                 predecessor_monitor=bpair,predecessors=[predecessor],history=history,
                 resource_decision={'disk_floor_bytes':1},pause_paths=[common['dedicated_pause'],common['global_pause']],
                 expected_admission_control={'schema':'caus.dispatcher_admission_control.v1','admission_holds':common['holds']},
                 phase='native',static_acceptances={},acceptance_rules={},
                 **{key:base[key] for key in ('provider','model','model_key','controller_host','provider_port','max_model_len','host_transfer')})
        for key in ('runtime','native_host','images','source_publication','capacity_inheritance','resource_decision'):
            evidence=self.save(key+'.json',{'fixture':key});flag='accepted_'+key
            review={flag:True,'evidence':evidence}
            if key=='capacity_inheritance':review.update(accepted_native_capacity_admission=True,next_phase='native',next_worker_cap=value['workers'],
                max_full=value['max_full'],workload=value['workload'],resident_config=value['config'])
            pair=dict(evidence=evidence,review=self.save(key+'-review.json',review));p['static_acceptances'][key]=pair
            p['acceptance_rules'][key]=dict(flag=flag,binding_key='evidence')
        dynamic=['allocation','controller_template','normalized_template','donor_histories',*runtime]
        p.pop('controller_template')
        tref=self.save('policy-template.json',dict(schema=s.TEMPLATE_SCHEMA,fixed=p,dynamic_keys=dynamic))
        donors=[dict(history['donor_scopes'][0],journal=h.checked(predecessor['closure']['evidence'])['journal'])]
        policy=a.bind_native_policy(tref,allocref,common['controller_template'],donors)
        pref=self.save('policy.json',policy)
        source_review=self.save('source-review.json',dict(accepted_native_corpus_admission_source=True,accepted_native_corpus_policy_template=True,
            policy_template=tref,source=h.pin(a.__file__)))
        inputs=dict(policy=pref,source_review=source_review,allocation=allocpair,predecessor_monitor=bpair,**p['static_acceptances'])
        iref=self.save('inputs.json',inputs)
        return a.NativeAdmission(pref,iref,{}), predecessor

    def test_whole_native_static_contract_accepts_qualification_and_exact_prefix_fallback(self):
        for prefix in (False,True):
            with self.subTest(prefix=prefix):
                operator,_=self.operator(prefix)
                config,jobs=operator.static()
                self.assertEqual(config['model'],'fixture')
                self.assertTrue(jobs)
                self.assertEqual(operator.policy['max_full'],0 if prefix else 1)
                self.assertEqual(operator.policy['workers'],8 if prefix else 4)

    def test_reviewed_native_fixed_scope_and_dynamic_queue_tampering_refuse(self):
        operator,_=self.operator()
        for key,value in [('max_full',2),('workers',8),('host_transfer',{'target_output_roots':['invented']}),
                          ('expected_queue',{}),('donor_histories',[])]:
            original=copy.deepcopy(operator.policy)
            operator.policy[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):operator.static()
            operator.policy=original

    def test_native_retirement_hook_observes_closed_owner_without_signal(self):
        from unittest.mock import Mock,patch
        from causes import campaign_admission as a
        operator,_=self.operator()
        attempt=self.root/'attempt';attempt.mkdir()
        authority=Mock()
        with patch.object(h,'retire_exact_idle_owner',side_effect=AssertionError('no termination')):
            receipt=operator.retire_predecessor(attempt,authority)
        self.assertTrue(h.checked(receipt)['no_signal_or_restart'])
        authority.check.assert_called_once()

    def test_native_waiting_uses_finite_pending_count_and_rejects_claims(self):
        from unittest.mock import patch
        from causes import campaign_admission as a
        from tests.causes.test_campaign_admission import AdmissionTests
        # Reuse the existing real on-disk waiting identity fixture.
        fixture=AdmissionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        policy,process,root=fixture.waiting()
        policy.update(schema=s.ADMISSION_SCHEMA,initial_jobs=fixture.save('jobs.json',[{'fixture':1},{'fixture':2}]))
        path=root/'.dispatcher/journal.jsonl';rows=[json.loads(line) for line in path.read_bytes().splitlines()]
        rows[-1]['pending']=2;path.write_bytes(b''.join(h.encoded(row)+b'\n' for row in rows))
        with patch.object(h,'process_identity'),patch.object(a,'exact_children_absent'),patch.object(h,'lock_owned_by',return_value=True):
            self.assertTrue(a.waiting_identity(policy,process)['zero_initial_claims'])
            rows[-1]['pending']=0;path.write_bytes(b''.join(h.encoded(row)+b'\n' for row in rows))
            with self.assertRaisesRegex(ValueError,'waiting_owner_not_observed'):a.waiting_identity(policy,process)


class NativeMonitorTests(unittest.TestCase):
    setUp = ClosedIssuedTests.setUp
    save = ClosedIssuedTests.save
    allocation = NativeAllocationTests.allocation
    runtime = NativeAllocationTests.runtime

    def lineage(self):
        from causes import campaign_monitor as m
        from tests.causes.test_campaign_monitor import fixture as monitor_fixture, add_continuation
        base, reg, objects, ref = monitor_fixture()
        add_continuation(base, reg, objects, ref)
        value, template, common, runtime = self.runtime()
        # The real allocator pins reside on disk; the old monitor metadata is
        # the existing synthetic fixture with a whole accepted base reference.
        base['remote_base'] = str(self.root)
        for phase in base['phases']:
            phase['output_root'] = str(self.root/'runs'/phase['phase'])
        base['host_transfer']['target_output_roots'] = [phase['output_root'] for phase in base['phases'] if phase['mode']=='finite']
        transfer = objects[base['host_transfer']['manifest']['path']]
        transfer['target_output_roots'] = base['host_transfer']['target_output_roots'].copy()
        base['controller_host'] = 'fixture'; transfer['target_host'] = 'fixture'
        base['model'] = 'fixture'
        base['holds'] = common['holds']
        base['phases'][-1]['controller_config'] = value['config']
        base['history']['model_configs'] = [value['config']]
        cfg = h.checked(value['config'])
        base['history']['model_aliases'] = ['model:'+s.digest([cfg['model'],cfg['endpoint']])]
        for row, phase in zip(base['history']['donor_scopes'],base['phases']):
            row['host'] = 'fixture';row['output_root'] = phase['output_root']
        basereg = {'phase':base['phases'][-1]['phase']}
        base_ref, reg_ref = ref('accepted-base-policy.json',copy.deepcopy(base)),ref('accepted-base-registration.json',basereg)
        review_ref = ref('accepted-base-review.json',dict(accepted_native_succession_base=True,policy=base_ref,registration=reg_ref))
        previous = base['phases'][-1]
        pause = self.save('old-pause.json',{'fixture':'pause'})
        process = previous['process']
        launch = ref('old-launch.json',{'process':process})
        ownership = ref('old-pause-owner.json',dict(process=process,marker=pause,marker_stat=h.stat_identity(pause['path'])))
        closure = dict(schema='causes.closed_issued_corpus_history.v1',output_root=previous['output_root'],process=process,launch=launch,
            claims=2,finals=2,active_claims=0,ambiguous_ids=[],owner_absent=True,output_lock_free=True,pending_count=2,
            never_claimed=self.save('old-pending.json',['old-a','old-b']),journal=dict(path=previous['output_root']+'/.dispatcher/journal.jsonl',bytes=2,sha256='j'),
            owned_pause=pause,owned_pause_stat=h.stat_identity(pause['path']),pause_ownership=ownership)
        closed_ref=ref('old-closed.json',closure)
        predecessor=dict(phase=previous['phase'],output_root=previous['output_root'],process=process,launch=launch,pause_path=pause['path'],
                         closure=dict(evidence=closed_ref,review=ref('old-closed-review.json',dict(accepted_closed_issued_corpus_history=True,closure=closed_ref))))
        allocref=self.save('allocation.json',value)
        allocpair=dict(evidence=allocref,review=ref('allocation-review.json',dict(accepted_native_corpus_allocation=True,allocation=allocref)))
        link=dict(phase='native',predecessor=predecessor,allocation=allocpair,
                  root_adoption=ref('allocation-adoption.json',dict(root_accepted=True,allocation=allocref,allocation_review=allocpair['review'])))
        policy=copy.deepcopy(base);policy['schema']=s.POLICY_SCHEMA
        policy['native_successions']=dict(base_policy=base_ref,base_registration=reg_ref,base_review=review_ref,
                                          base_phase_count=len(base['phases']),links=[link],validator_source=h.pin(s.__file__))
        phase=dict(phase='native',mode='finite',output_root=value['output_root'],workers=value['workers'],
                   process=dict(pid=999,process_starttime_ticks='999',argv=runtime['argv']),runtime_cohort=value['family'],
                   controller_jobs=value['jobs'],controller_config=value['config'],capacity_required=True,
                   controls={key:common[name] for key,name in [('--pause-file','dedicated_pause'),('--global-pause-file','global_pause'),('--start-barrier-file','barrier')]},
                   controller_templates=common['controller_template'])
        policy['phases'].append(phase);policy['sync_roots'].append(str(Path(value['output_root']).relative_to(self.root)))
        donors=[dict(host='fixture',output_root=p['output_root'],mode='sealed',recorder_relevant=True,
                     journal=closure['journal'] if p==previous else ref('history-'+p['phase'],{})) for p in base['phases']]
        phase['donor_histories']=donors
        policy['history']['donor_scopes']=[{k:row[k] for k in ('host','output_root','mode','recorder_relevant')} for row in donors]
        jobs=h.checked(value['jobs'])
        policy['history']['task_routes']=sorted({'task:'+s.digest([job['spec'].get('task_id'),job['spec'].get('image')]) for job in jobs})
        cap=ref('capacity.json',{})
        capacity=dict(evidence=cap,independent_review=ref('capacity-review.json',dict(accepted_native_capacity_admission=True,
            evidence=cap,next_phase='native',next_worker_cap=4,max_full=1,workload='mixed',resident_config=value['config'])))
        bundle=h.checked(template['bundle'])
        registration=dict(schema='caus.jp_companion_registration.v1',status='active',root_authorized=True,phase='native',
            runtime_cohort=value['family'],controller_host='fixture',controller_jobs=value['jobs'],controller_config=value['config'],
            controller_templates=common['controller_template'],prior_local_owners=[{k:p[k] for k in ('output_root','process')} for p in base['phases']],
            capacity_qualification=capacity,host_transfer=copy.deepcopy(policy['host_transfer']),provider=policy['provider'],tunnels=policy['tunnels'],
            reverse_listener_process=policy['reverse_listener_process'],continuation=None,donor_histories=donors,
            controller=dict(model='mi27',workers=4,output_root=value['output_root'],process=phase['process'],pythonpath=common['pythonpath'],
                source_pins=[{k:row[k] for k in ('path','bytes','sha256')} for row in bundle['files']],input_pins=runtime['runtime_input_pins'],
                queue_identity={'path':value['output_root']+'/.dispatcher/queue_identity.json'},
                admission_control={'path':value['output_root']+'/.dispatcher/admission_control.json'}))
        def reader(reference):
            return copy.deepcopy(objects[reference['path']]) if reference['path'] in objects else h.checked(reference)
        return policy,registration,objects,reader

    def test_native_finite_after_continuous_is_explicit_without_fake_transfer(self):
        from causes import campaign_monitor as m
        policy,registration,objects,reader=self.lineage()
        self.assertEqual(m.validate_registration(registration,policy,reader)['mode'],'finite')
        self.assertEqual(len(policy['host_transfer']['target_output_roots']),3)
        self.assertEqual(len(policy['sync_roots']),5)
        legacy=copy.deepcopy(policy);legacy['schema']='causes.campaign_monitor_policy.v1';legacy.pop('native_successions')
        with self.assertRaises(ValueError):m.validate_registration(registration,legacy,reader)

    def test_actual_remote_transfer_predicate_accepts_only_exact_native_finite_tail(self):
        import ast
        from causes import campaign_monitor as m
        tree = ast.parse(m.REMOTE)
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'native_tail_output')
        assertion = next(node for node in ast.walk(tree) if isinstance(node, ast.Assert)
                         and "reservation['target_host']" in ast.unparse(node.test))
        code = compile(ast.Module(body=[helper, assertion], type_ignores=[]),
                       '<actual campaign_monitor.REMOTE predicate>', 'exec')
        policy, registration, _, reader = self.lineage()
        m.validate_registration(registration, policy, reader)
        reservation = dict(target_host=policy['controller_host'],
                           target_output_roots=policy['host_transfer']['target_output_roots'],
                           jobs=policy['host_transfer']['original_phase02_jobs'])

        def check(p, r, read=reader):
            exec(code, dict(policy=p, x=r, reservation=reservation, json=json,
                            pincheck=lambda ref:h.encoded(read(ref))))

        check(policy, registration)
        self.assertNotIn(registration['controller']['output_root'], reservation['target_output_roots'])
        changes = [lambda p,r:p.update(schema='causes.campaign_monitor_policy.v1'),
                   lambda p,r:r.update(phase='wrong-native-phase'),
                   lambda p,r:r['controller'].update(output_root='/wrong-native-root'),
                   lambda p,r:p['native_successions']['links'][-1].update(phase='wrong-link'),
                   lambda p,r:r.update(controller_jobs={'wrong':'jobs'})]
        for change in changes:
            p, r = copy.deepcopy(policy), copy.deepcopy(registration)
            change(p, r)
            with self.subTest(change=change), self.assertRaises(AssertionError):
                check(p, r)
        def changed_allocation(ref):
            value = reader(ref)
            if value.get('schema') == 'causes.native_corpus_allocation.v1':
                value['output_root'] = '/wrong-linked-allocation'
            return value
        with self.assertRaises(AssertionError):
            check(policy, registration, changed_allocation)

    def test_native_base_owner_pending_capacity_and_donor_tampering_refuse(self):
        from causes import campaign_monitor as m
        changes=[lambda p,r:p['sync_roots'].reverse(), lambda p,r:p['phases'][0].update(workers=9),
                 lambda p,r:p['native_successions']['links'][0]['predecessor']['process'].update(pid=123),
                 lambda p,r:r['donor_histories'].pop(),lambda p,r:r['controller']['process']['argv'].__setitem__(
                     r['controller']['process']['argv'].index('--max-full')+1,'2')]
        for change in changes:
            policy,registration,objects,reader=self.lineage();change(policy,registration)
            with self.subTest(change=change),self.assertRaises(ValueError):m.validate_registration(registration,policy,reader)
        policy,registration,objects,reader=self.lineage()
        closure=policy['native_successions']['links'][0]['predecessor']['closure']['evidence']
        objects[closure['path']]['pending_count']=0
        with self.assertRaisesRegex(ValueError,'pending_list_changed'):m.validate_registration(registration,policy,reader)


class NativeEvidenceTests(unittest.TestCase):
    def evidence(self, release=False):
        from causes import campaign_admission as a
        from tests.causes.test_campaign_admission import AdmissionTests
        fixture=AdmissionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        old,_,_=fixture.evidence_fixture(a.RELEASE if release else a.START)
        operator=a.NativeAdmission.__new__(a.NativeAdmission)
        operator.policy,operator.inputs=old.policy,old.inputs
        operator.policy_ref={'path':'/fixture/native-policy','bytes':1,'sha256':'fixture'}
        p,x=operator.policy,operator.inputs
        p['allocation']=fixture.save('native-allocation.json',{'allocation_control':fixture.save('native-control.json',
            {'reservation':x['precreation_reservation']['evidence']})})
        p['required_sync_roots']={'mi':['existing'],'jp':['old','new']}
        foundation=h.checked(x['foundation']['evidence'])
        foundation.update(predecessors=p['predecessors'],legacy_owners_healthy=True,all_predecessors_absent_and_unlocked=True)
        ref=fixture.save('native-foundation.json',foundation)
        x['foundation']={'evidence':ref,'review':fixture.save('native-foundation-review.json',{'accepted_foundation':True,'evidence':ref})}
        if release:
            ready=h.checked(x['startup']['evidence'])
            ready.update(policy=operator.policy_ref,legacy_owners_healthy=True,mandatory_companion_valid=True,snapshot_ownership_valid=True,
                         required_sync_roots=p['required_sync_roots'],evidence_pins=[])
            ref=fixture.save('native-ready.json',ready)
            x['startup']={'evidence':ref,'review':fixture.save('native-ready-review.json',{'accepted_native_startup_consumers':True,'readiness':ref})}
        return operator,fixture

    def test_native_release_preserves_original_freshness_and_exact_roots(self):
        from unittest.mock import patch
        from causes import campaign_admission as a
        import datetime as dt
        operator,fixture=self.evidence(True)
        with patch.object(a,'validate_registration') as validate:
            result=operator.evidence(operator.release_action)
            self.assertTrue(result['all_predecessors_absent_and_unlocked'])
            validate.assert_called_once()
        for change in (lambda v:v.update(at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat()),
                       lambda v:v.update(required_sync_roots={'jp':['new']}),
                       lambda v:v.update(snapshot_ownership_valid=False),
                       lambda v:v.update(launch={'different':True})):
            operator,fixture=self.evidence(True)
            ready=h.checked(operator.inputs['startup']['evidence']);change(ready)
            ref=fixture.save('changed-ready.json',ready)
            operator.inputs['startup']={'evidence':ref,'review':fixture.save('changed-review.json',{'accepted_native_startup_consumers':True,'readiness':ref})}
            with self.subTest(change=change),self.assertRaises(ValueError):operator.evidence(operator.release_action)

    def test_native_foundation_cannot_drop_predecessor_or_allow_active_work(self):
        for field,value in [('predecessors',[]),('all_predecessors_absent_and_unlocked',False),
                            ('native_no_active_owned_work',False),('provider',{'changed':True})]:
            operator,fixture=self.evidence()
            found=h.checked(operator.inputs['foundation']['evidence']);found[field]=value
            ref=fixture.save('changed-foundation.json',found)
            operator.inputs['foundation']={'evidence':ref,'review':fixture.save('changed-foundation-review.json',{'accepted_foundation':True,'evidence':ref})}
            with self.subTest(field=field),self.assertRaises(ValueError):operator.evidence(operator.start_action)


if __name__ == '__main__':
    unittest.main()
