import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mixed_owner_under_test',
    ROOT/'src/causes/campaign_supplement_mixed_owner.py')
mixed = importlib.util.module_from_spec(spec); spec.loader.exec_module(mixed)


class Memory:
    def __init__(self): self.files = {}; self.serial = 0
    def put(self, value, name='data'):
        self.serial += 1; raw = mixed.encoded(value)
        ref = dict(path=f'/synthetic/{self.serial}_{name}', bytes=len(raw),
                   sha256=hashlib.sha256(raw).hexdigest())
        self.files[ref['path']] = raw
        return ref
    def read(self, ref): return self.files[ref['path']]
    def get(self, ref): return json.loads(self.read(ref))
    def accepted(self, value, flag, field='actual', **extra):
        ref = self.put(value)
        return dict(actual=ref, review=self.put({flag:True, field:ref, 'findings':[], **extra}))


def job(run, question='evaluation_strongly_favors', family='candidate_saved_self_v1'):
    result = dict(family=family, model='mi27', run_id=run, stage='early', question_id=question,
                  seed=17, original_seed=11, context_canonical_sha256='a'*64,
                  request_body_sha256='b'*64, request_file_sha256='c'*64,
                  question_canonical_sha256='d'*64)
    result['measurement_id'] = '/'.join(result[k] for k in
        ('family','model','run_id','stage','question_id'))
    return result


