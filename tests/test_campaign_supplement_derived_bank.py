import copy
import hashlib
import json
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from causes import campaign_supplement_derived_bank as derived


class Fixture:
    def __init__(self):
        self.documents = {}
        self.family = 'derived_probe_synthetic'
        self.owner_jobs = [dict(measurement_id='original/mi27/old/early/evaluation_strongly_favors',
                               original_failure='retained_missing')]
        self.counter = 1
        self.groups = []
        for group in range(3):
            rows = [self.row('baseline0', prepared=True), self.row('baseline1', prepared=False),
                    self.row(f'treatment{group}0', prepared=True), self.row(f'treatment{group}1', prepared=True)]
            self.groups.append(rows)

    def save(self, value):
        data = core.encoded(value)
        ref = dict(path=f'/synthetic-derived-bank/{len(self.documents)}', bytes=len(data),
                   sha256=hashlib.sha256(data).hexdigest())
        self.documents[ref['path']] = data
        return ref

    def read(self, ref):
        return self.documents[ref['path']]

    def row(self, name, prepared):
        key = 'derived_probe_'+name
        row = dict(schema='causes.derived_probe_input.v1', run_id=name,
                   measurement_key=key, view_kind='neutral_summary', stage='early',
                   protocol_sha256='f'*64, runtime_authorized=False,
                   disposition='prepared' if prepared else 'unavailable',
                   view=dict(synthetic=True) if prepared else None)
        row['jobs'] = [dict(family=self.family, model='mi27', run_id=key,
            original_run_id=name, stage='early', question_id=q, seed=i,
            protocol_sha256=row['protocol_sha256'],
            measurement_id='/'.join((self.family,'mi27',key,'early',q)))
            for i,q in enumerate(derived.QUESTIONS)] if prepared else []
        if not prepared:
            row['reason'] = 'original_missing_arm'
        return row

    def closure(self, jobs, complete, unstarted, *, extension=None, counter=None):
        value = dict(schema='causes.finite_supplement_tranche_closed.v1', role='mi',
            active=0, ambiguous=[], clean_native_ending=True,
            complete_ids=complete, unstarted_ids=unstarted, full_jobs=self.save(jobs),
            consecutive_failures=self.counter if counter is None else counter,
            qualification_findings=[dict(inherited_invalid='retained')])
        if extension is not None:
            value['derived_bank_extension'] = extension
        actual = self.save(value)
        review = self.save(dict(accepted_finite_supplement_closed_custody=True,
            actual=actual, findings=[], original_denominator_preserved=True,
            all_original_attempts_retained=True, qualification_findings=value['qualification_findings']))
        return value, dict(actual=actual, review=review)

    def build(self, *, mutate_bank=None, mutate_prepared=None, resume=None):
        owner_ids = [j['measurement_id'] for j in self.owner_jobs]
        previous, predecessor = self.closure(self.owner_jobs, owner_ids, [])
        protocol = dict(schema='causes.derived_probe_protocol.v1', family=self.family, model='mi27')
        descriptors, source_counts, jobs, seen_rows = [], [], [], set()
        for group_index, rows in enumerate(self.groups):
            pairs = [dict(task_id=f'task{i}', draw_key=0, variant=f'F{group_index}',
                arms={role:{k:rows[index][k] for k in ('run_id','measurement_key','disposition')}
                      for role,index in [('baseline',i),('treatment',i+2)]}) for i in range(2)]
            ready = sum(r['disposition']=='prepared' for r in rows)
            counts = dict(assigned=len(rows), ready=ready, unavailable=len(rows)-ready,
                          requests=sum(len(r['jobs']) for r in rows), missing_slots=(len(rows)-ready)*3)
            prepared = dict(schema='causes.frozen_derived_probe_preparation.v1', runtime_authorized=False,
                assignments=self.save([{k:r[k] for k in ('run_id','view_kind','stage')} for r in rows]),
                rows=self.save(rows), rows_canonical_sha256=core.digest(rows), pairs=self.save(pairs),
                protocol=self.save(protocol), counts=counts, actual_jobs=counts['requests'])
            if mutate_prepared:
                mutate_prepared(group_index, prepared, self)
            prep_ref = self.save(prepared)
            peer = dict(accepted_frozen_derived_probe_inputs=True, preparation=prep_ref, findings=[],
                        **{k:prepared[k] for k in ('assignments','pairs','protocol','rows','counts')})
            descriptors.append(dict(actual=prep_ref, review=self.save(peer)))
            new = shared = 0
            for r in rows:
                if r['measurement_key'] in seen_rows:
                    shared += len(r['jobs'])
                else:
                    seen_rows.add(r['measurement_key']);jobs.extend(r['jobs']);new += len(r['jobs'])
            source_counts.append(dict(preparation=prep_ref,new_jobs=new,shared_jobs=shared,counts=counts))
        bank = dict(schema=derived.SCHEMA,root_authorized=True,target_role='mi',
            family=self.family,model='mi27',execution_predecessor=predecessor,
            target_output='/synthetic-derived-bank/output',inputs=descriptors,
            job_count=len(jobs),jobs_sha256=core.digest(jobs),
            ids_sha256=core.digest([j['measurement_id'] for j in jobs]),source_counts=source_counts,
            unique_assigned_slots=24,unique_missing_slots=3)
        if mutate_bank:
            mutate_bank(bank)
        bank_ref=self.save(bank)
        descriptor=dict(actual=bank_ref,review=self.save(dict(accepted_derived_supplement_bank=True,
                                                             actual=bank_ref,findings=[])))
        scope='adopted_derived_bank'
        selected=jobs
        if resume is not None:
            previous,predecessor=self.closure(self.owner_jobs+jobs,owner_ids+[j['measurement_id'] for j in jobs[:resume]],
                [j['measurement_id'] for j in jobs[resume:]],extension=dict(adoption=descriptor))
            scope='derived_bank_remaining';selected=jobs[resume:]
        plan=dict(derived_bank=descriptor,predecessor_custody=predecessor,role='mi',
            family=self.family,model='mi27',occupied_ids=previous['complete_ids'],
            initial_failures=self.counter,output_root=bank['target_output'] if resume is None else '/synthetic-derived-bank/next',
            continuation_scope=scope,maximum_new_requests=len(selected),jobs_sha256=core.digest(selected))
        return plan,previous,selected,bank


