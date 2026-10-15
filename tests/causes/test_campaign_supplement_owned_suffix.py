"""Synthetic closed banks only: no production reader, executor or filesystem I/O."""
import copy
import hashlib
import json
import unittest

from causes import campaign_supplement_owned_suffix as suffix


def native_digest(value):
    """Independent fixture encoding specified by the unchanged native queue."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


class Saved:
    def __init__(self):
        self.data = {}
        self.pins = []
        self.reads = []

    def put(self, value):
        path = '/fixture/item_' + str(len(self.data))
        data = value if type(value) is bytes else json.dumps(value, sort_keys=True).encode()
        ref = dict(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.data[path] = data; self.pins.append(ref)
        return ref

    def read(self, ref):
        self.reads.append(ref['path'])
        return self.data[ref['path']]

    def value(self, ref):
        return json.loads(self.data[ref['path']])

    def change(self, ref, **fields):
        return self.put(dict(self.value(ref), **fields))

    def accepted(self, actual):
        ref = self.put(actual)
        peer = self.put(dict(accepted_finite_supplement_closed_custody=True,
            accepted_completed_actor_saved_closure=True, actual=ref, source=actual['source'],
            findings=[], all_original_attempts_retained=True, original_denominator_preserved=True,
            qualification_findings=actual['qualification_findings']))
        return dict(actual=ref, review=peer)


def job(i):
    v = dict(family='candidate_saved_self_v1', model='mi27', run_id='run_' + str(i),
             stage='early', question_id='evaluation_strongly_favors', seed=1700+i)
    v['measurement_id'] = '/'.join(v[k] for k in ('family', 'model', 'run_id', 'stage', 'question_id'))
    return v


def base_actual(s, full, terminal, *, invalid=None):
    ids = [j['measurement_id'] for j in s.value(full)]
    findings = [] if invalid is None else [dict(measurement_id=invalid, reason='preserved invalid final')]
    return dict(source=s.audit, role='mi', mode='completed_actor_direct_probe',
        clean_native_ending=True, active=0, ambiguous=[], unresolved_claimed_ids=[],
        quarantined=False, consecutive_failures=0, all_original_attempts_retained=True,
        original_denominator_preserved=True, terminal=terminal, occupied=terminal,
        complete_ids=ids[:terminal], unstarted_ids=ids[terminal:], full_jobs=full,
        qualification_findings=findings, inherited_qualification_findings=[],
        current_qualification_findings=findings, current_tranche_qualified=invalid is None,
        new_invalid_ids=[] if invalid is None else [invalid], native_invocations=[],
        stop_reason=None)


def close_run(s, plan_ref, full, terminal, attempts, captures, *, failure=False,
              reason="AdmissionHold('operator_pause')"):
    plan = s.value(plan_ref); previous = s.value(plan['predecessor_custody']['actual'])
    ids = [j['measurement_id'] for j in s.value(full)]
    invalid = ids[terminal-1] if failure else None
    actual = base_actual(s, full, terminal, invalid=invalid)
    actual.update(plan=plan_ref, output=plan['output_root'],
        prior_closed=plan['predecessor_custody']['actual'],
        prior_closed_review=plan['predecessor_custody']['review'],
        native_invocations=previous['native_invocations'] + [dict(plan=plan_ref)],
        inherited_qualification_findings=previous['qualification_findings'],
        qualification_findings=previous['qualification_findings'] + actual['current_qualification_findings'],
        consecutive_failures=int(failure), stop_reason=reason,
        original_journal=s.put(b'synthetic complete journal'))
    actual['native_result'] = s.put(dict(attempts=attempts, captures=captures,
        result=dict(active=0, consecutive_failures=int(failure), stop_reason=reason,
                    remaining_unclaimed=len(ids)-terminal,
                    new_final=terminal-len(plan['occupied_ids']))))
    actual['original_bank_extension'] = None
    if plan['continuation_scope'] == 'transferred_original_reservation':
        prior_count = len(plan['occupied_ids'])
        actual['original_bank_extension'] = dict(
            schema='causes.original_supplement_owner_bank_extension.v1',
            previous_closed=plan['predecessor_custody'], previous_full_jobs=previous['full_jobs'],
            transfer=plan['original_reservation_transfer'], full_jobs=full,
            prior_count=prior_count, appended_count=len(ids)-prior_count, cumulative_count=len(ids),
            appended_jobs_sha256=native_digest(s.value(full)[prior_count:]),
            appended_ids_sha256=native_digest(ids[prior_count:]), cumulative_jobs_sha256=native_digest(s.value(full)),
            prior_prefix_unchanged=True, native_families_preserved=True)
    return s.accepted(actual)


def next_plan(s, first_ref, predecessor, policy_ref, consumed_attempts, consumed_captures):
    first = s.value(first_ref); actual = s.value(predecessor['actual']); policy = s.value(policy_ref)
    full = s.value(actual['full_jobs']); jobs = full[len(actual['complete_ids']):]
    current = copy.deepcopy(first)
    current.pop('original_reservation_transfer')
    current.update(continuation_scope=suffix.SCOPE, predecessor_custody=predecessor,
        output_root='/fixture/suffix_' + str(len(s.data)), occupied_ids=actual['complete_ids'],
        jobs_sha256=native_digest(jobs), maximum_new_requests=len(jobs),
        initial_failures=actual['consecutive_failures'],
        maximum_admission_attempts=min(4*len(jobs),policy['maximum_admission_attempts']-consumed_attempts),
        maximum_captures=min(len(jobs),policy['maximum_captures']-consumed_captures))
    current['sources'].update(driver=s.put(b'new reviewed driver'),
                              owned_suffix=s.put(b'generic suffix validator'))
    previous_plan = s.value(actual['plan'])
    pause_path = previous_plan['output_root'] + '/PAUSE'
    pause = dict(path=pause_path, bytes=1, sha256=hashlib.sha256(b'x').hexdigest())
    s.data[pause_path] = b'x'; s.pins.append(pause)
    current['current_predecessor_pause'] = pause
    current['pause_paths'] = [p for p in previous_plan['pause_paths'] if p != pause_path] + [current['output_root']+'/PAUSE']
    peer = s.put(dict(accepted_original_owned_suffix_runtime_source=True,
        sources=current['sources'], suffix_policy=policy_ref, findings=[]))
    adoption = s.put(dict(accepted_original_owned_suffix_runtime_source=True,
        runtime_authorized=False, sources=current['sources'], suffix_policy=policy_ref,
        independent_review=peer, findings=[]))
    current['original_suffix_transition'] = s.put(dict(schema=suffix.TRANSITION,
        policy=policy_ref, predecessor=predecessor, predecessor_stop_reason=actual['stop_reason'],
        current_predecessor_pause=pause, source_review=peer, root_source_adoption=adoption))
    current['execution_pins'] = copy.deepcopy(s.pins)
    return current


def fixture(prior_count=2, added=3, completed=1, attempts=4, captures=1, failure=False,
            job_factory=job):
    s = Saved(); s.audit = s.put(b'unchanged closed auditor')
    full_jobs = [job_factory(i) for i in range(prior_count+added)]
    old_full = s.put(full_jobs[:prior_count]); full = s.put(full_jobs)
    prior = base_actual(s, old_full, prior_count, invalid=full_jobs[0]['measurement_id'])
    prior['native_invocations'] = [dict(plan=s.put(dict(old=True)))]
    previous = s.accepted(prior)
    transfer = s.put(dict(execution_predecessor=previous, target_output='/fixture/first'))
    transfer_peer = s.put(dict(accepted_original_supplement_reservation_transfer=True, actual=transfer, findings=[]))
    first = {k:s.put(dict(value=k)) for k in suffix.PRESERVED_FIELDS}
    first.update(role='mi', mode='completed_actor_direct_probe', family='candidate_saved_self_v1',
        model='mi27', endpoint='http://fixture.invalid', output_root='/fixture/first',
        local_free_space_floor_bytes=40*1024**3, max_clients=8,
        continuation_scope='transferred_original_reservation', predecessor_custody=previous,
        occupied_ids=[j['measurement_id'] for j in full_jobs[:prior_count]],
        jobs_sha256=native_digest(full_jobs[prior_count:]), maximum_new_requests=added,
        maximum_admission_attempts=4*added, maximum_captures=added, maximum_capture_uses=8,
        initial_failures=0, retain_admission_after_healthy_harvest=True,
        pause_paths=['/fixture/first/PAUSE'],
        sources={key:s.put(key.encode()) for key in ('driver','native_queue','completed_actor','budget','source_lineage')},
        original_reservation_transfer=dict(actual=transfer, review=transfer_peer))
    first_ref = s.put(first)
    closed = close_run(s, first_ref, full, prior_count+completed, attempts, captures, failure=failure)
    policy = s.put(dict(schema=suffix.POLICY, runtime_authorized=False, first_transfer_plan=first_ref,
        prior_count=prior_count, appended_count=added, full_count=prior_count+added,
        maximum_admission_attempts=4*added, maximum_captures=added))
    plan = next_plan(s, first_ref, closed, policy, attempts, captures)
    return s, plan, first_ref, policy


def replace_closed(s, plan, **fields):
    actual = dict(s.value(plan['predecessor_custody']['actual']), **fields)
    descriptor = s.accepted(actual)
    transition = s.value(plan['original_suffix_transition'])
    transition.update(predecessor=descriptor, predecessor_stop_reason=actual['stop_reason'])
    plan['predecessor_custody'] = descriptor
    plan['original_suffix_transition'] = s.put(transition)
    plan['execution_pins'] = copy.deepcopy(s.pins)


def technical_timeout(s, plan, **execution_changes):
    """Exact saved timeout profile, without manufacturing a healthy capture."""
    actual = s.value(plan['predecessor_custody']['actual'])
    previous_plan = s.value(actual['plan'])
    reason = "IntegrityError('remote_transport_or_source_refusal')"
    def named(path, value):
        data = json.dumps(value, sort_keys=True).encode()
        ref = dict(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        s.data[path] = data; s.pins.append(ref)
        return ref
    closed = named(previous_plan['output_root'] + '/control_transport/CLOSED.json',
        dict(reason='remote_failure', cause=reason, cleanup_error=None,
             supervisor_returncode=0, reconnect=False, automatic_retry=False))
    execution = dict(source_error=None, model_calls=0, automatic_retry=False,
        transport=dict(reason='timeout', complete=False, child_reaped=True,
                       cleanup_errors=[], returncode=-9))
    execution.update(execution_changes)
    er = named(previous_plan['output_root'] + '/remote_records/call_000070/000004_execution', execution)
    native = s.value(actual['native_result'])
    native.update(control_transport=closed, remote_calls=70)
    native['result']['stop_reason'] = reason
    nr = s.put(native); complete = s.put(dict(all_actors_succeeded=True))
    terminal = s.put(dict(terminal_refs={'DRIVER_RESULT.json':nr}, journal=actual['original_journal']))
    review = s.put(dict(accepted_exact_technical_ending=True, driver_result=nr,
        durable_complete=complete, execution=er, transport_closed=closed, terminal_peer=terminal,
        live_sources_modified=False, no_new_science_or_provider_calls=True,
        not_authority_to_reinvoke_old_binder=True))
    replace_closed(s, plan, native_result=nr, durable_complete=complete, stop_reason=reason)
    t = s.value(plan['original_suffix_transition'])
    t.update(technical_ending_review=review, current_predecessor_pause=None,
             root_source_adoption=s.change(t['root_source_adoption'], technical_ending_review=review))
    plan['current_predecessor_pause'] = None
    plan['original_suffix_transition'] = s.put(t)
    plan['execution_pins'] = copy.deepcopy(s.pins)
    return review


class OwnedSuffixTests(unittest.TestCase):
    def check(self, s, plan, **kwargs):
        return suffix.resolve_original_owned_suffix(plan, read=s.read, **kwargs)

    def refuse(self, s, plan, text=None, **kwargs):
        with self.assertRaises(suffix.SuffixError) as caught:
            self.check(s, plan, **kwargs)
        if text:
            self.assertIn(text, str(caught.exception))

    def test_native_digest_spacing_unicode_and_nested_values(self):
        value = [{'z': ['café', {'a': 1}], 'a': 'λ'}]
        expected_bytes = '[{"a": "λ", "z": ["café", {"a": 1}]}]'.encode()
        self.assertEqual(suffix.digest(value), hashlib.sha256(expected_bytes).hexdigest())
        compact_ascii = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        self.assertNotEqual(suffix.digest(value), hashlib.sha256(compact_ascii).hexdigest())
        with self.assertRaises(ValueError):
            suffix.digest({'bad': float('nan')})

    def test_native_encoded_nested_bank_resolves_without_rewriting_jobs(self):
        def nested_job(i):
            item = job(i)
            item['context_metadata'] = {'language': 'café λ', 'values': [i, {'ok': True}]}
            return item
        s, plan, _, _ = fixture(job_factory=nested_job)
        result = self.check(s, plan)
        self.assertEqual(result['jobs'], [nested_job(3), nested_job(4)])
        self.assertEqual(plan['jobs_sha256'], native_digest(result['jobs']))

    def test_ordered_remainder_and_inherited_invalid_are_unchanged(self):
        s, plan, _, _ = fixture(); before = copy.deepcopy(s.data)
        result = self.check(s, plan)
        self.assertEqual(result['jobs'], [job(3),job(4)])
        self.assertEqual(result['used_attempts'],4); self.assertEqual(result['used_captures'],1)
        self.assertEqual(result['remaining_attempts'],8); self.assertEqual(result['remaining_captures'],2)
        self.assertEqual(len(result['qualification_findings']),1)
        self.assertFalse(result['runtime_authorized']); self.assertEqual(s.data,before)
        self.assertEqual(len(s.reads),len(set(s.reads)))

    def test_exact_7314_cardinality_and_30_11_consumption(self):
        s, plan, _, _ = fixture(5175,7314,12,30,11)
        result=self.check(s,plan)
        self.assertEqual(len(result['jobs']),7302)
        self.assertEqual(result['complete_ids'],[job(i)['measurement_id'] for i in range(5187)])
        self.assertEqual((result['remaining_attempts'],result['remaining_captures']),(29208,7302))

    def test_accepted_timeout_keeps_failed_observation_and_never_replays_finals(self):
        s,p,_,_=fixture(5175,7314,12,30,11); technical_timeout(s,p)
        result=self.check(s,p)
        self.assertEqual((len(result['jobs']),result['used_attempts'],result['used_captures']),
                         (7302,30,11))
        self.assertEqual((result['remaining_attempts'],result['remaining_captures']),(29208,7302))
        self.assertEqual(result['jobs'][0],job(5187))

    def test_timeout_review_does_not_accept_source_mutation_or_incomplete_cleanup(self):
        for fields in [dict(source_error='changed'),dict(model_calls=True),dict(automatic_retry=True),
                       dict(transport=dict(reason='timeout',complete=False,child_reaped=False,
                                           cleanup_errors=[],returncode=-9))]:
            with self.subTest(fields=fields):
                s,p,_,_=fixture(); technical_timeout(s,p,**fields)
                self.refuse(s,p,'technical_ending')

    def test_timeout_proof_cannot_substitute_unrelated_remote_generation(self):
        s,p,_,_=fixture(); technical_timeout(s,p)
        actual=s.value(p['predecessor_custody']['actual'])
        nr=s.change(actual['native_result'],remote_calls=69)
        replace_closed(s,p,native_result=nr)
        self.refuse(s,p,'technical_ending')

    def test_failed_final_stays_occupied_and_inherits_failure_counter(self):
        s, plan, _, _=fixture(failure=True); result=self.check(s,plan)
        self.assertEqual(result['consecutive_failures'],1)
        self.assertEqual(result['jobs'],[job(3),job(4)])
        self.assertEqual(len(result['qualification_findings']),2)

    def test_second_continuation_deducts_every_child_budget(self):
        s, plan, first, policy=fixture(); previous=s.put(plan)
        descriptor=close_run(s,previous,s.value(plan['predecessor_custody']['actual'])['full_jobs'],4,3,1)
        next_one=next_plan(s,first,descriptor,policy,7,2)
        result=self.check(s,next_one)
        self.assertEqual(result['jobs'],[job(4)])
        self.assertEqual((result['used_attempts'],result['used_captures']),(7,2))
        self.assertEqual((result['remaining_attempts'],result['remaining_captures']),(4,1))

    def test_nonterminal_ambiguous_and_quarantined_custody_refused(self):
        for change in (dict(active=1),dict(clean_native_ending=False),dict(ambiguous=['x']),
                       dict(unresolved_claimed_ids=['x']),dict(quarantined=True),dict(consecutive_failures=3)):
            with self.subTest(change=change):
                s,p,_,_=fixture();replace_closed(s,p,**change);self.refuse(s,p,'nonterminal')

    def test_failure_counter_reset_refused(self):
        s,p,_,_=fixture(failure=True);p['initial_failures']=0;self.refuse(s,p,'current_predecessor')

    def test_replayed_or_reordered_ids_and_budget_reset_refused(self):
        for field,value in [('maximum_new_requests',3),('maximum_admission_attempts',12),
                            ('maximum_captures',3),('jobs_sha256',native_digest([job(4),job(3)]))]:
            with self.subTest(field=field):
                s,p,_,_=fixture();p[field]=value;self.refuse(s,p,'remaining_budget')
        s,p,_,_=fixture();p['occupied_ids']=p['occupied_ids'][:-1];self.refuse(s,p,'current_predecessor')

    def test_partial_claim_never_becomes_unstarted(self):
        s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual'])
        replace_closed(s,p,unresolved_claimed_ids=[a['unstarted_ids'][0]])
        self.refuse(s,p,'nonterminal')

    def test_output_reuse_refused(self):
        s,p,_,_=fixture();p['output_root']='/fixture/first'
        p['pause_paths']=['/fixture/first/PAUSE'];self.refuse(s,p,'output_reuse')

    def test_original_transfer_cannot_be_readopted(self):
        s,p,first,_=fixture();p['original_reservation_transfer']=s.value(first)['original_reservation_transfer']
        self.refuse(s,p,'no_readoption')

    def test_native_sources_immutable_even_with_new_approval(self):
        for key in ('native_queue','completed_actor','source_lineage'):
            with self.subTest(key=key):
                s,p,_,_=fixture();p['sources'][key]=s.put(b'changed native');self.refuse(s,p,'sources_changed')

    def test_operational_source_change_requires_exact_source_peer(self):
        s,p,_,_=fixture();p['sources']['budget']=s.put(b'faster budget');self.refuse(s,p,'source_acceptance')

    def test_whole_prior_native_history_must_be_retained(self):
        s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual'])
        replace_closed(s,p,native_invocations=a['native_invocations'][-1:]);self.refuse(s,p,'history_changed')

    def test_qualification_findings_cannot_disappear(self):
        s,p,_,_=fixture();replace_closed(s,p,qualification_findings=[]);self.refuse(s,p,'history_changed')

    def test_ordered_closed_partition_cannot_change(self):
        s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual'])
        replace_closed(s,p,unstarted_ids=list(reversed(a['unstarted_ids'])));self.refuse(s,p,'partition')

    def test_bool_and_invalid_counter_aliases_refused(self):
        for key in ('maximum_new_requests','maximum_admission_attempts','maximum_captures','maximum_capture_uses','initial_failures'):
            with self.subTest(key=key):
                s,p,_,_=fixture();p[key]=True;self.refuse(s,p)
        for key in ('active','terminal','occupied','consecutive_failures'):
            with self.subTest(closed_key=key):
                s,p,_,_=fixture();replace_closed(s,p,**{key:True});self.refuse(s,p)

    def test_native_result_consumption_and_closed_result_must_agree(self):
        for change in (dict(attempts=True),dict(attempts=13),dict(captures=5)):
            with self.subTest(change=change):
                s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual'])
                replace_closed(s,p,native_result=s.change(a['native_result'],**change));self.refuse(s,p)
        s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual']);native=s.value(a['native_result'])
        native['result']['remaining_unclaimed']=3;replace_closed(s,p,native_result=s.put(native));self.refuse(s,p,'consumption')

    def test_operator_pause_requires_exact_retained_marker(self):
        s,p,_,_=fixture();p['current_predecessor_pause']=None
        t=s.value(p['original_suffix_transition']);t['current_predecessor_pause']=None;p['original_suffix_transition']=s.put(t)
        self.refuse(s,p,'pause_unbound')

    def test_transport_refusal_is_not_automatically_resumable(self):
        s,p,_,_=fixture();replace_closed(s,p,stop_reason="IntegrityError('remote_transport_or_source_refusal')")
        self.refuse(s,p,'technical_ending_review_required')

    def test_arbitrary_failure_reason_never_becomes_capacity_hold(self):
        s,p,_,_=fixture();replace_closed(s,p,stop_reason="IntegrityError('source_changed')");self.refuse(s,p,'stop_reason')

    def test_exact_peer_flags_and_roster_required(self):
        s,p,_,_=fixture();p['execution_pins']=[];self.refuse(s,p,'pause_binding')
        s,p,_,_=fixture();p['predecessor_custody']['review']=s.change(p['predecessor_custody']['review'],accepted_completed_actor_saved_closure=1)
        self.refuse(s,p,'closed_peer')

    def test_tampered_bytes_duplicate_keys_and_read_bound_refused(self):
        s,p,_,_=fixture();ref=p['original_suffix_transition'];s.data[ref['path']]+=b' ';self.refuse(s,p,'pin_bytes')
        s,p,_,_=fixture();p['original_suffix_transition']=s.put(b'{"x":1,"x":2}')
        self.refuse(s,p,'duplicate_json_key')
        s,p,_,_=fixture();self.refuse(s,p,'read_bound',maximum_distinct_bytes=1)

    def test_all_remaining_complete_is_not_a_new_execution(self):
        s,p,_,_=fixture();a=s.value(p['predecessor_custody']['actual']);ids=[job(i)['measurement_id'] for i in range(5)]
        replace_closed(s,p,complete_ids=ids,unstarted_ids=[],terminal=5,occupied=5)
        self.refuse(s,p,'partition')


if __name__ == '__main__':
    unittest.main()
