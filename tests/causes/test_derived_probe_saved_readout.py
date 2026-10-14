"""Synthetic future closures only; these fixtures are not runtime evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import derived_probe_saved_readout as readout
from shared import evidence_bytes


class Archive:
    def __init__(self, root, *, unavailable=False):
        self.root, self.serial = Path(root), 0
        self.source = self.save('source', {'synthetic_source': True})
        self.questions=[dict(id=q,awareness_type='evaluation',threshold=q.removeprefix('evaluation_'),text=q+'?')
                        for q in readout.QUESTIONS]
        self.config = self.save('config', {'endpoint':'unused', 'model':'synthetic', 'questions':self.questions})
        self.protocol = dict(schema='causes.derived_probe_protocol.v1', family='derived_probe_test', model='test',
                             config=self.config, sources={'campaign_runner':self.source})
        protocol_ref = self.save('protocol', self.protocol)
        self.rows = []
        for run in ('B', 'A'):
            key = 'derived_probe_' + run
            row = dict(schema='causes.derived_probe_input.v1', run_id=run, view_kind='neutral_initial', stage='early',
                measurement_key=key, disposition='prepared', runtime_authorized=False,
                protocol_sha256='a'*64, context_canonical_sha256='b'*64, view_canonical_sha256='c'*64,
                view={'file':self.save('view', {'phase':'prefix','messages':[]})},
                original={'seed':17}, origin={'allocation':self.source,'config':self.config},
                provenance=[self.source], jobs=[])
            for question in readout.QUESTIONS:
                request = self.save('request', {'question':question})
                job = dict(family='derived_probe_test', model='test', run_id=key, original_run_id=run,
                    original_seed=17, seed=19, stage='early', question_id=question, protocol_sha256='a'*64,
                    context_canonical_sha256='b'*64, derived_view_canonical_sha256='c'*64,
                    question_canonical_sha256=readout.digest(next(q for q in self.questions if q['id']==question)), request_body_sha256='e'*64,
                    request_file_sha256=request['sha256'], request_body_bytes=request['bytes']-1)
                job['measurement_id'] = '/'.join(job[k] for k in ('family','model','run_id','stage','question_id'))
                row['jobs'].append(job)
            if unavailable and run == 'A':
                row.update(disposition='unavailable', view=None, jobs=[], reason='saved_input_unavailable')
            self.rows.append(row)
        self.pairs = [dict(task_id='task', draw_key=0, variant='variant', arms={arm:
            {k:row[k] for k in ('run_id','measurement_key','disposition')}
            for arm,row in zip(('baseline','treatment'),self.rows)})]
        self.jobs = [j for r in self.rows for j in r['jobs']]
        self.job_rows = {j['measurement_id']:r for r in self.rows for j in r['jobs']}
        self.counts = dict(assigned=2, ready=2-int(unavailable), unavailable=int(unavailable),
                           requests=len(self.jobs), missing_slots=3*int(unavailable))
        self.prep = dict(schema='causes.frozen_derived_probe_preparation.v1', runtime_authorized=False,
            assignments=self.save('assignments',[{k:r[k] for k in ('run_id','view_kind','stage')} for r in self.rows]),
            rows=self.save('rows',self.rows), rows_canonical_sha256=readout.digest(self.rows),
            pairs=self.save('pairs',self.pairs), protocol=protocol_ref, counts=self.counts, actual_jobs=len(self.jobs))
        self.input = self.accept(self.prep, 'accepted_frozen_derived_probe_inputs', field='preparation',
                                **{k:self.prep[k] for k in ('rows','assignments','pairs','protocol','counts')})
        self.previous = dict(schema='causes.finite_supplement_tranche_closed.v1', complete_ids=['old/job'],
            unstarted_ids=[], qualification_findings=[{'stage':'native_admission','measurement_id':'old/job','error':'retained'}],
            consecutive_failures=0)
        self.previous_ref = self.accept(self.previous,'accepted_finite_supplement_closed_custody',
            original_denominator_preserved=True,all_original_attempts_retained=True,
            qualification_findings=self.previous['qualification_findings'])
        self.bank_value = dict(schema='causes.derived_supplement_bank_adoption.v1', root_authorized=True,
            family='derived_probe_test', model='test', inputs=[self.input], execution_predecessor=self.previous_ref,
            job_count=len(self.jobs), jobs_sha256=readout.digest(self.jobs),
            ids_sha256=readout.digest([j['measurement_id'] for j in self.jobs]), unique_assigned_slots=6,
            unique_missing_slots=3*int(unavailable), source_counts=[dict(preparation=self.input['actual'],
                new_jobs=len(self.jobs),shared_jobs=0,counts=self.counts)])
        self.bank = self.accept(self.bank_value,'accepted_derived_supplement_bank')

    def save(self, name, value, *, raw=False):
        self.serial += 1
        path = self.root / (str(self.serial)+'_'+name+'.json')
        path.write_bytes(value if raw else readout.encoded(value)+b'\n')
        return evidence_bytes.pin(path)

    def accept(self, actual, flag, field='actual', **extra):
        ref = self.save('actual',actual)
        return dict(actual=ref,review=self.save('review',{flag:True,field:ref,'findings':[],**extra}))

    def closure(self, count=None, *, previous=None, gzip=False, mutation=None, invalid=None, technical=None):
        prior, prior_ref, offset = previous or (self.previous,self.previous_ref,0)
        selected = self.jobs[offset:]
        count = len(selected) if count is None else count
        current = selected[:count]; ids = [j['measurement_id'] for j in current]
        plan = dict(schema='causes.completed_actor_driver_plan.v1', derived_bank=self.bank,
            predecessor_custody=prior_ref,occupied_ids=prior['complete_ids'],initial_failures=prior['consecutive_failures'],
            family=self.bank_value['family'],model='test',output_root=str(self.root/'runtime'),
            maximum_new_requests=len(selected),jobs_sha256=readout.digest(selected),sources={'driver':self.source})
        plan_ref=self.save('plan',plan); result_ref=self.save('native_result', {'synthetic':True})
        recipe_ref=self.save('recipe',dict(plan=plan_ref,source=self.source,driver_result=result_ref))
        values=[];events=[];findings=[]
        for index,job in enumerate(current):
            mid=job['measurement_id'];row=self.job_rows[mid]
            final=self.save('final',{'synthetic':mid})
            status='invalid_schema' if index == technical else 'complete'
            parsed=dict(technical_status=status,answer='no' if status == 'complete' else None,reason='native reason')
            captures=[]
            capture_root=self.root/('capture_'+str(self.serial));capture_root.mkdir()
            for name in ('request.json','attempt.json','receipt.json','response.raw','result.json'):
                path=capture_root/name
                path.write_bytes(readout.encoded({'question':job['question_id']})+b'\n' if name=='request.json' else b'{}\n')
                captures.append(evidence_bytes.pin(path))
            value=dict(measurement_id=mid,disposition='final_verified',technical_status=status,
                admissible_label=parsed['answer'],label=parsed['answer'],raw_parsed_label=parsed['answer'],
                reason=parsed['reason'],raw_result=parsed,failure=None,integrity_failure=False,final=final,
                input_canonical_sha256=readout.digest(job), context=row['view']['file'],derived_view=row['view'],
                context_canonical_sha256=job['context_canonical_sha256'],question_canonical_sha256=job['question_canonical_sha256'],
                seed=job['seed'],request_body_sha256=job['request_body_sha256'],capture_files=captures,
                offline_verification='exact_frozen_request_render_and_parser_reconstructed',
                derived_input_binding=dict(schema='causes.derived_probe_final_binding.v1',config=self.config,
                    context=row['view']['file'],provenance=[*row['origin'].values(),*row['provenance'],*self.protocol['sources'].values()]))
            values.append(value)
            events.extend([dict(event='claimed',measurement_id=mid,input_sha256=readout.digest(job)),
                dict(event='route_success' if status=='complete' else 'route_failure',measurement_id=mid,technical_status=status,final=final)])
            if index == invalid: findings.append(dict(stage='native_admission',measurement_id=mid,error='synthetic refusal'))
        if mutation:mutation(values,events,plan)
        # Rebind the deliberately changed synthetic plan for adversarial tests.
        plan_ref=self.save('plan_final',plan)
        recipe_ref=self.save('recipe_final',dict(plan=plan_ref,source=self.source,driver_result=result_ref))
        if gzip:
            record=evidence_bytes.write_gzip(self.root/('payload'+str(self.serial)+'.gz'),readout.encoded(values))
            payload=self.save('wrapper',record)
        else:payload=self.save('verified',values)
        allids=[j['measurement_id'] for j in self.jobs]
        actual=dict(schema='causes.finite_supplement_tranche_closed.v1',source=self.source,plan=plan_ref,recipe=recipe_ref,
            native_result=result_ref,derived_execution_protocol=self.prep['protocol'],derived_scientific_protocol=self.protocol,
            clean_native_ending=True,active=0,unresolved_claimed_ids=[],ambiguous=[],original_denominator_preserved=True,
            all_original_attempts_retained=True,prior_closed=prior_ref['actual'],prior_closed_review=prior_ref['review'],
            output=str(self.root/'runtime'),verified_finals=payload,
            journal=self.save('journal',b''.join(readout.encoded(e)+b'\n' for e in events),raw=True),
            new_scientifically_qualified_ids=[mid for i,mid in enumerate(ids) if i!=invalid],
            new_invalid_ids=[] if invalid is None else [ids[invalid]],inherited_qualification_findings=prior['qualification_findings'],
            current_qualification_findings=findings,qualification_findings=prior['qualification_findings']+findings,
            consecutive_failures=0,complete_ids=['old/job']+allids[:offset+count],unstarted_ids=allids[offset+count:],
            terminal=1+offset+count,occupied=1+offset+count,
            derived_bank_extension=dict(schema='causes.derived_supplement_owner_bank_extension.v1',adoption=self.bank,
                execution_predecessor=self.previous_ref,appended_count=len(self.jobs),prior_count=1,cumulative_count=1+len(self.jobs),
                prior_prefix_unchanged=True,native_families_preserved=True,
                appended_jobs_sha256=readout.digest(self.jobs),appended_ids_sha256=readout.digest(allids)),
            native_invocations=[dict(plan=plan_ref,source=self.source,result=result_ref,derived_bank=self.bank,
                                     family=plan['family'],model=plan['model'],jobs_sha256=plan['jobs_sha256'])])
        ref=self.accept(actual,'accepted_finite_supplement_closed_custody',accepted_completed_actor_saved_closure=True,
            source=self.source,original_denominator_preserved=True,all_original_attempts_retained=True,
            qualification_findings=actual['qualification_findings'])
        return actual,ref,offset+count

    def spec(self):
        spec=dict(schema='causes.frozen_derived_probe_readout_specification.v1',input_preparation=self.input['actual'],
            analysis_source=evidence_bytes.pin(readout.analysis.__file__),
            pairs=self.prep['pairs'],rows=self.prep['rows'],protocol=self.prep['protocol'],function='contrast',
            function_parameters=dict(release=False,family_size=10),measurement_type='appended_direct_self_report',
            label_function='self_label',awareness_type='evaluation',thresholds=list(readout.THRESHOLDS),
            question_ids=list(readout.QUESTIONS),inference_released=False,planned_family_size=10,
            separate_verbalized_awareness_required=True,assignment=dict(factor='test',members={'task':dict(
                control=['derived_probe_B'],treatment=['derived_probe_A'])}),
            counts=dict(assigned_arms=2,pairs=1,ready_arms=self.counts['ready'],tasks=1,unavailable_arms=self.counts['unavailable']))
        ref=self.save('spec',spec)
        return ref,self.save('spec_peer',dict(accepted_frozen_derived_probe_readout_specification=True,
                                           specification=ref,preparation=self.input['actual'],findings=[]))


class SavedDirectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.archive=Archive(self.temp.name)

    def test_plain_complete_and_descriptive_contract(self):
        a=self.archive;actual,ref,_=a.closure()
        result=readout.resolve_saved_direct(a.bank,[ref])
        self.assertEqual((result['assigned'],result['claimed'],result['pending']),(6,6,0))
        self.assertTrue(all(r['status']=='observed' and r['label']=='no' for r in result['measurements']))
        self.assertEqual(len(result['origins']),1)
        spec,peer=a.spec();summary=readout.describe_direct(spec,peer,result)
        self.assertEqual(summary['contrasts']['presence']['equal_task_effect'],0)
        self.assertNotIn('se_upper_bound',summary['contrasts']['presence'])

    def test_gzip_matches_plain_labels(self):
        a=self.archive;_,ref,_=a.closure(gzip=True)
        result=readout.resolve_saved_direct(a.bank,[ref])
        self.assertEqual([r['label'] for r in result['measurements']],['no']*6)

    def test_ordered_partial_then_continuation(self):
        a=self.archive;first=a.closure(2)
        partial=readout.resolve_saved_direct(a.bank,[first[1]])
        self.assertEqual(partial['pending'],4)
        last=a.closure(previous=first,gzip=True)
        result=readout.resolve_saved_direct(a.bank,[first[1],last[1]])
        self.assertEqual(result['claimed'],6)
        self.assertEqual([r['origin']['source_index'] for r in result['measurements']],[0,0,1,1,1,1])
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[last[1],first[1]])

    def test_invalid_admission_preserves_native_label_but_is_missing(self):
        a=self.archive;_,ref,_=a.closure(invalid=0,technical=1)
        rows=readout.resolve_saved_direct(a.bank,[ref])['measurements']
        self.assertEqual((rows[0]['status'],rows[0]['label'],rows[0]['native_label']),('invalid_admission',None,'no'))
        self.assertEqual((rows[1]['status'],rows[1]['label']),('technical_missing',None))

    def test_unavailable_and_pending_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            a=Archive(directory,unavailable=True)
            result=readout.resolve_saved_direct(a.bank,[])
            self.assertEqual([r['status'] for r in result['measurements']],['pending']*3+['unavailable']*3)

    def test_shared_baseline_is_one_physical_measurement(self):
        a=self.archive;rows=copy.deepcopy(a.rows)
        rows[1]['run_id']='C';rows[1]['measurement_key']='derived_probe_C'
        for job in rows[1]['jobs']:
            job.update(run_id='derived_probe_C',original_run_id='C')
            job['measurement_id']='/'.join(job[k] for k in ('family','model','run_id','stage','question_id'))
        pairs=copy.deepcopy(a.pairs);pairs[0]['variant']='second'
        pairs[0]['arms']['treatment']={k:rows[1][k] for k in ('run_id','measurement_key','disposition')}
        prep=dict(a.prep,rows=a.save('second_rows',rows),rows_canonical_sha256=readout.digest(rows),
            assignments=a.save('second_assignments',[{k:r[k] for k in ('run_id','view_kind','stage')} for r in rows]),
            pairs=a.save('second_pairs',pairs))
        descriptor=a.accept(prep,'accepted_frozen_derived_probe_inputs',field='preparation',
                            **{k:prep[k] for k in ('rows','assignments','pairs','protocol','counts')})
        jobs=a.jobs+rows[1]['jobs'];bank=copy.deepcopy(a.bank_value)
        bank.update(inputs=bank['inputs']+[descriptor],job_count=9,jobs_sha256=readout.digest(jobs),
            ids_sha256=readout.digest([j['measurement_id'] for j in jobs]),unique_assigned_slots=9,
            source_counts=bank['source_counts']+[dict(preparation=descriptor['actual'],new_jobs=3,shared_jobs=3,counts=a.counts)])
        result=readout.resolve_saved_direct(a.accept(bank,'accepted_derived_supplement_bank'),[])
        self.assertEqual((result['assigned'],result['pending'],len(result['comparisons'])),(9,9,2))
        self.assertEqual(sum(r['original_run_id']=='B' for r in result['measurements']),3)

    def test_shared_treatment_cannot_be_relabelled_as_baseline_reuse(self):
        a=self.archive;second=a.accept(a.prep,'accepted_frozen_derived_probe_inputs',field='preparation',
                        **{k:a.prep[k] for k in ('rows','assignments','pairs','protocol','counts')})
        bank=copy.deepcopy(a.bank_value);bank['inputs'].append(second)
        with self.assertRaisesRegex(ValueError,'shared_baseline_changed'):
            readout.resolve_saved_direct(a.accept(bank,'accepted_derived_supplement_bank'),[])

    def test_forged_unavailable_input_cannot_hide_jobs(self):
        a=self.archive;rows=copy.deepcopy(a.rows);rows[1]['disposition']='unavailable'
        prep=dict(a.prep,rows=a.save('changed_rows',rows),rows_canonical_sha256=readout.digest(rows))
        descriptor=a.accept(prep,'accepted_frozen_derived_probe_inputs',field='preparation',
                            **{k:prep[k] for k in ('rows','assignments','pairs','protocol','counts')})
        bank=dict(a.bank_value,inputs=[descriptor])
        with self.assertRaisesRegex(ValueError,'unavailable_input'):
            readout.resolve_saved_direct(a.accept(bank,'accepted_derived_supplement_bank'),[])

    def test_wrong_verified_seed_request_context_and_label(self):
        for field,value in [('seed',99),('request_body_sha256','f'*64),('context_canonical_sha256','f'*64),
                            ('admissible_label','yes'),('measurement_id','foreign')]:
            with self.subTest(field=field):
                _,ref,_=self.archive.closure(mutation=lambda rows,events,plan:rows[0].__setitem__(field,value))
                with self.assertRaises((ValueError,KeyError)):readout.resolve_saved_direct(self.archive.bank,[ref])

    def test_claim_reordering_and_duplicate_terminal_refused(self):
        for mutate in [lambda r,e,p:r.reverse(),lambda r,e,p:e.append(copy.deepcopy(e[1]))]:
            _,ref,_=self.archive.closure(mutation=mutate)
            with self.assertRaises(ValueError):readout.resolve_saved_direct(self.archive.bank,[ref])

    def test_plan_bank_and_failure_carry_refused(self):
        for field,value in [('initial_failures',9),('derived_bank',self.archive.previous_ref)]:
            _,ref,_=self.archive.closure(mutation=lambda r,e,p:p.__setitem__(field,value))
            with self.assertRaises(ValueError):readout.resolve_saved_direct(self.archive.bank,[ref])

    def test_false_acceptance_and_replaced_saved_file(self):
        a=self.archive;_,ref,_=a.closure()
        peer=json.loads(Path(ref['review']['path']).read_bytes());peer['accepted_completed_actor_saved_closure']=False
        false=dict(ref,review=a.save('false_peer',peer))
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[false])
        Path(ref['actual']['path']).write_bytes(b'{}')
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[ref])

    def test_gzip_trailing_data_and_limit_refused(self):
        a=self.archive;actual,ref,_=a.closure(gzip=True)
        wrapper=json.loads(Path(actual['verified_finals']['path']).read_bytes())
        path=Path(wrapper['stored']['path']);path.write_bytes(path.read_bytes()+b'trailing')
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[ref])
        _,fresh,_=a.closure(gzip=True)
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[fresh],maximum_payload_bytes=10)

    def test_native_final_qualification_failure_is_not_a_negative_label(self):
        a=self.archive
        def mutate(rows,events,plan):
            rows[0]={'measurement_id':rows[0]['measurement_id'],'qualification_error':'bad saved final'}
        actual,ref,_=a.closure(invalid=0,mutation=mutate)
        # The synthetic acceptance names the same actual native-final finding.
        actual['current_qualification_findings'][0]['stage']='native_final'
        actual['qualification_findings'][-1]['stage']='native_final'
        ref=a.accept(actual,'accepted_finite_supplement_closed_custody',accepted_completed_actor_saved_closure=True,
            source=a.source,original_denominator_preserved=True,all_original_attempts_retained=True,
            qualification_findings=actual['qualification_findings'])
        row=readout.resolve_saved_direct(a.bank,[ref])['measurements'][0]
        self.assertEqual((row['status'],row['label'],row['native_label']),('qualification_failure',None,None))

    def test_valid_nonbinary_native_category_is_preserved(self):
        def mutate(rows,events,plan):
            rows[0].update(label='insufficient_evidence',admissible_label='insufficient_evidence',
                           raw_parsed_label='insufficient_evidence')
            rows[0]['raw_result']['answer']='insufficient_evidence'
        _,ref,_=self.archive.closure(mutation=mutate)
        row=readout.resolve_saved_direct(self.archive.bank,[ref])['measurements'][0]
        self.assertEqual((row['status'],row['label']),('observed','insufficient_evidence'))

    def test_instrument_protocol_ignores_endpoint_only(self):
        a=self.archive;config=json.loads(Path(a.config['path']).read_bytes());question=a.questions[0]
        first=readout.direct_instrument_protocol(config,a.source,question)
        self.assertEqual(first,readout.direct_instrument_protocol(dict(config,endpoint='elsewhere'),a.source,question))
        self.assertNotEqual(first,readout.direct_instrument_protocol(dict(config,model='other'),a.source,question))
        self.assertNotEqual(first,readout.direct_instrument_protocol(config,a.source,dict(question,text='different?')))
        self.assertNotEqual(first,readout.direct_instrument_protocol(dict(config,probe_max_tokens=7),a.source,question))

    def test_output_bound_and_missing_threshold_refused(self):
        a=self.archive
        with self.assertRaises(ValueError):readout.resolve_saved_direct(a.bank,[],maximum_result_bytes=1)
        result=readout.resolve_saved_direct(a.bank,[]);result['measurements'].pop()
        spec,peer=a.spec()
        with self.assertRaises(ValueError):readout.describe_direct(spec,peer,result)

    def test_frozen_analysis_bytes_must_match_loaded_implementation(self):
        a=self.archive;result=readout.resolve_saved_direct(a.bank,[]);spec,_=a.spec()
        value=json.loads(Path(spec['path']).read_bytes());value['analysis_source']=a.source
        ref=a.save('changed_spec',value)
        peer=a.save('changed_spec_peer',dict(accepted_frozen_derived_probe_readout_specification=True,
            specification=ref,preparation=a.input['actual'],findings=[]))
        with self.assertRaises(ValueError):readout.describe_direct(ref,peer,result)


if __name__ == '__main__':unittest.main()