class DerivedBankTests(unittest.TestCase):
    def test_exact_union_and_shared_controls_missingness_and_failure_carry(self):
        f=Fixture();plan,previous,jobs,bank=f.build()
        result=derived.resolve(plan,previous,read=f.read)
        self.assertEqual(result['jobs'],jobs)
        self.assertEqual(len(jobs),21)
        self.assertEqual([r['new_jobs'] for r in result['source_counts']],[9,6,6])
        self.assertEqual([r['shared_jobs'] for r in result['source_counts']],[0,3,3])
        self.assertEqual(result['unique_missing_slots'],3)
        self.assertEqual(result['prior_owner_jobs'],f.owner_jobs)
        self.assertEqual(previous['consecutive_failures'],1)
        self.assertEqual(len(result['comparisons']),3)

    def test_partial_continuation_retains_cumulative_bank_and_order(self):
        f=Fixture();plan,previous,jobs,_=f.build(resume=5)
        result=derived.resolve(plan,previous,read=f.read)
        self.assertEqual(result['jobs'],jobs)
        self.assertEqual(len(jobs),16)
        self.assertEqual(len(result['cumulative_jobs']),22)
        previous['unstarted_ids'].reverse()
        with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)

    def test_failure_reset_wrong_owner_and_ambiguous_transition_refused(self):
        for key,value in [('initial_failures',0),('occupied_ids',[]),
                          ('predecessor_custody',{}),('original_reservation_transfer',{})]:
            f=Fixture();plan,previous,_,_=f.build();plan[key]=value
            with self.subTest(key=key),self.assertRaises((core.IntegrityError,KeyError)):
                derived.resolve(plan,previous,read=f.read)

    def test_changed_source_order_union_digest_and_counts_refuse(self):
        mutations=[lambda b:b['inputs'].reverse(),lambda b:b.__setitem__('jobs_sha256','0'*64),
                   lambda b:b.__setitem__('ids_sha256','0'*64),lambda b:b.__setitem__('job_count',20),
                   lambda b:b.__setitem__('unique_missing_slots',0),lambda b:b.__setitem__('root_authorized',False),
                   lambda b:b.__setitem__('unique_assigned_slots',21)]
        for mutation in mutations:
            f=Fixture();plan,previous,_,_=f.build(mutate_bank=mutation)
            with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)

    def test_shared_baseline_job_and_missing_reason_cannot_change(self):
        for missing in (False,True):
            f=Fixture()
            if missing:f.groups[1][1]['reason']='different_missing_reason'
            else:f.groups[1][0]['jobs'][0]['seed']+=1
            plan,previous,_,_=f.build()
            with self.assertRaisesRegex(core.IntegrityError,'shared_control'):
                derived.resolve(plan,previous,read=f.read)

    def test_duplicate_treatment_is_not_a_shared_control(self):
        f=Fixture();f.groups[1][2]=copy.deepcopy(f.groups[0][2]);plan,previous,_,_=f.build()
        with self.assertRaisesRegex(core.IntegrityError,'shared_control'):
            derived.resolve(plan,previous,read=f.read)

    def test_dropped_question_reordered_assignment_and_changed_pairs_refused(self):
        def assignment(i,p,f):
            if i==1:
                values=json.loads(f.read(p['assignments']));values.reverse();p['assignments']=f.save(values)
        def pair(i,p,f):
            if i==1:
                values=json.loads(f.read(p['pairs']));values[0]['arms']['baseline']['run_id']='other';p['pairs']=f.save(values)
        for mutate in (assignment,pair):
            f=Fixture();plan,previous,_,_=f.build(mutate_prepared=mutate)
            with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)
        f=Fixture();f.groups[1][2]['jobs'].pop();plan,previous,_,_=f.build()
        with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)

    def test_changed_protocol_or_row_hash_refused(self):
        def protocol(i,p,f):
            if i==1:
                value=json.loads(f.read(p['protocol']));value['extra_source']='changed';p['protocol']=f.save(value)
        def row_hash(i,p,f):
            if i==1:p['rows_canonical_sha256']='0'*64
        for mutate in (protocol,row_hash):
            f=Fixture();plan,previous,_,_=f.build(mutate_prepared=mutate)
            with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)

    def test_no_replay_budget_and_exact_current_custody(self):
        for key,value in [('maximum_new_requests',True),('maximum_new_requests',22),
                          ('jobs_sha256','0'*64),('continuation_scope','original')]:
            f=Fixture();plan,previous,_,_=f.build();plan[key]=value
            with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)
        f=Fixture();plan,previous,_,_=f.build();previous['consecutive_failures']=0
        with self.assertRaises(core.IntegrityError):derived.resolve(plan,previous,read=f.read)

    def test_public_contract_explicit_branch_and_threshold_preserved(self):
        f=Fixture();plan,previous,jobs,_=f.build()
        evidence=f.save(dict(synthetic=True));identity={'provider':'synthetic'};process={'pid':42}
        custody=f.save(dict(schema='causes.completed_actor_direct_probe_custody.v1',active_actor_roles=[],
            old_actor_owners_absent=True,no_unresolved_actors=True,completed_actor_records=[evidence],
            provider_identity=identity,provider_process=process))
        descriptor=dict(actual=custody,review=f.save(dict(accepted_completed_actor_direct_probe_custody=True,
                                                        actual=custody,findings=[])))
        plan.update(mode=direct.MODE,max_clients=8,completed_actor_custody=descriptor,
            provider_identity=identity,owner_identity={'pid':123},source_epoch='f'*64,
            maximum_admission_attempts=84,maximum_captures=21,maximum_capture_uses=8)
        contract=dict(schema=direct.CONTRACT_SCHEMA,active_actor_roles=[],max_clients=8,server_max_sequences=16,
            completed_actor_custody=descriptor,predecessor_custody=plan['predecessor_custody'],
            maximum_observation_age_seconds=30,claim_ready_age_seconds=29,
            provider_identity=identity,provider_process=process,baseline_preemptions=0,derived_bank=plan['derived_bank'])
        direct.validate_contract(plan,contract,jobs,initial_failures=1,max_clients=8,read=f.read)
        contract['derived_bank']={}
        with self.assertRaisesRegex(core.IntegrityError,'derived_bank_contract'):
            direct.validate_contract(plan,contract,jobs,initial_failures=1,max_clients=8,read=f.read)
        plan['initial_failures']=3
        with self.assertRaisesRegex(core.IntegrityError,'failure_carry'):
            direct.validate_contract(plan,contract,jobs,initial_failures=3,max_clients=8,read=f.read)


if __name__=='__main__':
    unittest.main()
