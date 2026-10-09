import ast
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_monitor as m


def fixture():
    objects = {}
    def ref(name, value):
        raw = m.encoded(value)
        result = {'path':'/fixture/' + name,'bytes':len(raw),'sha256':m.digest(raw)}
        objects[result['path']] = value
        return result
    roots = ['/fixture/runs/q4','/fixture/runs/q8','/fixture/runs/main']
    phase_names = ['q4','q8','main']
    hosts = {'controller_host':'provider','transfer_host':'tasks'}
    config = ref('config.json', {'model':'example','endpoint':'http://localhost/v1/chat/completions'})
    jobs = ref('jobs.json', [])
    extension = ref('empty.json', [])
    holds = ref('holds.json', {'holds':[]})
    bundle = ref('bundle.json', {'path':'/fixture/code','files':[{'path':'/fixture/code/dispatcher.py','bytes':3,'sha256':'a'*64}]})
    transfer = {'target_output_roots':roots.copy(),'original_phase02_jobs':jobs,'mi_reservation':ref('reservation.json',{}),
                'mi_unused_output_root':'/fixture/runs/unused'}
    transfer['manifest'] = ref('transfer.json', {'mi_reservation':transfer['mi_reservation'],'jobs':jobs,
        'target_output_roots':roots.copy(),'target_host':'provider','all_target_ids_unissued_on_both_hosts':True,'prior_phase01_retired':True})
    transfer['independent_review'] = ref('transfer-review.json', {'accepted_JP_phase02_host_transfer':True,'transfer':transfer['manifest']})
    phases = []; templates = []
    for i, (name, root) in enumerate(zip(phase_names, roots)):
        workers = 4 if i == 0 else 8
        controls = {'--pause-file':'/fixture/controls/PAUSE','--global-pause-file':'/fixture/PAUSE','--start-barrier-file':'/fixture/controls/BARRIER'}
        argv = ['/usr/bin/python3','-m','causes.campaign_dispatcher','--workers',str(workers),'--output',root]
        for flag, value in controls.items(): argv.extend((flag,value))
        process = {'pid':100+i,'process_starttime_ticks':str(200+i),'argv':argv}
        phases.append({'phase':name,'mode':'finite','output_root':root,'workers':workers,'process':process,'runtime_cohort':'finite',
                       'controller_jobs':jobs,'controller_config':config,'capacity_required':i>0,'controls':controls})
        templates.append({'phase':name,'output_root':root,'runtime_cohort':'finite','proposed_workers':workers,
                          'argv_template_at_proposed_cap':['env','PYTHONPATH=/fixture/code',*argv],
                          'jobs':jobs,'config':config,'empty_extensions':extension,'bundle':bundle})
    template_pin = ref('templates.json', {'templates':templates})
    for phase in phases: phase['controller_templates'] = template_pin
    policy = {'schema':'causes.campaign_monitor_policy.v1','required':True,'remote_base':'/fixture',**hosts,
              'model_key':'mi27','model':'example','max_model_len':100,'provider_port':8000,'relay_port':8001,
              'minimum_gpu_free_mib':2048,'max_observation_age_seconds':180,
              'sync_roots':['runs/q4','runs/q8','runs/main'],'phases':phases,'holds':holds,'host_transfer':transfer,
              'provider':{'unit':'example.service'},'tunnels':[{'process':{'pid':91,'argv':['ssh','first']}},{'process':{'pid':92,'argv':['ssh','second']}}],'reverse_listener_process':{'pid':90},
              'legacy':{'owners':[{'pid':1,'model':'mi27'},{'pid':2,'model':'tex9'}],
                        'identity_receipt':ref('identity.json',{}),'files':[{'path':'/legacy/code','expected_sha256':'b'*64}],
                        'alarm_keys':['source_or_controller_identity_mismatch']}}
    reg = {'schema':'caus.jp_companion_registration.v1','status':'active','root_authorized':True,
           'phase':'q4','runtime_cohort':'finite','controller_host':'provider','controller_jobs':jobs,'controller_config':config,
           'controller_templates':template_pin,'prior_local_owners':[],'capacity_qualification':None,
           'host_transfer':copy.deepcopy(transfer),'provider':copy.deepcopy(policy['provider']),
           'tunnels':copy.deepcopy(policy['tunnels']),'reverse_listener_process':{'pid':90},
           'controller':{'model':'mi27','workers':4,'output_root':roots[0],'process':copy.deepcopy(phases[0]['process']),
                'pythonpath':'/fixture/code','source_pins':copy.deepcopy(objects[bundle['path']]['files']),
                'input_pins':[jobs,config,extension,bundle,template_pin,holds],
                'queue_identity':{'path':roots[0]+'/.dispatcher/queue_identity.json'},
                'admission_control':{'path':roots[0]+'/.dispatcher/admission_control.json'}}}
    return policy, reg, objects, ref