def fixture(change=lambda stage, data: None, *, partial=False, same_bank=False, derived_resume=False):
    m = Memory(); helper = m.put({'accepted_helper':True}, 'source')
    source = m.put({'source':True}); model = 'mi27'; original_jobs = [job('shared-run')]
    change('base_jobs', original_jobs)
    base = dict(schema='causes.finite_supplement_tranche_closed.v1', role='mi',
        mode='completed_actor_direct_probe', active=0, ambiguous=[], clean_native_ending=True,
        unresolved_claimed_ids=[], full_jobs=m.put(original_jobs), complete_ids=mixed._ids(original_jobs),
        unstarted_ids=[], terminal=len(original_jobs), occupied=len(original_jobs), consecutive_failures=0,
        qualification_findings=[], cumulative_invalid_ids=[], native_invocations=[], source=source)
    base['plan'] = m.put({'sources':{'driver':source}})

    def close(saved):
        return m.accepted(saved, 'accepted_finite_supplement_closed_custody',
            accepted_completed_actor_saved_closure=True, source=saved['source'],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            qualification_findings=saved['qualification_findings'])

    base_ref = close(base)
    protocol = m.put({'schema':'causes.derived_probe_protocol.v1','family':'derived_probe_family',
                      'model':model})
    rows = []
    for index in range(4):
        run = 'shared-run' if index == 0 else 'neutral-run-'+str(index)
        key = 'derived_probe_key_'+str(index)
        row = dict(schema='causes.derived_probe_input.v1', run_id=run,
            measurement_key=key, view_kind='neutral_initial', stage='early', runtime_authorized=False,
            disposition='prepared', view=m.put({'view':index}), view_canonical_sha256='e'*64,
            context_canonical_sha256='f'*64, protocol_sha256='9'*64, jobs=[])
        for question in mixed.QUESTIONS:
            j = job(key, question, 'derived_probe_family')
            j.update(original_run_id=run, derived_view_canonical_sha256=row['view_canonical_sha256'],
                     context_canonical_sha256=row['context_canonical_sha256'],
                     protocol_sha256=row['protocol_sha256'])
            row['jobs'].append(j)
        if index == 3:
            row.update(disposition='unavailable', view=None, jobs=[], reason='saved context unavailable')
        rows.append(row)
    change('rows', rows)
    pairs = []
    for a,b in ((0,1),(2,3)):
        pairs.append(dict(arms={name:{k:rows[index][k] for k in
            ('run_id','measurement_key','disposition')} for name,index in (('baseline',a),('treatment',b))}))
    assignments = [{k:r[k] for k in ('run_id','view_kind','stage')} for r in rows]
    prep = dict(schema='causes.frozen_derived_probe_preparation.v1', runtime_authorized=False,
        rows=m.put(rows), pairs=m.put(pairs), assignments=m.put(assignments), protocol=protocol,
        counts={'assigned':4,'ready':3,'unavailable':1,'requests':9,'missing_slots':3},
        rows_canonical_sha256=mixed.digest(rows))
    inp = m.accepted(prep, 'accepted_frozen_derived_probe_inputs', 'preparation',
                     **{k:prep[k] for k in ('rows','pairs','assignments','protocol','counts')})
    derived_jobs = [copy.deepcopy(j) for row in rows for j in row['jobs']]
    bank = dict(schema='causes.derived_supplement_bank_adoption.v1', root_authorized=True,
        target_role='mi', family='derived_probe_family', model=model, execution_predecessor=base_ref,
        inputs=[inp], job_count=len(derived_jobs), jobs_sha256=mixed.digest(derived_jobs),
        ids_sha256=mixed.digest(mixed._ids(derived_jobs)))
    bank_ref = m.accepted(bank, 'accepted_derived_supplement_bank')

    def append(prior_ref, prior, before, jobs, kind, stage, partial_cut=False):
        full = copy.deepcopy(before+jobs); change(stage+'_full', full)
        full_ref = m.put(full)
        plan = dict(schema='causes.completed_actor_driver_plan.v1', role='mi', model=model,
            family=jobs[0]['family'] if jobs else 'candidate_saved_self_v1',
            predecessor_custody=prior_ref, occupied_ids=prior['complete_ids'],
            initial_failures=prior['consecutive_failures'], sources={'driver':source},
            maximum_new_requests=len(full)-len(prior['complete_ids']),
            jobs_sha256=mixed.digest(full[len(prior['complete_ids']):]), output_root='/synthetic/output/'+stage)
        if kind == 'derived':
            plan.update(family=bank['family'],derived_bank=bank_ref,
                        continuation_scope='adopted_derived_bank' if jobs else 'derived_bank_remaining')
            extension = dict(schema=mixed.DERIVED, adoption=bank_ref,
                execution_predecessor=base_ref, previous_closed=base_ref,
                previous_full_jobs=prior['full_jobs'], prior_count=len(before), appended_count=len(jobs),
                appended_jobs_sha256=mixed.digest(jobs), appended_ids_sha256=mixed.digest(mixed._ids(jobs)),
                cumulative_count=len(full), cumulative_jobs_sha256=mixed.digest(full), full_jobs=full_ref,
                prior_prefix_unchanged=True, native_families_preserved=True)
            if not jobs:
                full_ref=prior['full_jobs']
                extension=copy.deepcopy(prior['derived_bank_extension'])
            ext_key = 'derived_bank_extension'
        elif jobs:
            transfer = dict(schema='causes.original_supplement_reservation_transfer.v1',
                execution_predecessor=prior_ref, target_role='mi', target_output=plan['output_root'],
                family=plan['family'], model=model, job_count=len(jobs), jobs_sha256=mixed.digest(jobs))
            plan['original_reservation_transfer'] = m.accepted(transfer,
                'accepted_original_supplement_reservation_transfer')
            extension = dict(schema=mixed.ORIGINAL, previous_closed=prior_ref,
                previous_full_jobs=prior['full_jobs'], transfer=plan['original_reservation_transfer'],
                source_family=plan['family'], model=model, prior_count=len(before), appended_count=len(jobs),
                appended_jobs_sha256=mixed.digest(jobs), appended_ids_sha256=mixed.digest(mixed._ids(jobs)),
                cumulative_count=len(full), cumulative_jobs_sha256=mixed.digest(full), full_jobs=full_ref,
                prior_prefix_unchanged=True, native_families_preserved=True)
            ext_key = 'original_bank_extension'
        else:
            extension = None; ext_key = 'original_bank_extension'
        if extension is not None: change(stage+'_extension', extension)
        plan_ref = m.put(plan)
        done_count = len(full)-1 if partial_cut else len(full)
        new_ids = mixed._ids(full)[len(prior['complete_ids']):done_count]
        result = m.put({'result':dict(active=0, consecutive_failures=0,
                                     new_final=len(new_ids), completed_ids=new_ids)})
        invocation = dict(plan=plan_ref, source=source, result=result, process_absent=True, pid=42,
            family=plan['family'], model=model, jobs_sha256=plan['jobs_sha256'])
        if kind == 'derived': invocation['derived_bank'] = bank_ref
        elif jobs: invocation['original_reservation_transfer'] = plan['original_reservation_transfer']
        saved = dict(schema=base['schema'], role='mi', mode=base['mode'], active=0, ambiguous=[],
            clean_native_ending=True, unresolved_claimed_ids=[], source=source, plan=plan_ref,
            output=plan['output_root'], full_jobs=full_ref, complete_ids=mixed._ids(full)[:done_count],
            unstarted_ids=mixed._ids(full)[done_count:], terminal=done_count, occupied=done_count,
            consecutive_failures=0, qualification_findings=[], inherited_qualification_findings=[],
            current_qualification_findings=[], cumulative_invalid_ids=[], prior_invalid_ids=[],
            new_invalid_ids=[], prior_closed=prior_ref['actual'], prior_closed_review=prior_ref['review'],
            native_invocations=prior['native_invocations']+[invocation], native_result=result,
            **{ext_key:extension})
        change(stage+'_saved', saved)
        return close(saved), saved, full

    dref, dclosed, all_derived = append(base_ref, base, original_jobs, derived_jobs, 'derived', 'derived',
                                        partial_cut=derived_resume)
    derived_links=[dref]
    if derived_resume:
        dref,dclosed,all_derived=append(dref,dclosed,all_derived,[],'derived','derived_resume')
        derived_links.append(dref)
    new_original = [job('later-one'), job('later-two')]
    change('new_original', new_original)
    oref, oclosed, full = append(dref, dclosed, all_derived, new_original, 'original', 'original',
                                partial_cut=partial or same_bank)
    links = derived_links+[oref]
    if same_bank:
        oref, oclosed, full = append(oref, oclosed, full, [], 'original', 'resume')
        links.append(oref)
    proof = dict(schema=mixed.SCHEMA, head=oref, original_base=base_ref, links=links, model=model,
        counts=dict(owner=len(full),original=len(original_jobs)+len(new_original),derived=len(derived_jobs)),
        owner_ids_sha256=mixed.digest(mixed._ids(full)),
        original_ids_sha256=mixed.digest(mixed._ids(original_jobs+new_original)),
        derived_ids_sha256=mixed.digest(mixed._ids(derived_jobs)))
    change('proof', proof)
    ref = m.accepted(proof, 'accepted_mixed_supplement_owner_partition', source=helper, head=oref)
    return m, ref, oref, helper


