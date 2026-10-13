import copy
import fcntl
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
from causes import campaign_supplement as core
from causes import campaign_supplement_finite as finite
from causes.campaign_monitor import replay_history
from tests.causes import test_campaign_supplement_co_residency as fixture


class FiniteSupplementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.count = 0; self.calls = []
        self.wait_after_first = self.mutate_source = self.failure = False
        self.guard = fixture.CoResidencyTests(methodName='runTest')
        self.guard.setUp(); self.addCleanup(self.guard.doCleanups)
        self.policy = copy.deepcopy(self.guard.policy)
        self.contract = copy.deepcopy(self.guard.contract)
        self.output = self.root/'remaining'
        self.policy.update(phase='continue', output_root=str(self.output),
                           local_pause_paths=[str(self.output/'PAUSE')],
                           local_free_space_floor_bytes=1)
        self.jobs = [dict(family='fixed', model='model', run_id=f'run{i}', stage='early',
                         question_id=q, measurement_id=f'fixed/model/run{i}/early/{q}',
                         seed=i*2+n, request_body_sha256=f'exact-{i}-{n}')
                     for i in range(4) for n,q in enumerate(('strong','lean'))]
        for job in self.jobs:
            payload = {'measurement_id': job['measurement_id'], 'seed': job['seed']}
            job['request_file_sha256'] = core.hashlib.sha256(core.encoded(payload)+b'\n').hexdigest()
        self.partition = dict(schema=finite.PARTITION_SCHEMA,
                              source_jobs=self.save('original.json',self.jobs),
                              remaining_requests=4,roles={})
        self.descriptors = {}
        for role,pilot,remaining in [('a',[0,1],[4,5]),('b',[2,3],[6,7])]:
            self.partition['roles'][role] = dict(max_clients=1,ordinals=remaining,pilot_ordinals=pilot,
                jobs=self.save(role+'.jobs.json',[self.jobs[i]for i in remaining]),
                pilot_jobs=self.save(role+'.pilots.json',[self.jobs[i]for i in pilot]))
            oldout=self.root/('pilot-'+role);core.mkdir(oldout)
            result=core.run_queue(oldout,[self.jobs[i]for i in pilot],self.worker,
                lambda:{'fixture':True},lambda unused:None,[],family='fixed',model='model',
                max_clients=1,local_free_space_floor_bytes=1)
            finals=[dict(measurement_id=self.jobs[i]['measurement_id'],
                final=core.pin(core.measurement_path(oldout,self.jobs[i],family='fixed',model='model')/'FINAL.json'))for i in pilot]
            actual=self.save(role+'.closed.json',dict(role=role,output=str(oldout),
                complete_ids=[self.jobs[i]['measurement_id']for i in pilot],terminal=2,
                active=0,unstarted_ids=[],ambiguous=[],all_technical_complete=True,
                full_pilot_qualified=True,journal=core.pin(oldout/'.control/journal.jsonl'),
                result=self.save(role+'.result.json',result),
                verified_finals=self.save(role+'.verified.json',finals)))
            peer=self.save(role+'.peer.json',dict(accepted_fixture_closure=True,actual=actual,findings=[]))
            self.descriptors[role]=dict(actual=actual,review=peer,acceptance_flag='accepted_fixture_closure')
        self.partition_ref=self.save('partition.json',self.partition)
        self.policy['pilot_measurement_ids']=[self.jobs[i]['measurement_id']for i in [0,1]]
        self.calls.clear();core.mkdir(self.output/'.control')
        self.lock=(self.output/'.control/executor.lock').open('ab');self.addCleanup(self.lock.close)
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        disk=mock.patch.object(core.shutil,'disk_usage',return_value=types.SimpleNamespace(free=100*1024**3))
        disk.start();self.addCleanup(disk.stop)

    def save(self,name,value):
        path=self.root/name;core.save(path,value);return core.pin(path)

    def changed(self,value):
        self.count+=1;return self.save(f'changed-{self.count}.json',value)

    def worker(self,job,destination):
        self.calls.append(job['measurement_id']);core.mkdir(destination)
        result = {'technical_status':'invalid_answer_schema'if self.failure else'complete'}
        core.save(destination/'request.json', {'measurement_id':job['measurement_id'], 'seed':job['seed']})
        core.save(destination/'attempt.json', {'retry':False})
        core.save(destination/'receipt.json', {'status':'complete'})
        (destination/'response.raw').write_bytes(b'synthetic offline fixture')
        core.save(destination/'result.json', result)
        return result

    def sources(self): pass
    def request(self,job): return job

    def observe(self):
        value=copy.deepcopy(self.guard.observation);at=core.now()
        value['observed_from']=value['observed_until']=at
        value['co_residency']['observed_from']=value['co_residency']['observed_until']=at
        value['co_residency']['actors'][0]['guard']['at']=at
        if self.wait_after_first and self.calls:value['co_residency']['provider']['waiting_requests']=1
        if self.mutate_source:Path(self.partition['source_jobs']['path']).write_text('[]')
        return value

    def run_role(self):
        callbacks=dict(worker=self.worker,observation=self.observe,source_check=self.sources,
                       request_check=self.request,replay_history=replay_history)
        refs={k:core.pin(Path(v.__code__.co_filename).resolve())for k,v in callbacks.items()}
        return finite.run_finite_queue(self.partition_ref,'a',self.descriptors,self.output,
            self.policy,self.contract,**callbacks,callback_sources=refs,required_pins=[],
            pause_paths=self.policy['local_pause_paths'])

    def test_exact_partition_and_pilot_closure(self):
        before=copy.deepcopy(self.partition)
        p,jobs,pilots=finite.finite_partition(self.partition_ref)
        self.assertEqual(p,before);self.assertEqual(jobs['a'],self.jobs[4:6])
        self.assertEqual(pilots['b'],self.jobs[2:4])
        self.assertEqual(finite.qualified_pilots(p,pilots,self.descriptors)['a']['terminal'],2)

    def test_reorder_duplicate_gap_boolean_refuse(self):
        for indices in ([5,4],[4,4],[4],[True,5]):
            value=copy.deepcopy(self.partition);value['roles']['a']['ordinals']=indices
            with self.subTest(indices=indices),self.assertRaises(core.IntegrityError):
                finite.finite_partition(self.changed(value))

    def test_native_numeric_seed_identity_preserved(self):
        value=copy.deepcopy(self.partition);jobs=copy.deepcopy(self.jobs[4:6]);jobs[0]['seed']=float(jobs[0]['seed'])
        value['roles']['a']['jobs']=self.save('float-seed.json',jobs)
        with self.assertRaisesRegex(core.IntegrityError,'projection'):finite.finite_partition(self.changed(value))

    def test_no_context_split(self):
        value=copy.deepcopy(self.partition)
        for role,indices in [('a',[4,6]),('b',[5,7])]:
            value['roles'][role]['ordinals']=indices
            value['roles'][role]['jobs']=self.save('split-'+role+'.json',[self.jobs[i]for i in indices])
        with self.assertRaisesRegex(core.IntegrityError,'split_context'):finite.finite_partition(self.changed(value))

    def test_no_two_client_or_boolean_promotion(self):
        for clients in (2,True):
            value=copy.deepcopy(self.partition);value['roles']['a']['max_clients']=clients
            with self.assertRaisesRegex(core.IntegrityError,'one_client'):finite.finite_partition(self.changed(value))

    def test_unaccepted_pilot_prevents_queue(self):
        self.descriptors['b']['review']=self.save('not-accepted.json',{'findings':[]})
        with self.assertRaisesRegex(core.IntegrityError,'acceptance'):self.run_role()
        self.assertFalse(self.calls)

    def test_native_queue_exact_remaining_and_no_replay(self):
        self.assertEqual(self.run_role()['new_final'],2)
        self.assertEqual(self.calls,[j['measurement_id']for j in self.jobs[4:6]])
        second=self.run_role();self.assertEqual((second['existing_final'],second['new_final']),(2,0))
        self.assertEqual(len(self.calls),2)

    def test_real_actor_priority_guard_holds_second_claim(self):
        self.wait_after_first=True;result=self.run_role()
        self.assertEqual((result['new_final'],result['remaining_unclaimed']),(1,1))
        self.assertIn('actor_priority_metrics',result['stop_reason']);self.assertEqual(len(self.calls),1)

    def test_source_drift_between_observation_and_claim(self):
        self.mutate_source=True;result=self.run_role()
        self.assertEqual(result['new_final'],0);self.assertIn('file_pin_mismatch',result['stop_reason'])
        self.assertFalse(self.calls)

    def test_invalid_attempts_occupied_not_replaced(self):
        self.failure=True;result=self.run_role()
        self.assertEqual((result['new_final'],result['consecutive_failures']),(2,2))
        self.assertEqual(self.run_role()['existing_final'],2);self.assertEqual(len(self.calls),2)

    def test_partial_attempt_not_replayed(self):
        job=self.jobs[4];dest=core.measurement_path(self.output,job,family='fixed',model='model')
        core.mkdir(dest);core.save(dest/'INPUT.json',job)
        with self.assertRaisesRegex(core.AdmissionHold,'partial_attempt'):self.run_role()
        self.assertFalse(self.calls)

    def test_changed_pilot_final_refuses_before_new_claim(self):
        closure=finite.checked(self.descriptors['a']['actual'])
        final=finite.checked(closure['verified_finals'])[0]['final']
        Path(final['path']).write_text('{}')
        with self.assertRaisesRegex(core.IntegrityError,'file_pin_mismatch'):self.run_role()
        self.assertFalse(self.calls)

    def test_executor_lock_required(self):
        fcntl.flock(self.lock,fcntl.LOCK_UN)
        with self.assertRaisesRegex(core.IntegrityError,'executor_lock'):self.run_role()
        self.assertFalse(self.calls)

    def test_registered_unknown_pid_argv_and_plan_refuse(self):
        plans,owners,observed={ },[],[];source=core.pin(Path(__file__).resolve())
        for n,role in enumerate(('a','b')):
            p=dict(role=role,output_root=str(self.root/role),execution_host='client-host',provider_host='gpu-'+role,max_clients=1,source=source)
            ref=self.save(role+'.plan.json',p);plans[role]=ref
            argv=['/python',source['path'],'--plan',json.dumps(ref)]
            process=dict(pid=10+n,process_starttime_ticks=str(100+n),argv=argv)
            owners.append(dict(role=role,plan=ref,source=source,output=p['output_root'],host='client-host',process=process,approved_argv=argv))
            observed.append(dict(host='client-host',process=process))
        ref=self.save('owners.json',dict(schema=finite.OWNERS_SCHEMA,root_authorized=True,owners=owners))
        self.assertEqual(finite.registered_owners(ref,plans,observed),observed)
        for mutated in [observed[:1],observed+[dict(host='client-host',process=dict(pid=99))],
                        [dict(observed[0],process=dict(observed[0]['process'],pid=99)),observed[1]],
                        [dict(observed[0],process=dict(observed[0]['process'],argv=['/other'])),observed[1]]]:
            with self.subTest(observed=mutated),self.assertRaises(core.IntegrityError):
                finite.registered_owners(ref,plans,mutated)


if __name__=='__main__':unittest.main()
