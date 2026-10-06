import copy
import datetime as dt
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from causes import campaign_admission as a
from causes import campaign_handoff as h
from tests.causes.test_corpus_continuation import fixture


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()

    def save(self,name,value,raw=False):
        path=self.base/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(value if raw else h.encoded(value)+b'\n')
        return h.pin(path)

    def test_source_check_also_works_without_qualified_self_module_alias(self):
        expected={name:h.pin(module.__file__)['sha256'] for name,module in
                  [('causes.campaign_admission',a),('causes.campaign_handoff',h)]}
        with patch.dict(sys.modules):
            del sys.modules['causes.campaign_admission']
            a.loaded_sources_match(expected)
            with self.assertRaisesRegex(ValueError,'loaded_operator_module_changed'):
                a.loaded_sources_match({**expected,'causes.campaign_handoff':'0'*64})

    def reservation(self):
        return dict(schema='causes.corpus_allocation_reservation.v1',run_ids=['old'],allocation_seeds=[1,2,3,4],
                    allocation_identities=[dict(run_id='old',seed=1,block_seed=2,task_seed=3,model_seed=4)],
                    frozen_input_pins=[],host_cuts=[dict(host='fixture',started_at=h.now(),finished_at=h.now())],
                    capture_pins=[],persisted_full_blocks=[])

    def test_new_attempt_metadata_preserves_reservation_but_new_block_or_typed_seed_cannot(self):
        old=self.reservation();new=copy.deepcopy(old)
        new['capture_pins']=[{'sha256':'a different metadata cut'}]
        self.assertTrue(a.reservation_unchanged(old,new))
        for mutate in (lambda r:r['allocation_identities'][0].update(seed=1.0),
                       lambda r:r['allocation_identities'][0].update(seed=5),
                       lambda r:r['allocation_identities'].append(dict(run_id='new',seed=5)),
                       lambda r:r.update(allocation_seeds=[1,2,3,4,5])):
            value=copy.deepcopy(new);mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):a.reservation_unchanged(old,value)
        new['host_cuts'][0]['started_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat()
        with self.assertRaisesRegex(ValueError,'stale'):a.reservation_unchanged(old,new)

    def finite(self):
        jobs=[{'allocation':{'run_id':str(i)},'fixture':i} for i in range(2)]
        config={'model':'fixture'}
        rows=[{'event':'claimed','run_id':str(i),'job_sha256':a.object_sha(job),
               'config_sha256':a.object_sha(config)} for i,job in enumerate(jobs)]
        # Finite exhaustion can precede the last final while the last batch drains.
        rows += [{'event':'waiting_for_operator_pause_no_eligible_extensions'},
                 {'event':'episode_finished','run_id':'0'},
                 {'event':'route_failure','route':'model:fixture','consecutive_failures':1},
                 {'event':'episode_finished','run_id':'1'},
                 {'event':'route_success','routes':['model:fixture']}]
        custody={'completed_ids':['0','1'],'summaries':{str(i):self.save(str(i)+'/summary.json',{'closed':True}) for i in range(2)}}
        return jobs,config,rows,custody

    def test_terminal_custody_keeps_diagnosed_failure_and_exhaustion_chronology(self):
        jobs,config,rows,custody=self.finite()
        raw=b''.join(h.encoded(r)+b'\n' for r in rows)
        result=a.finite_terminal_identity(jobs,config,raw,custody)
        self.assertEqual(result,dict(assigned=2,claims=2,finals=2,lifetime_route_failures=1))
        for changed in (rows[:-2]+rows[-1:],rows+[rows[0]],rows+[{'event':'unresolved_summary'}],
                        rows[2:]+rows[:2]):
            with self.assertRaises(ValueError):
                a.finite_terminal_identity(jobs,config,b''.join(h.encoded(r)+b'\n' for r in changed),custody)
        with self.assertRaisesRegex(ValueError,'torn'):a.finite_terminal_identity(jobs,config,raw[:-1],custody)
        Path(custody['summaries']['0']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'pin_changed'):a.finite_terminal_identity(jobs,config,raw,custody)

    def test_donors_cannot_clear_one_another_and_live_prefix_is_immutable(self):
        scopes=[dict(host='fixture',output_root='/fixture/'+r,mode='live_prefix',recorder_relevant=True) for r in ('one','two')]
        policy=dict(donor_scopes=scopes,model_aliases=['model:fixture'],task_routes=[])
        raw=h.encoded({'event':'route_failure','route':'model:fixture','consecutive_failures':1})+b'\n'
        baseline=[];current=[]
        for i,scope in enumerate(scopes):
            prefix=raw if i==0 else b'{"event":"controller_started"}\n'
            ref={'path':scope['output_root']+'/.dispatcher/journal.jsonl','bytes':len(prefix),'sha256':h.pin(self.save_bytes(str(i),prefix))['sha256']}
            baseline.append(dict(scope,journal=ref))
            full=prefix if i==0 else prefix+h.encoded({'event':'route_success','routes':['model:fixture']})+b'\n'
            cap=self.save('cut'+str(i),full,raw=True)
            current.append(dict(scope,journal={**cap,'path':ref['path']},capture=cap,at=h.now()))
        with self.assertRaisesRegex(ValueError,'requires_reconciliation'):a.donor_cuts_clear(policy,baseline,current)
        full=raw+h.encoded({'event':'route_success','routes':['model:fixture']})+b'\n'
        cap=self.save('cleared',full,raw=True);current[0].update(capture=cap,journal={**cap,'path':baseline[0]['journal']['path']})
        self.assertTrue(a.donor_cuts_clear(policy,baseline,current))
        baseline[0]['journal']['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'prefix_changed'):a.donor_cuts_clear(policy,baseline,current)

    def save_bytes(self,name,raw):
        return Path(self.save(name,raw,raw=True)['path'])

    def test_resources_are_derived_from_prototypes_with_explicit_reserves(self):
        decision=dict(new_workers=8,existing_task_workers=0,effective_cpu_budget=16,os_provider_cpu_reserve=8,
                      os_provider_ram_reserve_bytes=32,disk_floor_bytes=112,disk_growth_reserve_bytes=32)
        measured=dict(cpu_budget=16,memory_limit_bytes=64,memory_available_bytes=64,root_available_bytes=144)
        prototypes=[{'spec':{'cpus':1,'memory_bytes':4}}]
        self.assertTrue(a.resource_check(decision,measured,prototypes,8))
        for field in ('cpu_budget','memory_available_bytes','root_available_bytes'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                a.resource_check(decision,{**measured,field:measured[field]-1},prototypes,8)
        with self.assertRaisesRegex(ValueError,'nonempty'):a.resource_check(decision,measured,[],8)

    def test_reviewed_template_only_allows_declared_dynamic_fields(self):
        ref={'path':'/fixture/template','bytes':1,'sha256':'x'}
        template=dict(schema='causes.continuous_admission_policy_template.v1',fixed={'source_sha256':'s','workers':8},dynamic_keys=['owner'])
        policy={'policy_template':ref,'source_sha256':'s','workers':8,'owner':{'pid':1}}
        review=dict(accepted_continuous_admission_source=True,accepted_continuous_admission_policy_template=True,policy_template=ref,source={'sha256':'s'})
        a.reviewed_policy(policy,template,review)
        for changed in ({**policy,'workers':8.0},{**policy,'unexpected':'field'},{**policy,'source_sha256':'changed'}):
            with self.assertRaises(ValueError):a.reviewed_policy(changed,template,review)

    def test_pure_binding_uses_whole_catalog_and_never_creates_a_block(self):
        data=fixture();refs={k:self.save(k+'.json',data[k]) for k in ('catalog','prototypes','original_jobs','config','reservation')}
        control=dict(schema='causes.corpus_continuation_control.v1',**refs,
                     **{k:data[k] for k in ('family','model_key','task_host','provider_host')},repetition_start=1)
        cref=self.save('CONTROL.json',control)
        names=('campaign_dispatcher.py','campaign_runner.py','campaign_sandbox.py','corpus_allocation.py','corpus_continuation.py')
        bundle=self.save('BUNDLE.json',{'files':[{'name':n,'sha256':n} for n in names]})
        tref=self.save('TEMPLATES.json',{'templates':[dict(jobs=self.save('EMPTY.json',[]),config=refs['config'],
                       prototypes=refs['prototypes'],bundle=bundle,argv_template_at_proposed_cap=['env','PYTHONPATH=fixture','python'])]})
        fixed=dict(allocator_package={},allocator_review={},workers=8,dedicated_pause='/fixture/PAUSE',
                   global_pause='/PAUSE',barrier='/fixture/BARRIER',holds={})
        dynamic=['controller_template','normalized_template','argv','continuation','expected_queue',
                 'expected_controller_fields','donor_histories','runtime_input_pins']
        pref=self.save('TEMPLATE.json',dict(fixed=fixed,dynamic_keys=dynamic))
        before=copy.deepcopy(data)
        with patch.object(a.CorpusContinuation,'block',side_effect=AssertionError('allocation not authorized')):
            policy=a.bind_policy(pref,cref,tref,[])
        self.assertEqual(policy['expected_queue']['jobs_sha256'],a.object_sha([]))
        self.assertEqual(policy['continuation']['binding']['prototype_set_sha256'],a.object_sha(data['prototypes']))
        self.assertEqual(data,before)
        damaged=h.checked(refs['prototypes']);damaged[0]['variant']['request']='changed'
        Path(refs['prototypes']['path']).write_bytes(h.encoded(damaged))
        with self.assertRaises(ValueError):a.bind_policy(pref,cref,tref,[])

    def waiting(self):
        root=self.base/'waiting';(root/'.dispatcher/controllers/one').mkdir(parents=True)
        barrier=self.save('ADMISSION_BARRIER',{'owned':True})
        policy=dict(output_root=str(root),barrier=barrier['path'],expected_queue={'jobs':[]},
                    expected_admission_control={'holds':'fixture'},expected_controller_fields={'workers':8})
        for path,value in [('queue_identity.json',policy['expected_queue']),('admission_control.json',policy['expected_admission_control']),
                           ('controllers/one/identity.json',policy['expected_controller_fields'])]:
            self.save('waiting/.dispatcher/'+path,value)
        rows=[{'event':'controller_started'},{'event':'admission_holds_bound'},
              {'event':'admission_barrier_waiting','active':0,'pending':0}]
        self.save('waiting/.dispatcher/journal.jsonl',b''.join(h.encoded(r)+b'\n' for r in rows),raw=True)
        return policy,{'pid':1},root

    def test_waiting_owner_rejects_first_claim_block_and_changed_controller(self):
        policy,process,root=self.waiting()
        with patch.object(h,'process_identity'),patch.object(a,'exact_children_absent'),patch.object(h,'lock_owned_by',return_value=True):
            self.assertTrue(a.waiting_identity(policy,process)['zero_initial_claims'])
            blocks=root/'.dispatcher/blocks';blocks.mkdir();(blocks/'000000000000.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'created_block'):a.waiting_identity(policy,process)
            (blocks/'000000000000.json').unlink()
            with (root/'.dispatcher/journal.jsonl').open('ab') as stream:stream.write(b'{"event":"claimed"}\n')
            with self.assertRaisesRegex(ValueError,'already_issued'):a.waiting_identity(policy,process)

    def test_start_after_retirement_failure_retains_attempt_and_cannot_repeat(self):
        control=self.base/'controls';control.mkdir()
        launch=self.save('old/LAUNCH.json',{'process':{'pid':1}})
        custody=self.save('CUSTODY.json',{'roots':{'/old':{'journal':{'path':'/old/journal'}}}})
        policy=dict(control_root=str(control),handoff_lock=str(control/'handoff.lock'),attempt_root=str(control/'attempt'),
                    output_root=str(self.base/'new'),barrier=str(control/'ADMISSION_BARRIER'),family='fixture',
                    predecessors=[dict(output_root='/old',launch=launch)])
        pref=self.save('POLICY.json',policy);iref=self.save('INPUTS.json',{'prior_custody':{'evidence':custody}})
        operator=a.ContinuousAdmission(pref,iref,{})
        authority=Mock();authority.reference={}
        with patch.object(operator,'authority',return_value=authority),patch.object(h,'retire_exact_idle_owner',return_value={'retired':True}) as retire,\
             patch.object(h,'create_owned_barrier',side_effect=OSError('fixture publication failure')),patch.object(a.subprocess,'Popen') as spawn:
            with self.assertRaises(OSError):operator.start(execute=True)
            with self.assertRaisesRegex(ValueError,'namespace_exists'):operator.start(execute=True)
            retire.assert_called_once();spawn.assert_not_called()
        failure=json.loads((control/'attempt/FAILED.json').read_bytes())
        self.assertTrue(failure['manual_reconciliation_required'])
        self.assertIsNone(failure['spawn_returned_pid'])


if __name__=='__main__':unittest.main()