class MixedOwnerTests(unittest.TestCase):
    def run_fixture(self, change=lambda s,d:None, **kwargs):
        m, ref, head, source = fixture(change, **kwargs)
        return mixed.resolve(ref, predecessor=head, source=source, read=m.read)

    def test_domains_coexist_and_full_history_remains(self):
        result = self.run_fixture()
        self.assertEqual((len(result['owner_jobs']),len(result['original_jobs']),len(result['derived_jobs'])),(12,3,9))
        self.assertEqual(result['derived_jobs'][1]['original_run_id'],result['original_jobs'][0]['run_id'])
        self.assertEqual(result['derived_jobs'][1]['question_id'],result['original_jobs'][0]['question_id'])
        self.assertNotEqual(result['derived_jobs'][1]['context_canonical_sha256'],result['original_jobs'][0]['context_canonical_sha256'])

    def test_same_bank_tail(self): self.assertEqual(len(self.run_fixture(same_bank=True)['links']),3)

    def test_same_adoption_derived_suffix_then_original_append(self):
        result=self.run_fixture(derived_resume=True)
        self.assertEqual([link['kind'] for link in result['links']],['derived','derived','original'])
        self.assertEqual(len(result['owner_jobs']),12)

    def test_partial_is_import_only(self):
        m, ref, head, source = fixture(partial=True)
        with self.assertRaisesRegex(mixed.MixedOwnerError,'incomplete_execution'):
            mixed.resolve(ref,predecessor=head,source=source,read=m.read)
        result=mixed.resolve(ref,predecessor=head,source=source,read=m.read,require_complete=False)
        self.assertEqual(len(result['owner_jobs']),12)

    def test_changed_old_prefix(self):
        def change(stage,data):
            if stage=='original_full': data[0]['seed']+=1
        with self.assertRaisesRegex(mixed.MixedOwnerError,'changed_owner_prefix'):self.run_fixture(change)

    def test_derived_request_not_reclassified(self):
        def change(stage,data):
            if stage=='derived_full':data[1]['request_body_sha256']='0'*64
        with self.assertRaisesRegex(mixed.MixedOwnerError,'derived_append_changed'):self.run_fixture(change)

    def test_original_namespace_collision(self):
        def change(stage,data):
            if stage=='new_original':data[0]=job('shared-run',family='requested_five_historical_self_v1')
        with self.assertRaisesRegex(mixed.MixedOwnerError,'original_append_alias'):self.run_fixture(change)

    def test_source_peer_and_head_binding(self):
        m,ref,head,source=fixture()
        with self.assertRaisesRegex(mixed.MixedOwnerError,'source_and_head'):
            mixed.resolve(ref,predecessor=head,source=m.put({'other':True}),read=m.read)

    def test_counter_and_qualification_history(self):
        for field,value in [('consecutive_failures',2),('inherited_qualification_findings',[{'missing':'old'}])]:
            def change(stage,data):
                if stage=='original_saved':data[field]=value
            with self.subTest(field=field),self.assertRaises(mixed.MixedOwnerError):self.run_fixture(change)

    def test_extension_digest_and_predecessor(self):
        for field,value in [('appended_ids_sha256','0'*64),('prior_prefix_unchanged',False),('prior_count',0)]:
            def change(stage,data):
                if stage=='original_extension':data[field]=value
            with self.subTest(field=field),self.assertRaises(mixed.MixedOwnerError):self.run_fixture(change)

    def test_omitted_derived_chain(self):
        def change(stage,data):
            if stage=='proof':data['links']=data['links'][1:]
        with self.assertRaises(mixed.MixedOwnerError):self.run_fixture(change)

    def test_metadata_bound_before_read(self):
        m,ref,head,source=fixture(); bad=copy.deepcopy(ref);bad['actual']['bytes']=8*1024**2+1
        calls=[]
        with self.assertRaisesRegex(mixed.MixedOwnerError,'pin_bound'):
            mixed.resolve(bad,predecessor=head,source=source,read=lambda r:calls.append(r))
        self.assertEqual(calls,[])

    def test_nonfinite_exponent(self):
        m,ref,head,source=fixture();raw=b'{"extra":1e999}'
        bad=dict(path='/synthetic/nonfinite',bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        m.files[bad['path']]=raw
        with self.assertRaisesRegex(mixed.MixedOwnerError,'nonfinite'):
            mixed.resolve(dict(actual=bad,review=ref['review']),predecessor=head,source=source,read=m.read)


if __name__ == '__main__': unittest.main()