def add_continuation(policy, reg, objects, ref):
    phase = copy.deepcopy(policy['phases'][-1]); phase.update(phase='continuous',mode='continuous',output_root='/fixture/runs/continuous',
        runtime_cohort='continuous',capacity_required=False)
    phase['process'] = {'pid':104,'process_starttime_ticks':'204','argv':['/usr/bin/python3','-m','causes.campaign_dispatcher','--workers','8','--output',phase['output_root']]}
    for flag,value in phase['controls'].items(): phase['process']['argv'].extend((flag,value))
    config = phase['controller_config']; prototypes = ref('prototypes.json',[{'complete':'payload','spec':{'task_id':'a','image':'image@sha256:abc'}}])
    control = {'schema':'causes.corpus_continuation_control.v1','family':'continuous','model_key':'mi27',
               'task_host':'provider','provider_host':'provider','repetition_start':1,'config':config,'prototypes':prototypes,
               'catalog':ref('catalog.json',{'cells':['whole catalog']}),'original_jobs':ref('original.json',[{'original':True}]),
               'reservation':ref('seed-ledger.json',{'seeds':[1,2,3]})}
    control_pin = ref('control.json',control)
    phase['process']['argv'].extend(('--corpus-continuation',control_pin['path'],'--corpus-continuation-sha256',control_pin['sha256']))
    source_names = ['campaign_dispatcher.py','corpus_allocation.py','corpus_continuation.py']
    bundle = ref('continuous-bundle.json',{'path':'/fixture/newcode','files':[{'path':'/fixture/newcode/'+name,'bytes':4,'sha256':'c'*64} for name in source_names]})
    template = {'phase':'continuous','runtime_cohort':'continuous','output_root':phase['output_root'],'proposed_workers':8,
                'argv_template_at_proposed_cap':['env','PYTHONPATH=/fixture/newcode',*phase['process']['argv']],
                'jobs':phase['controller_jobs'],'config':config,'prototypes':prototypes,'bundle':bundle}
    phase['controller_templates'] = ref('continuous-templates.json',{'templates':[template]})
    package = ref('source-package.json',{'tracked_sources_and_exact_snapshots':[{'snapshot':row} for row in objects[bundle['path']]['files']]})
    binding = {k:control[k] for k in ('family','model_key','task_host','provider_host','repetition_start')}
    binding.update(schema='causes.corpus_continuation_binding.v1',model_config_sha256=config['sha256'])
    for k,n in {'catalog':'catalog_sha256','prototypes':'prototype_set_sha256','original_jobs':'original_jobs_sha256','config':'config_object_sha256','reservation':'reservation_sha256'}.items():
        binding[n] = m.digest(m.encoded(objects[control[k]['path']]))
    phase['continuation'] = {'control':control_pin,'binding':binding,'package':package,
        'source_review':ref('source-review.json',{'accepted_JP_continuation_source':True,'package':package}),
        'allocator_sources':{name:'c'*64 for name in source_names}}
    phase['donor_histories'] = [{'host':'provider','output_root':p['output_root'],'mode':'sealed','recorder_relevant':True,'journal':ref('donor'+str(i),{})} for i,p in enumerate(policy['phases'])]
    policy['history'] = {'donor_scopes':[{k:row[k] for k in ('host','output_root','mode','recorder_relevant')} for row in phase['donor_histories']],
        'model_configs':[config],'model_aliases':['model:'+m.digest(m.encoded(['example','http://localhost/v1/chat/completions']))],
        'task_routes':['task:'+m.digest(m.encoded(['a','image@sha256:abc']))]}
    reg['prior_local_owners'] = [{'output_root':p['output_root'],'process':p['process']} for p in policy['phases']]
    policy['phases'].append(phase); policy['sync_roots'].append('runs/continuous')
    reg.update(phase='continuous',runtime_cohort='continuous',controller_templates=phase['controller_templates'],
               continuation=copy.deepcopy(phase['continuation']),donor_histories=copy.deepcopy(phase['donor_histories']))
    c=reg['controller'];c.update(workers=8,output_root=phase['output_root'],process=copy.deepcopy(phase['process']),pythonpath='/fixture/newcode',
        source_pins=copy.deepcopy(objects[bundle['path']]['files']),input_pins=[template[k] for k in ('jobs','config','prototypes','bundle')]+[phase['controller_templates'],policy['holds'],control_pin,*[control[k] for k in ('catalog','original_jobs','reservation')]],
        queue_identity={'path':phase['output_root']+'/.dispatcher/queue_identity.json'},admission_control={'path':phase['output_root']+'/.dispatcher/admission_control.json'})


