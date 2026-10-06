import base64
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_admission as a
from causes import campaign_admission_evidence as e
from causes import campaign_census as c
from causes import campaign_handoff as h


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.cfg=dict(capture_base=str(self.base),max_file_bytes=1024**2,max_total_bytes=4*1024**2,max_files=32,
                      native_host='native',transfer_host='legacy',remote_base=str(self.base/'remote'),
                      snapshot_destination=str(self.base/'snapshots'),transport_timeout_seconds=10,
                      remote_pythonpath=str(self.base/'code'),remote_python='python3')

    def save(self,name,value,raw=False):
        path=self.base/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(value if raw else h.encoded(value)+b'\n');return h.pin(path)

    def map_for(self,remote,raw):
        local=self.save('retained/'+Path(remote['path']).name,raw,True)
        return self.save('MAP.json',{'files':[dict(remote=remote,local=local)]})

    def fixture(self):
        root=str(self.base/'remote/runs/finite/model');rid='case_1'
        job=dict(allocation=dict(run_id=rid,seed=1),spec=dict(task_id='one',cpus=1),variant=dict(id='one',request='fixture'))
        config={'model':'fixture'}
        jobs_ref=self.save('jobs.json',[job]);config_ref=self.save('config.json',config)
        relative='runs/finite/model/'+rid;attempt=self.base/'snapshots/attempts/cut';folder=attempt/'data'/relative
        summary={'run_id':rid,'sandbox_closure':dict(state='closed',overlay_mounted=False),
                 'probes':[dict(stage='early',instrument='presence',technical_status='invalid_answer_schema'),
                           dict(stage='end',instrument='presence',technical_status='complete')]}
        values={'allocation.json':job['allocation'],'spec.json':job['spec'],'variant.json':job['variant'],
                'model.json':config,'summary.json':summary};pins={};files=[]
        for name,value in values.items():
            ref=self.save(str((folder/name).relative_to(self.base)),value);pins[name]=ref
            files.append({**ref,'path':name})
        manifest=self.save(str((attempt/'MANIFEST.json').relative_to(self.base)),dict(host='native',base=self.cfg['remote_base'],
            runs=[dict(run=relative,files=files,bytes=sum(r['bytes'] for r in files),summary_sha256=pins['summary.json']['sha256'])]))
        self.save(str((attempt/'REQUEST.json').relative_to(self.base)),dict(host='native',base=self.cfg['remote_base'],roots=['runs/finite/model']))
        entry=dict(run=relative,path=str(folder),manifest=manifest['path'],manifest_sha256=manifest['sha256'],files=5,
                   bytes=sum(r['bytes'] for r in files),summary_sha256=pins['summary.json']['sha256'])
        self.save(str((attempt/'VERIFIED.json').relative_to(self.base)),dict(runs=[entry]))
        index=self.save('INDEX_PREFIX.jsonl',h.encoded(entry)+b'\n',True)
        journal=[dict(event='claimed',run_id=rid,job_sha256=a.object_sha(job),config_sha256=a.object_sha(config)),
                 dict(event='waiting_for_operator_pause_no_eligible_extensions'),
                 dict(event='episode_finished',run_id=rid,summary_sha256=pins['summary.json']['sha256']),
                 dict(event='route_failure',route='model:one',consecutive_failures=1)]
        raw=b''.join(h.encoded(r)+b'\n' for r in journal)
        remote=dict(path=root+'/.dispatcher/journal.jsonl',bytes=len(raw),sha256=e.hashlib.sha256(raw).hexdigest())
        mapping=self.map_for(remote,raw)
        policy=dict(predecessors=[dict(output_root=root,jobs=jobs_ref,config=config_ref,launch={'path':'retained-original-launch'})],
                    host_transfer={'target_output_roots':[root]})
        return dict(root=root,rid=rid,job=job,config=config,index=index,entry=entry,remote=remote,mapping=mapping,policy=policy,pins=pins)

    def terminal(self,fixture):
        with patch.object(e,'settings',return_value=(self.cfg,fixture['policy'])):
            return e.terminal_custody({'configuration':'fixture'},fixture['index'],{fixture['root']:fixture['remote']},fixture['mapping'])

    def test_closed_technical_failure_is_preserved_in_complete_terminal_custody(self):
        f=self.fixture();value=self.terminal(f);row=value['roots'][f['root']]
        self.assertTrue(value['all_expected_terminal_custody']);self.assertTrue(value['technical_missingness_preserved'])
        self.assertEqual(row['completed_ids'],[f['rid']]);self.assertEqual(row['terminal_counts']['lifetime_route_failures'],1)
        self.assertEqual(row['technical_missingness'][0]['technical']['probe_statuses'][0]['technical_status'],'invalid_answer_schema')
        self.assertEqual(row['technical_missingness'][0]['technical']['probe_statuses'],[
            {'stage':'early','instrument':'presence','technical_status':'invalid_answer_schema'},
            {'stage':'end','instrument':'presence','technical_status':'complete'}])
        self.assertEqual(row['summaries'][f['rid']]['path'],f['root']+'/'+f['rid']+'/summary.json')
        self.assertEqual(row['summaries'][f['rid']]['sha256'],f['pins']['summary.json']['sha256'])

    def test_duplicate_or_missing_custody_and_typed_frozen_drift_refuse(self):
        f=self.fixture()
        for rows in ([],[f['entry'],f['entry']]):
            changed=copy.deepcopy(f);changed['index']=self.save('INDEX_BAD.jsonl',b''.join(h.encoded(r)+b'\n' for r in rows),True)
            with self.assertRaisesRegex(ValueError,'membership'):self.terminal(changed)
        changed=copy.deepcopy(f);changed['job']['allocation']['seed']=1.0
        changed['policy']['predecessors'][0]['jobs']=self.save('different_jobs.json',[changed['job']])
        with self.assertRaisesRegex(ValueError,'frozen_identity'):self.terminal(changed)

    def test_missing_final_refuses_even_when_a_closed_snapshot_exists(self):
        f=self.fixture();row,raw=e.mapped(f['mapping'],f['remote'])
        records=[json.loads(line) for line in raw.splitlines()];records=[r for r in records if r['event']!='episode_finished']
        raw=b''.join(h.encoded(r)+b'\n' for r in records);f['remote']=dict(f['remote'],bytes=len(raw),sha256=e.hashlib.sha256(raw).hexdigest())
        f['mapping']=self.map_for(f['remote'],raw)
        with self.assertRaisesRegex(ValueError,'membership_incomplete'):self.terminal(f)

    def test_capture_preserves_exact_pin_and_rejects_torn_journal_and_duplicate_map(self):
        ref=self.save('remote/journal',b'{"event":"fixture"}\n',True);capture=e.FileCapture(self.cfg,'native')
        row=capture.take(ref['path'],ref,journal=True)
        self.assertEqual(row['remote'],ref);self.assertEqual(row['unconsumed_tail_bytes'],0)
        out=self.base/'out';out.mkdir();mapped=e.retain_capture({'files':[row]},out,self.cfg)
        self.assertEqual(e.mapped(mapped,ref)[1],h.checked_bytes(ref))
        out2=self.base/'out2';out2.mkdir()
        with self.assertRaisesRegex(ValueError,'duplicate'):e.retain_capture({'files':[row,row]},out2,self.cfg)
        torn=self.save('remote/torn',b'{"event":',True)
        with self.assertRaisesRegex(ValueError,'torn'):e.FileCapture(self.cfg,'native').take(torn['path'],journal=True)

    def test_transport_timeout_retains_partial_bytes_and_never_retries(self):
        request=dict(configuration={'fixture':True},host='native',action='foundation',stage='before_start')
        out=self.base/'transport'
        with patch.object(e,'accepted_source',return_value=(self.cfg,{})),patch.object(e.subprocess,'run',
                side_effect=subprocess.TimeoutExpired(['ssh'],10,output=b'partial',stderr=b'cut')) as run:
            with self.assertRaisesRegex(ValueError,'no_retry'):e.capture(request,{'review':'fixture'},out)
        self.assertEqual(run.call_count,1);execution=json.loads((out/'EXECUTION.json').read_bytes())
        self.assertTrue(execution['remote_completion_unknown']);self.assertTrue(execution['timed_out'])
        self.assertEqual(h.checked_bytes(execution['stdout']),b'partial');self.assertFalse((out/'REPORT.json').exists())

    def test_census_adapter_delegates_exact_host_and_source_without_composing(self):
        host=dict(host='native',base=str(self.base/'remote'),roots=['runs/one/model'],top_jobs=[],control_directories=[],
            limits=dict(max_file_bytes=4096,max_total_bytes=4096,max_files=4,max_directory_entries=8,max_allocations=8))
        self.save('remote/runs/one/model/id/allocation.json',dict(run_id='id',seed=7,block_seed=8,task_seed=9,model_seed=10))
        config=self.save('census.json',dict(hosts=[host],collector_sha256=h.pin(c.__file__)['sha256']))
        fixed={'census_configs':{'before_start':config,'waiting_release':config}}
        with patch.object(e,'settings',return_value=(self.cfg,fixed)),patch.object(c,'compose_reservation',side_effect=AssertionError('no composition')):
            actual=e.collect(dict(configuration={},action='census',host='native',census_config=config))
        self.assertEqual(actual['collector_source'],h.pin(c.__file__))
        self.assertEqual(actual['files'][0]['allocations'][0]['seed'],7)
        self.assertEqual(actual['files'][0]['allocations'][0]['run_id'],'id')

    def test_capture_report_rejects_changed_projection(self):
        row={'host':'native','remote':self.save('remote/one.json',{'one':1})}
        row['base64']=base64.b64encode(h.checked_bytes(row['remote'])).decode()
        wire=dict(action='foundation',host='native',at=h.now(),read_only=True,files=[row],observation={'valid':True})
        out=self.base/'capture';out.mkdir();mapping=e.retain_capture(wire,out,self.cfg)
        request=self.save('request.json',dict(configuration={},host='native',action='foundation'))
        intent=self.save('intent.json',dict(request=request));stdout=self.save('stdout',h.encoded(wire),True)
        execution=self.save('execution.json',dict(returncode=0,timed_out=False,remote_completion_unknown=False,
            intent=intent,stdout=stdout,stderr=self.save('stderr',b'',True)))
        report={k:v for k,v in wire.items() if k!='files'};report.update(configuration={},receipt_map=mapping,execution=execution)
        good=self.save('report.json',report);self.assertEqual(e.captured_report(good),report)
        report['observation']['valid']=False;bad=self.save('bad_report.json',report)
        with self.assertRaisesRegex(ValueError,'projection_changed'):e.captured_report(bad)

    def test_waiting_capture_retains_original_marker_and_archive_in_one_unique_map(self):
        policy=self.save('policy.json',{'policy_template':{'fixture':True}})
        marker=self.save('remote/controls/ADMISSION_BARRIER',b'owned marker\n',True)
        archive=self.save('remote/receipts/ARCHIVE',b'owned marker\n',True)
        creation=self.save('remote/receipts/CREATE.json',{})
        authority=self.save('remote/receipts/AUTHORITY.json',{})
        owned=self.save('remote/receipts/OWNED.json',dict(marker=marker,archived_marker=archive,
            creation_intent=creation,creation_authority=authority))
        intent=self.save('remote/receipts/INTENT.json',{})
        retirement=self.save('remote/receipts/RETIREMENT.json',dict(intent=intent))
        process=self.save('remote/receipts/PROCESS.json',{})
        queue=self.save('remote/receipts/QUEUE.json',{})
        journal=self.save('remote/runs/waiting/model/.dispatcher/journal.jsonl',b'{"event":"admission_barrier_waiting"}\n',True)
        launch=self.save('remote/receipts/LAUNCH.json',dict(process_receipt=process,intent=intent,owned_barrier=owned,
            retirement=retirement,queue_identity=queue,admission_control=queue,controller_identity=queue))
        observation=dict(at=h.now(),journal=journal,zero_initial_claims=True,zero_persisted_blocks=True,
                         process={'pid':1},owned_barrier=owned)
        with patch.object(a,'observe_waiting',return_value=observation) as observe:
            value=e.collect_waiting(dict(self.cfg,policy_template={'fixture':True}),policy,launch)
        self.assertEqual(observe.call_args.args,(policy,launch));out=self.base/'wait_capture';out.mkdir()
        mapping=e.retain_capture(value,out,self.cfg)
        self.assertEqual(e.mapped(mapping,marker)[1],e.mapped(mapping,archive)[1])
        self.assertEqual(json.loads(e.mapped(mapping,launch)[1]),h.checked(launch))
        refs=[r['remote']['path'] for r in h.checked(mapping)['files']]
        self.assertEqual(len(refs),len(set(refs)))

    def test_index_cut_retains_actual_tail_without_scanning_live_episodes(self):
        raw=b'{"run":"old/id"}\n'+b'{"partial":'
        self.save('snapshots/accepted.jsonl',raw,True)
        with patch.object(e,'settings',return_value=(self.cfg,{})):
            report=e.index_prefix({},self.base/'index_cut')
        value=h.checked(report)
        self.assertEqual(h.checked_bytes(value['prefix']),b'{"run":"old/id"}\n')
        self.assertEqual(value['unconsumed_tail_bytes'],len(b'{"partial":'))
        self.assertEqual(value['bytes_read'],len(raw))

    def test_accepted_native_request_without_host_and_public_explicit_host(self):
        f=self.fixture();request=Path(f['entry']['manifest']).parent/'REQUEST.json'
        row=json.loads(request.read_bytes());row.pop('host');request.write_bytes(h.encoded(row))
        self.assertEqual(self.terminal(f)['roots'][f['root']]['completed_ids'],[f['rid']])
        row['host']='another_host';request.write_bytes(h.encoded(row))
        with self.assertRaisesRegex(ValueError,'snapshot_host_scope_changed'):self.terminal(f)

    def test_terminal_final_requires_exact_copied_summary_digest(self):
        f=self.fixture();raw=e.mapped(f['mapping'],f['remote'])[1]
        rows=[json.loads(line) for line in raw.splitlines()]
        for row in rows:
            if row['event']=='episode_finished':row['summary_sha256']='0'*64
        changed=b''.join(h.encoded(row)+b'\n' for row in rows)
        f['remote']=dict(f['remote'],bytes=len(changed),sha256=e.hashlib.sha256(changed).hexdigest())
        f['mapping']=self.map_for(f['remote'],changed)
        with self.assertRaisesRegex(ValueError,'terminal_final_summary_hash_changed'):self.terminal(f)

    def test_foundation_produces_exact_consumer_fields_and_refuses_mixed_stages(self):
        now=h.now();process=dict(pid=123,process_starttime_ticks='1',argv=['fixture'])
        reg=self.save('registration.json',{'tunnels':[{'process':process}]})
        cfg=dict(self.cfg,foundation_registration=reg,policy_template={'template':True},
                 monitor_policy=self.save('monitor_policy.json',{}),donor_scopes=[])
        reports={};baselines=[]
        for host in ('legacy','native'):
            root=str(self.base/'remote/runs'/host/'model');scope=dict(host=host,output_root=root,mode='sealed',recorder_relevant=True)
            cfg['donor_scopes'].append(scope);raw=b'{"event":"route_success","routes":["model:fixture"]}\n'
            local=self.save(host+'/journal',raw,True);remote=dict(local,path=root+'/.dispatcher/journal.jsonl')
            mapping=self.save(host+'/map.json',{'files':[dict(remote=remote,local=local)]})
            baselines.append(dict(scope,journal=remote))
            reports[host]=dict(action='foundation',host=host,configuration={},read_only=True,started_at=now,at=now,
                stage='before_start',receipt_map=mapping,execution=self.save(host+'/execution.json',{}),
                donor_cuts=[dict(scope,journal=remote,at=now)])
        fixed=dict(host_transfer={'target_output_roots':['q4','q8','main']},provider={'process':process},legacy_owners=['kept'],
            model='fixture',max_model_len=100,workers=8,history={'donor_scopes':cfg['donor_scopes'],
            'model_aliases':['model:fixture'],'task_routes':[]},resource_decision=dict(new_workers=8,existing_task_workers=0,
            effective_cpu_budget=16,os_provider_cpu_reserve=8,os_provider_ram_reserve_bytes=32*1024**3,
            disk_growth_reserve_bytes=32*1024**3,disk_floor_bytes=112*1024**3))
        prototypes=self.save('prototypes.json',[{'spec':dict(cpus=1,memory_bytes=4*1024**3)}])
        fixed['control_template']=self.save('control_template.json',dict(prototypes=prototypes))
        reports['native']['observation']=dict(provider=process,no_owned_active_work=True,resources=dict(cpu_budget=20,
            memory_limit_bytes=128*1024**3,memory_available_bytes=128*1024**3,root_available_bytes=200*1024**3))
        reports['legacy']['observation']=dict(original_transfer_present=True,unused_MI_output_absent=True)
        refs={host:self.save(host+'/report.json',{}) for host in reports}
        policy=self.save('policy.json',dict(policy_template=cfg['policy_template'],donor_histories=baselines))
        legacy=self.save('legacy_status.json',{'at':now})
        relay=self.save('relay.json',dict(started_at=now,at=now,configuration={},registration=reg,read_only=True,
            tunnels=[process],listener={'pid':123},model={'model':'fixture','max_model_len':100}))
        def supplied(ref):return reports[next(k for k,v in refs.items() if v==ref)]
        with patch.object(e,'settings',return_value=(cfg,fixed)),patch.object(e,'captured_report',side_effect=supplied),\
             patch.object(e.monitor,'validate_legacy'):
            value=e.foundation({},policy,legacy,relay,refs)
            self.assertTrue(value['native_no_active_owned_work']);self.assertEqual(value['host_transfer'],fixed['host_transfer'])
            self.assertEqual(value['at'],now);self.assertEqual([r['host'] for r in value['donor_cuts']],['legacy','native'])
            reports['native']['stage']='waiting_release'
            with self.assertRaisesRegex(ValueError,'stages_disagree'):e.foundation({},policy,legacy,relay,refs)


if __name__=='__main__':unittest.main()
