"""Small saved-custody fixtures; no collector, parser, provider or projection call."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

import test_campaign_supplement_mixed_owner as fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
spec = importlib.util.spec_from_file_location('mixed_union_under_test',
    ROOT/'src/causes/campaign_supplement_mixed_closed_union.py')
union = importlib.util.module_from_spec(spec); spec.loader.exec_module(union)


class Fixture:
    def __init__(self, *, derived_failure=False):
        self.m, self.partition, self.head, self.helper = fixtures.fixture()
        self.docs = {path:json.loads(raw) for path,raw in self.m.files.items()}
        proof = self.docs[self.partition['actual']['path']]
        self.base = proof['original_base']
        base = self.docs[self.base['actual']['path']]
        source = base['source']; self.original_runner = source; self.original_adapter = source
        self.m.files[self.helper['path']] = (ROOT/'src/causes/campaign_supplement_mixed_owner.py').read_bytes()
        del self.docs[self.helper['path']]

        def put(value):
            ref=self.m.put(value); self.docs[ref['path']]=copy.deepcopy(value);return ref
        self.put = put
        legacy_plan = put(dict(jobs=base['full_jobs'],endpoint='synthetic-endpoint',output_root='/synthetic/legacy'))
        legacy = dict(schema='causes.finite_supplement_tranche_closed.v1',role='mi',active=0,ambiguous=[],
            plan=legacy_plan,output='/synthetic/legacy',full_jobs=base['full_jobs'],complete_ids=[],
            unstarted_ids=base['complete_ids'],native_collector=source,native_runner=source,adapter=source,
            identity={'synthetic':True},consecutive_failures=0,qualification_findings=[],cumulative_invalid_ids=[],
            native_invocations=[],terminal=0,occupied=0)
        legacy_ref=put(legacy); legacy_peer=put(dict(accepted_bounded_finite_tranche_closed=True,
                                                    actual=legacy_ref,findings=[]))
        previous_ref=dict(actual=legacy_ref,review=legacy_peer); previous=legacy
        self.rows_by_id={};self.native_rows=[]
        for index,descriptor in enumerate([self.base]+proof['links']):
            saved=self.docs[descriptor['actual']['path']]
            plan=self.docs[saved['plan']['path']]
            jobs=self.docs[saved['full_jobs']['path']]
            is_derived=index==1; output='/synthetic/invocation'+str(index)
            selected=jobs[len(previous['complete_ids']):len(saved['complete_ids'])]
            for job in jobs:
                job.setdefault('context',None);job.setdefault('frozen_config',None);job.setdefault('original_refs',[])
            # Adding schema fields to this fixture is done consistently in all
            # source banks/accepted rows before pinning; it is not a runtime edit.
            plan.update(schema='causes.completed_actor_driver_plan.v1',mode='completed_actor_direct_probe',
                predecessor_custody=previous_ref,occupied_ids=previous['complete_ids'],
                initial_failures=previous['consecutive_failures'],maximum_new_requests=len(selected),
                jobs_sha256=union.digest(selected),family=selected[0]['family'],model='mi27',role='mi',
                sources={'driver':source,'native_queue':source,'collector':source},output_root=output,
                endpoint='synthetic-endpoint')
            constructor=(dict(schema='causes.derived_probe_execution_constructor.v1',
                              scientific_modules={'campaign_runner':source,'derived_probe_inputs':source})
                         if is_derived else dict(bindings={'runner':source,'adapter':source}))
            plan['constructor']=put(constructor)
            rows=[]; events=[];counter=plan['initial_failures']
            for ordinal,job in enumerate(selected):
                final=self.raw(output+'/job'+str(ordinal)+'/FINAL.json',b'{}')
                self.raw(str(Path(final['path']).with_name('INPUT.json')),union.encoded(job))
                failed=is_derived and derived_failure and ordinal==len(selected)-1
                status='failed' if failed else 'ok'
                row=dict(measurement_id=job['measurement_id'],final=final,capture_files=[],
                         technical_status=status,raw_parsed_label=None if failed else 'yes',
                         request_body_sha256=job['request_body_sha256'])
                rows.append(row); self.rows_by_id[job['measurement_id']]=row
                events.append(dict(event='claimed',measurement_id=job['measurement_id']))
                counter=counter+1 if failed else 0
                events.append(dict(event='route_failure' if failed else 'route_success',
                    measurement_id=job['measurement_id'],final=final,technical_status=status,
                    consecutive_failures=counter))
            self.native_rows.extend(rows)
            journal=self.raw(output+'/journal',b''.join(union.encoded(e)+b'\n' for e in events))
            result=put(dict(result=dict(phase='completed_actor_direct_probe',active=0,existing_final=0,
                new_final=len(rows),remaining_unclaimed=0,consecutive_failures=counter,
                stop_reason=None,completed_ids=[row['measurement_id'] for row in rows])))
            recipe=put(dict(source=source,plan=saved['plan'],driver_result=result,
                            native_collector=source,derived_collector=source))
            invocation=dict(plan=saved['plan'],source=source,result=result,pid=100+index,
                process_absent=True,family=plan['family'],model='mi27',jobs_sha256=plan['jobs_sha256'],
                original_reservation_transfer=plan.get('original_reservation_transfer'))
            if is_derived:invocation['derived_bank']=plan['derived_bank']
            saved.update(output=output,prior_closed=previous_ref['actual'],prior_closed_review=previous_ref['review'],
                source=source,recipe=recipe,native_result=result,journal=journal,
                verified_finals=put(rows),source_pins=put([source]),
                admission_observations=put([dict(measurement_id=r['measurement_id'],qualified=True) for r in rows]),
                original_denominator_preserved=True,all_original_attempts_retained=True,
                inherited_qualification_findings=[],current_qualification_findings=[],qualification_findings=[],
                new_scientifically_qualified_ids=[row['measurement_id'] for row in rows],new_invalid_ids=[],
                prior_invalid_ids=[],cumulative_invalid_ids=[],scientifically_qualified_total=len(saved['complete_ids']),
                consecutive_failures=counter,stop_reason=None,
                native_invocations=copy.deepcopy(previous['native_invocations'])+[invocation])
            peer=self.docs[descriptor['review']['path']]
            peer.update(qualified_admission_ids=[row['measurement_id'] for row in rows],
                        invalid_admission_ids=[],invalid_admission_custody=[])
            if index:
                extension=saved['derived_bank_extension' if is_derived else 'original_bank_extension']
                extension['cumulative_jobs_sha256']=union.digest(jobs)
                extension['appended_jobs_sha256']=union.digest(selected)
                if not is_derived:
                    transfer=self.docs[plan['original_reservation_transfer']['actual']['path']]
                    transfer.update(target_output=output,jobs_sha256=union.digest(selected))
            previous_ref,previous=descriptor,saved
        # Keep the accepted prepared derived rows byte-identical to their jobs.
        derived_plan=self.docs[self.docs[proof['links'][0]['actual']['path']]['plan']['path']]
        bank=self.docs[derived_plan['derived_bank']['actual']['path']]
        prepared=self.docs[bank['inputs'][0]['actual']['path']]
        rows=self.docs[prepared['rows']['path']]
        for row in rows:
            for job in row['jobs']:
                job.update(context=None,frozen_config=None,original_refs=[])
        prepared['rows_canonical_sha256']=union.digest(rows)
        bank['jobs_sha256']=union.digest([j for row in rows for j in row['jobs']])
        self.source_peer=put(dict(accepted_original_after_derived_source=True,findings=[],sources={'mixed_owner':self.helper}))
        self.descriptor=dict(schema='causes.supplement_mixed_closed_union.v1',
            roots=[dict(role='mi',**self.head)],maximum_payload_bytes=1024**2,
            mixed_owner_source=self.helper,mixed_owner_source_review=self.source_peer,
            owner_partitions=[dict(role='mi',partition=self.partition)])
        self.repin()

    def raw(self,path,data):
        self.m.files[path]=data
        return dict(path=path,bytes=len(data),sha256=hashlib.sha256(data).hexdigest())

    def repin(self):
        cache={};active=set()
        def reference(ref):
            path=ref['path']
            if path not in cache:
                assert path not in active;active.add(path)
                raw=union.encoded(rewrite(self.docs[path])) if path in self.docs else self.m.files[path]
                self.m.files[path]=raw
                cache[path]=dict(path=path,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
                active.remove(path)
            return cache[path]
        def rewrite(value):
            if type(value) is dict:
                if set(value)=={'path','bytes','sha256'}:return reference(value)
                return {k:rewrite(v) for k,v in value.items()}
            if type(value) is list:return [rewrite(v) for v in value]
            return value
        self.descriptor=rewrite(self.descriptor)
        self.head={k:reference(v) for k,v in self.head.items()}
        self.partition={k:reference(v) for k,v in self.partition.items()}
        self.base={k:reference(v) for k,v in self.base.items()}
        self.get=lambda ref:json.loads(self.m.read(ref))

    def normalize(self, descriptor=None):
        return union.normalize_closed_union(descriptor or self.descriptor,self.get,self.m.read,
                                            lambda *a,**k:([],[]))

    def qualification_failure(self, *, final=True, admission=True, original=False):
        proof=self.docs[self.partition['actual']['path']]
        chain=[self.base]+proof['links']; target=0 if original else 1
        saved=self.docs[chain[target]['actual']['path']]
        rows=self.docs[saved['verified_finals']['path']]; mid=rows[0]['measurement_id']
        observations=self.docs[saved['admission_observations']['path']]
        findings=[]
        if final:
            rows[0]=dict(measurement_id=mid,qualification_error='synthetic native final failure')
            findings.append(dict(measurement_id=mid,stage='native_final',error=rows[0]['qualification_error']))
        if admission:
            observations[0]=dict(measurement_id=mid,qualified=False,error='synthetic native admission failure')
            findings.append(dict(measurement_id=mid,stage='native_admission',error=observations[0]['error']))
        previous_findings=[]; previous_invalid=[]
        for index, descriptor in enumerate(chain):
            item=self.docs[descriptor['actual']['path']]; peer=self.docs[descriptor['review']['path']]
            current=findings if index==target else []
            invalid=[mid] if index==target else []
            item.update(current_qualification_findings=current,inherited_qualification_findings=previous_findings,
                qualification_findings=previous_findings+current,prior_invalid_ids=previous_invalid,
                new_invalid_ids=invalid,cumulative_invalid_ids=previous_invalid+invalid)
            if index==target:
                item['new_scientifically_qualified_ids'].remove(mid)
                peer['qualified_admission_ids'].remove(mid);peer['invalid_admission_ids']=[mid]
            item['scientifically_qualified_total']=len(item['complete_ids'])-len(item['cumulative_invalid_ids'])
            peer['qualification_findings']=item['qualification_findings']
            previous_findings=item['qualification_findings']; previous_invalid=item['cumulative_invalid_ids']
        self.repin()

    def membership(self, normalized):
        actual=self.get(self.head['actual']);plan=self.get(actual['plan']);bank=self.get(actual['full_jobs'])
        wanted={row['measurement_id'] for row in normalized['rows']}
        jobs=[j for j in bank if j['measurement_id'] in wanted]
        global_jobs=self.put(jobs)
        assignments=self.put(dict(epochs={j['run_id']:'early-fixed' for j in jobs}))
        assignment_peer=self.put(dict(accepted_test_assignments=True,assignments=assignments,findings=[]))
        rows=[]
        for ordinal,(job,row,origin) in enumerate(zip(jobs,normalized['rows'],normalized['origins'])):
            native=self.get(origin['actual']);full=self.get(native['full_jobs'])
            input_ref=dict(path=str(Path(row['final']['path']).with_name('INPUT.json')),
                           bytes=len(union.encoded(job)),sha256=hashlib.sha256(union.encoded(job)).hexdigest())
            rows.append(dict(measurement_id=job['measurement_id'],jobs=native['full_jobs'],
                job_index=[j['measurement_id'] for j in full].index(job['measurement_id']),
                global_job_ordinal=ordinal,global_job_sha256=union.digest(job),input=input_ref,
                role='mi',endpoint=plan['endpoint'],terminal=origin['terminal'],final=row['final'],
                capture_files=row['capture_files'],epoch='early-fixed',readout='self_'+job['question_id'],
                **{k:job[k] for k in ('run_id','stage','question_id','context','frozen_config','original_refs')}))
        membership=self.put(dict(global_jobs=global_jobs,assignments=assignments,active_rows=[],
            measurement_ids=[j['measurement_id'] for j in jobs],rows=rows))
        extension=self.put(dict(schema='causes.supplement_global_jobs_extension.v1',
            previous_global_jobs=self.get(self.base['actual'])['full_jobs'],
            previous_assignments=assignments,global_jobs=global_jobs,assignments=assignments,
            closed_bank_extensions=[{k:e[k] for k in ('actual','review')}
                                    for e in normalized['original_bank_extensions']]))
        return dict(global_jobs=global_jobs,assignments=assignments,assignment_review=assignment_peer,
            assignment_acceptance_flag='accepted_test_assignments',membership=membership,
            global_jobs_extension=extension,parts=[dict(role='mi',**self.head,plan=actual['plan'],
                jobs=actual['full_jobs'],output_root=actual['output'],endpoint=plan['endpoint'],
                terminal_count=actual['terminal'])])


class MixedUnionTests(unittest.TestCase):
    def test_whole_owner_checked_then_original_rows_only(self):
        f=Fixture();result=f.normalize()
        self.assertEqual(len(result['rows']),3)
        self.assertEqual(result['whole_owner_rows_validated'],12)
        self.assertEqual(len(result['excluded_derived_evidence']),9)
        self.assertEqual([o['verified_index'] for o in result['origins']],[0,0,1])
        self.assertEqual([row['measurement_id'] for row in result['rows']],
                         [f.native_rows[i]['measurement_id'] for i in (0,10,11)])
        self.assertTrue(all(row==f.rows_by_id[row['measurement_id']] for row in result['rows']))

    def test_derived_failure_remains_history_not_original_missing(self):
        f=Fixture(derived_failure=True);result=f.normalize()
        self.assertEqual([r['technical_status'] for r in result['rows']],['ok']*3)
        derived=f.get(f.get(f.partition['actual'])['links'][0]['actual'])
        self.assertEqual(derived['consecutive_failures'],1)
        head=f.get(f.head['actual']);self.assertEqual(f.get(head['plan'])['initial_failures'],1)
        self.assertEqual(len(result['excluded_derived_evidence']),9)

    def test_derived_final_and_admission_findings_share_one_retained_id(self):
        f=Fixture();f.qualification_failure();result=f.normalize()
        self.assertEqual(len(result['rows']),3)
        failure=result['excluded_derived_evidence'][0]
        self.assertEqual([r['stage'] for r in failure['qualification']['findings']],
                         ['native_final','native_admission'])
        self.assertEqual(f.get(f.head['actual'])['cumulative_invalid_ids'],[failure['measurement_id']])

    def test_derived_final_only_failure_and_exact_error_join(self):
        f=Fixture();f.qualification_failure(admission=False)
        self.assertEqual(len(f.normalize()['excluded_derived_evidence'][0]['qualification']['findings']),1)
        proof=f.docs[f.partition['actual']['path']];saved=f.docs[proof['links'][0]['actual']['path']]
        f.docs[saved['verified_finals']['path']][0]['qualification_error']='different failure'
        f.repin()
        with self.assertRaisesRegex(ValueError,'qualification_partition'):f.normalize()

    def test_original_final_qualification_failure_remains_refused(self):
        f=Fixture();f.qualification_failure(admission=False,original=True)
        with self.assertRaises(ValueError):f.normalize()

    def test_default_refuses_mixed_history(self):
        f=Fixture();descriptor={k:f.descriptor[k] for k in ('roots','maximum_payload_bytes')}
        descriptor['schema']='causes.supplement_closed_union.v1'
        with self.assertRaises(ValueError):f.normalize(descriptor)

    def test_original_only_default_has_no_mixed_output(self):
        f=Fixture();descriptor=dict(schema='causes.supplement_closed_union.v1',
            roots=[dict(role='mi',**f.base)],maximum_payload_bytes=1024**2)
        result=f.normalize(descriptor)
        self.assertEqual(len(result['rows']),1)
        self.assertNotIn('mixed_owner_partitions',result)
        self.assertNotIn('excluded_derived_evidence',result)

    def test_missing_partition_and_wrong_source_peer_refuse(self):
        f=Fixture();descriptor=copy.deepcopy(f.descriptor);descriptor['owner_partitions']=[]
        with self.assertRaisesRegex(ValueError,'partitions'):f.normalize(descriptor)
        f=Fixture();self.assertIn('mixed_owner_source',f.descriptor)
        f.docs[f.source_peer['path']]['sources']['mixed_owner']=f.put({'wrong':True});f.repin()
        with self.assertRaisesRegex(ValueError,'source_review'):f.normalize()

    def test_partition_membership_rejoin(self):
        f=Fixture();result=f.normalize()
        ids=union._original_ids(result,f.head['actual'],f.get)
        self.assertEqual(ids,{row['measurement_id'] for row in result['rows']})
        result['mixed_owner_partitions'][0]['original_ids'].append('forged')
        with self.assertRaisesRegex(ValueError,'partition_rejoin'):
            union._original_ids(result,f.head['actual'],f.get)

    def test_global_original_bank_is_distinct_from_whole_owner_bank(self):
        f=Fixture();normalized=f.normalize();data=f.membership(normalized)
        jobs,membership,assignments,rows,origins=union.validate_membership(data,normalized,f.get)
        self.assertEqual(len(jobs),3);self.assertEqual(len(f.get(data['parts'][0]['jobs'])),12)
        self.assertEqual([m['job_index'] for m in membership['rows']],[0,10,11])
        self.assertEqual(rows,normalized['rows']);self.assertEqual(origins,normalized['origins'])
        evidence=union.projected_evidence(data['membership'],membership,f.put(rows),rows,origins)
        self.assertEqual(set(evidence),{row['measurement_id'] for row in rows})

    def test_derived_jobs_cannot_enter_global_original_bank(self):
        f=Fixture();normalized=f.normalize();data=f.membership(normalized)
        all_jobs=f.get(data['parts'][0]['jobs'])
        data['global_jobs']=f.put(all_jobs)
        with self.assertRaisesRegex(ValueError,'global_jobs_extension_binding'):
            union.validate_membership(data,normalized,f.get)


if __name__=='__main__':unittest.main()