class MonitorTests(unittest.TestCase):
    def test_exact_finite_and_continuous_scopes_keep_three_target_transfer(self):
        policy,reg,objects,ref=fixture(); reader=lambda p:copy.deepcopy(objects[p['path']])
        self.assertEqual(m.validate_registration(reg,policy,reader)['phase'],'q4')
        add_continuation(policy,reg,objects,ref)
        self.assertEqual(m.validate_registration(reg,policy,reader)['mode'],'continuous')
        self.assertEqual(len(policy['host_transfer']['target_output_roots']),3)
        self.assertEqual(len(policy['sync_roots']),4)
        policy['host_transfer']['target_output_roots'].append('/fixture/runs/continuous')
        with self.assertRaisesRegex(ValueError,'transfer_scope'):m.validate_policy(policy)

    def test_registration_rejects_owner_controls_or_provider_drift(self):
        policy,reg,objects,_=fixture();reader=lambda p:objects[p['path']]
        for mutate in (lambda r:r['controller']['process'].update(pid=999),
                       lambda r:r['controller'].update(workers=4.0),
                       lambda r:r['controller']['input_pins'].append({'path':'/fixture/controls/BARRIER'}),
                       lambda r:r['provider'].update(unit='other.service'),
                       lambda r:r['controller']['source_pins'].clear()):
            value=copy.deepcopy(reg);mutate(value)
            with self.assertRaises(ValueError):m.validate_registration(value,policy,reader)

    def test_whole_continuation_catalog_reservation_and_prior_history_required(self):
        policy,reg,objects,ref=fixture();add_continuation(policy,reg,objects,ref);reader=lambda p:objects[p['path']]
        for mutate in (lambda p,r:r['continuation']['binding'].update(prototype_set_sha256='0'*64),
                       lambda p,r:r['controller']['input_pins'].pop(),
                       lambda p,r:r['donor_histories'].pop(),
                       lambda p,r:r['prior_local_owners'].pop()):
            p,r=copy.deepcopy((policy,reg));mutate(p,r)
            with self.assertRaises(ValueError):m.validate_registration(r,p,reader)
        objects['/fixture/prototypes.json'][0]['complete']='changed entire prototype'
        with self.assertRaisesRegex(ValueError,'object_binding'):m.validate_registration(reg,policy,reader)

    def test_history_preserves_failures_per_donor_and_requalification_identity(self):
        def raw(rows):return b''.join(m.encoded(r)+b'\n' for r in rows)
        failed=raw([{'event':'route_failure','route':'model:old-endpoint','consecutive_failures':1}])
        other=raw([{'event':'route_success','routes':['model:new-endpoint','model:old-endpoint']}])
        self.assertEqual(m.replay_history(failed)['nonzero_failures'],{'model:old-endpoint':1})
        self.assertEqual(m.replay_history(other)['nonzero_failures'],{})
        same=m.replay_history(failed+other)
        self.assertEqual(same['lifetime_route_failures'],1);self.assertEqual(same['nonzero_failures'],{})
        self.assertEqual(m.history_blockers(m.replay_history(failed),['model:new-endpoint','model:old-endpoint'],[],False),['model:old-endpoint'])
        self.assertEqual(m.history_blockers(m.replay_history(failed),['model:new-endpoint'],[],False),[])
        with self.assertRaisesRegex(ValueError,'unbound_history'):
            m.replay_history(raw([{'event':'route_requalified','route':'r','quarantine_id':'invented'}]))
        claim={'event':'claimed','run_id':'id','job_sha256':'h'}
        with self.assertRaisesRegex(ValueError,'duplicate_or_conflicting'):
            m.replay_history(raw([claim,claim]))
        self.assertEqual(m.replay_history(failed+b'{torn')['unconsumed_tail_bytes'],5)

    def test_legacy_two_owner_source_and_freshness_guards_remain(self):
        policy,_,_,_=fixture();now=dt.datetime.now(dt.timezone.utc)
        status={'at':now.isoformat(),'alarms':{'source_or_controller_identity_mismatch':False},'source_identity':{
            'all_expected_identities_match':True,'kernel_profile_name_loaded':True,
            'overlay':{'authorized':True,'valid':True,'receipt':policy['legacy']['identity_receipt']},
            'controllers':[{**p,'live':True,'argv_matches':True,'starttime_matches':True} for p in policy['legacy']['owners']],
            'files':[{**r,'actual_sha256':r['expected_sha256'],'matches':True} for r in policy['legacy']['files']]}}
        m.validate_legacy(status,policy,now)
        for mutate in (lambda s:s['source_identity']['controllers'].append({'pid':3,'model':'jp'}),
                       lambda s:s['source_identity']['files'][0].update(actual_sha256='changed'),
                       lambda s:s.update(at=(now-dt.timedelta(seconds=181)).isoformat())):
            bad=copy.deepcopy(status);mutate(bad)
            with self.assertRaises(ValueError):m.validate_legacy(bad,policy,now)
        merged=m.merge_monitor(status,{'required':True,'valid':False,'alarms':{}})
        self.assertEqual(merged['source_identity'],status['source_identity'])
        self.assertTrue(merged['alarms']['companion_missing_or_invalid'])
        self.assertFalse(status['alarms']['source_or_controller_identity_mismatch'])
        with self.assertRaises(ValueError):m.merge_monitor(status,{})

    def test_snapshot_authorization_is_fresh_exact_and_detached(self):
        policy,_,_,_=fixture();config=SimpleNamespace(policy=policy)
        snapshot=SimpleNamespace(host='provider',remote_base='/fixture',roots=tuple(policy['sync_roots']))
        result={'valid':True,'alarms':{'companion_missing_or_invalid':False,'route_history_blocked':False},'required':True,'authorization':{'sha256':'retained'},'policy':{'sha256':'policy'},'registration':{'sha256':'reg'},'review':{'sha256':'review'}}
        with patch.object(m,'observe',return_value=result) as observe:
            auth=m.snapshot_authorizer(config,lambda:None)
            first=auth(snapshot);first['evidence']['authorization']['sha256']='mutated'
            result['at']='later';result['counters']={'finished':10}
            second=auth(snapshot)
            self.assertEqual(observe.call_count,2);self.assertEqual(second['evidence']['authorization']['sha256'],'retained')
            self.assertNotIn('at',second['evidence']);self.assertNotIn('counters',second['evidence'])
            snapshot.roots+=('runs/unregistered',)
            with self.assertRaisesRegex(ValueError,'scope'):auth(snapshot)
        snapshot.roots=tuple(policy['sync_roots'])
        with patch.object(m,'observe',return_value={'valid':False,'alarms':{}}):
            with self.assertRaisesRegex(ValueError,'refused'):m.snapshot_authorizer(config,lambda:None)(snapshot)

    def test_route_hold_retains_alarm_and_allows_completed_evidence_custody(self):
        policy,_,_,_=fixture();config=SimpleNamespace(policy=policy)
        snapshot=SimpleNamespace(host='provider',remote_base='/fixture',roots=tuple(policy['sync_roots']))
        result={'valid':True,'required':True,'alarms':{'companion_missing_or_invalid':False,'route_history_blocked':True},
                'authorization':{'sha256':'retained'},'policy':{'sha256':'policy'},'registration':{'sha256':'reg'},'review':{'sha256':'review'}}
        with patch.object(m,'observe',return_value=result):
            authorization=m.snapshot_authorizer(config,lambda:None)(snapshot)
            self.assertEqual(authorization['evidence']['registration'],result['registration'])
            self.assertTrue(m.merge_monitor({'alarms':{}},result)['alarms']['route_history_blocked'])
            self.assertTrue(result['alarms']['route_history_blocked'])
            result['alarms']['companion_missing_or_invalid']=True
            with self.assertRaisesRegex(ValueError,'refused'):m.snapshot_authorizer(config,lambda:None)(snapshot)

    def test_missing_policy_or_authorization_has_no_optional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'policy.json';policy,_,_,_=fixture();p.write_bytes(m.encoded(policy))
            ref={'path':str(p),'bytes':p.stat().st_size,'sha256':m.digest(p.read_bytes())}
            config=m.MonitorConfig(ref,root/'missing-auth.json')
            p.unlink()
            with patch.object(m,'remote') as remote:
                observed=m.observe(config,lambda:None)
                self.assertFalse(observed['valid']);self.assertTrue(observed['required']);remote.assert_not_called()

    def test_remote_history_replay_is_same_function_and_source_is_portable(self):
        remote=ast.parse(m.REMOTE)
        source=ast.parse(Path(m.__file__).read_text())
        for name in ('replay_history','history_blockers','continuous_progress'):
            find=lambda tree:next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            self.assertEqual(ast.dump(find(remote)),ast.dump(find(source)))
        source_text = Path(m.__file__).read_text()
        for prefix in ('/home/', '/root/'):
            self.assertNotIn(prefix, source_text)
        self.assertNotRegex(source_text, r'https?://(?:127\.0\.0\.1|localhost):\d+')
        compile(m.REMOTE,'monitor_remote','exec')

    def test_continuous_waiting_owner_has_empty_queue_and_no_blocks_or_claims(self):
        continuation={'control':{'sha256':'c'},'binding':{'catalog_sha256':'d'}}
        identity={'jobs_sha256':m.digest(m.encoded([])),'corpus_continuation':copy.deepcopy(continuation)}
        state=m.continuous_progress(identity,continuation,[],[],0,True)
        self.assertEqual(state['initial_jobs'],0);self.assertFalse(state['first_block_observed'])
        for rows,count in (([{'event':'claimed'}],0),([{'event':'extension_block_created'}],1),([],1)):
            with self.assertRaisesRegex(ValueError,'issued_work'):
                m.continuous_progress(identity,continuation,[],rows,count,True)
        active=m.continuous_progress(identity,continuation,[],[{'event':'extension_block_created'},{'event':'claimed'}],1,False)
        self.assertTrue(active['first_block_observed']);self.assertEqual(active['claimed'],1)
        with self.assertRaisesRegex(ValueError,'must_be_empty'):
            m.continuous_progress(identity,continuation,[{'id':'old frozen job'}],[],0,True)


if __name__ == '__main__':
    unittest.main()
