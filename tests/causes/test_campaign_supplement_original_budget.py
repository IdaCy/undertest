"""RAM-only original-transfer histories; no source loader or native executor."""
import copy
import hashlib
import json
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_closed_scope as scope
from causes import campaign_supplement_completed_actor as actor
from causes import campaign_supplement_original_budget as budget
from causes import campaign_supplement_source_lineage as lineage


class Fixture:
    def __init__(self, *, count=6, prior_count=2, attempt_rate=4, capture_rate=1,
                 claims=1, attempts=3, captures=1, counter=0, jobs_change=None, first_change=None):
        self.data, self.reads, self.serial = {}, [], 0
        self.stop = "IntegrityError('local_free_space_floor')"
        self.count, self.prior_count = count, prior_count
        self.attempt_rate, self.capture_rate = attempt_rate, capture_rate
        self.full = []
        for i in range(prior_count + count):
            job = dict(family='original', model='model', run_id='run' + str(i), stage='early',
                       question_id='evaluation_strongly_favors', original_seed=i, seed=i)
            job['measurement_id'] = '/'.join(job[k] for k in
                ('family', 'model', 'run_id', 'stage', 'question_id'))
            self.full.append(job)
        if jobs_change:
            jobs_change(self.full)
        self.ids = [j['measurement_id'] for j in self.full]
        self.full_ref = self.put(self.full)
        self.old_sources = {k: self.put(('old ' + k).encode()) for k in
                            ('native_queue', 'completed_actor', 'driver')}
        self.first_sources = {k: self.put(('new ' + k).encode()) for k in
                              ('native_queue', 'completed_actor', 'driver')}
        self.next_sources = dict(self.first_sources, driver=self.put(b'suffix driver'),
                                 source_lineage=self.put(b'lineage helper'))
        self.binder = self.put(b'binder source')
        self.auditor = self.put(b'closed auditor source')
        self.provider, self.process = {'host': 'synthetic'}, {'pid': 3, 'start': 4}
        completed = self.put(dict(schema='causes.completed_actor_direct_probe_custody.v1',
            active_actor_roles=[], old_actor_owners_absent=True, no_unresolved_actors=True,
            completed_actor_records=[self.put({})], provider_identity=self.provider,
            provider_process=self.process))
        self.actors = dict(actual=completed, review=self.put(dict(
            accepted_completed_actor_direct_probe_custody=True, actual=completed, findings=[])))
        base_plan = self.put(dict(sources=self.old_sources))
        self.base_value = dict(schema='causes.finite_supplement_tranche_closed.v1',
            source=self.auditor, plan=base_plan, full_jobs=self.put(self.full[:prior_count]),
            complete_ids=self.ids[:prior_count], unstarted_ids=[], terminal=prior_count,
            occupied=prior_count, consecutive_failures=0, active=0, ambiguous=[],
            unresolved_claimed_ids=[], quarantined=False, clean_native_ending=True,
            qualification_findings=[{'inherited': 'retained'}], cumulative_invalid_ids=[self.ids[0]],
            all_original_attempts_retained=True, original_denominator_preserved=True,
            native_invocations=[dict(plan=base_plan)], original_bank_extension=None)
        self.base = self.closed_peer(self.base_value)
        parents = {'causes.campaign_supplement': self.old_sources['native_queue'],
                   'causes.campaign_supplement_completed_actor': self.old_sources['completed_actor']}
        selected = {'causes.campaign_supplement': self.first_sources['native_queue'],
                    'causes.campaign_supplement_completed_actor': self.first_sources['completed_actor']}
        derivation = self.put(dict(only_early_gate_changed=True, default_opt_in=False,
            proofs=[dict(module=k, source=selected[k], parent=parents[k]) for k in selected]))
        prep = self.put(dict(sources=selected, parent_sources=parents, source_derivation=derivation))
        peer = self.put(dict(accepted_healthy_harvest_continuation_source=True,
            accepted_completed_actor_healthy_harvest_source=True, sources=selected,
            parent_sources=parents, preparation=prep, source_derivation=derivation, findings=[]))
        adoption = self.put(dict(schema='causes.healthy_harvest_source_root_adoption.v1',
            accepted_source_for_prospective_migration=True, sources=selected, parent_sources=parents,
            source_preparation=prep, independent_review=peer, findings=[]))
        self.migration = self.put(dict(schema=lineage.MIGRATION, kind=lineage.KIND,
            predecessor_plan=base_plan, previous_source=self.old_sources['native_queue'],
            previous_runtime_completed_actor_source=self.old_sources['completed_actor'],
            accepted_public_dependency=self.old_sources['completed_actor'],
            source=self.first_sources['native_queue'], completed_actor_source=self.first_sources['completed_actor'],
            driver_source=self.first_sources['driver'], binder_source=self.binder,
            source_review=peer, root_source_adoption=adoption, source_preparation=prep,
            source_derivation=derivation))
        self.transfer = dict(actual=self.put({'synthetic_transfer': True}),
                             review=self.put({'synthetic_transfer_peer': True}))
        self.first = dict(schema='causes.completed_actor_driver_plan.v1',
            mode=actor.MODE, role='synthetic', family='original', model='model',
            endpoint='synthetic', constructor={'seed': 'preserved'}, parent_policy={'fixed': True},
            provider_identity=self.provider, completed_actor_custody=self.actors,
            retirement_pause_pins=[], local_free_space_floor_bytes=80 * 1024**3,
            retention={'reserve_bytes': 123}, max_clients=2, owner_identity={'pid': 7}, source_epoch='first',
            continuation_scope='transferred_original_reservation',
            predecessor_custody=self.base, occupied_ids=self.ids[:prior_count], initial_failures=0,
            maximum_new_requests=count, maximum_admission_attempts=count * attempt_rate,
            maximum_captures=count * capture_rate, maximum_capture_uses=3,
            jobs_sha256=core.digest(self.full[prior_count:]), output_root='/fixture/first-output',
            sources=self.first_sources, native_source_migration=self.migration,
            original_reservation_transfer=self.transfer, retain_admission_after_healthy_harvest=True)
        self.first['contract'] = self.contract(self.first)
        if first_change:
            first_change(self.first)
        self.first_ref = self.put(self.first)
        first_closed = self.hold(self.first_ref, claims=claims, attempts=attempts, captures=captures, counter=counter)
        first_closed['original_bank_extension'] = dict(schema=scope.EXTENSION_SCHEMA,
            previous_closed=self.base, previous_full_jobs=self.base_value['full_jobs'], transfer=self.transfer,
            source_family='original', model='model', prior_count=prior_count, appended_count=count,
            appended_jobs_sha256=self.first['jobs_sha256'], appended_ids_sha256=core.digest(self.ids[prior_count:]),
            cumulative_count=len(self.full), cumulative_jobs_sha256=core.digest(self.full), full_jobs=self.full_ref,
            prior_prefix_unchanged=True, native_families_preserved=True)
        self.origin = self.closed_peer(first_closed)
        self.head = self.origin
        self.used_attempts, self.used_captures = attempts, captures
        self.plan = self.next_plan()

    def put(self, value):
        self.serial += 1
        data = value if type(value) is bytes else core.encoded(value)
        path = '/fixture/budget-record-' + str(self.serial)
        self.data[path] = data
        return dict(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def read(self, ref):
        self.reads.append(ref['path'])
        return self.data[ref['path']]

    def value(self, ref):
        return json.loads(self.data[ref['path']])

    def closed_peer(self, value):
        actual = self.put(value)
        review = self.put(dict(accepted_finite_supplement_closed_custody=True,
            accepted_completed_actor_saved_closure=True, actual=actual, source=value['source'], findings=[],
            all_original_attempts_retained=True, original_denominator_preserved=True,
            qualification_findings=value['qualification_findings']))
        return dict(actual=actual, review=review)

    def contract(self, plan):
        return self.put(dict(schema=actor.CONTRACT_SCHEMA, active_actor_roles=[],
            completed_actor_custody=self.actors, predecessor_custody=plan['predecessor_custody'],
            max_clients=2, server_max_sequences=2, maximum_observation_age_seconds=29,
            claim_ready_age_seconds=5, provider_identity=self.provider, provider_process=self.process,
            baseline_preemptions=0))

    def hold(self, plan_ref, *, claims, attempts, captures, counter=0):
        plan = self.value(plan_ref); prior = self.value(plan['predecessor_custody']['actual'])
        begin, end = len(plan['occupied_ids']), len(plan['occupied_ids']) + claims
        result = self.put(dict(attempts=attempts, captures=captures,
            result=dict(consecutive_failures=counter, stop_reason=self.stop, active=0,
                        new_final=claims, remaining_unclaimed=len(self.full) - end,
                        completed_ids=list(reversed(self.ids[begin:end])))))
        entry = dict(plan=plan_ref, source=plan['sources']['driver'], result=result,
            process_absent=True, family=plan['family'], model=plan['model'],
            jobs_sha256=plan['jobs_sha256'], original_reservation_transfer=plan.get('original_reservation_transfer'))
        return dict(schema='causes.finite_supplement_tranche_closed.v1', role=plan['role'], mode=actor.MODE,
            source=self.auditor, plan=plan_ref, native_result=result, output=plan['output_root'],
            prior_closed=plan['predecessor_custody']['actual'], prior_closed_review=plan['predecessor_custody']['review'],
            full_jobs=self.full_ref, complete_ids=self.ids[:end], unstarted_ids=self.ids[end:],
            terminal=end, occupied=end, clean_native_ending=True, active=0, ambiguous=[],
            unresolved_claimed_ids=[], quarantined=False, current_tranche_qualified=True, new_invalid_ids=[],
            consecutive_failures=counter, stop_reason=self.stop,
            inherited_qualification_findings=prior['qualification_findings'],
            qualification_findings=prior['qualification_findings'], current_qualification_findings=[],
            prior_invalid_ids=prior['cumulative_invalid_ids'], cumulative_invalid_ids=prior['cumulative_invalid_ids'],
            all_original_attempts_retained=True, original_denominator_preserved=True,
            native_invocations=prior['native_invocations'] + [entry], original_bank_extension=None)

    def next_plan(self):
        head = self.value(self.head['actual']); prior_plan = self.value(head['plan'])
        p = copy.deepcopy(self.first); p.pop('original_reservation_transfer')
        count = len(head['unstarted_ids'])
        p.update(continuation_scope='full_untouched_fixed_suffix', predecessor_custody=self.head,
            occupied_ids=head['complete_ids'], initial_failures=head['consecutive_failures'],
            maximum_new_requests=count, maximum_admission_attempts=min(count * self.attempt_rate,
                self.first['maximum_admission_attempts'] - self.used_attempts),
            maximum_captures=min(count * self.capture_rate, self.first['maximum_captures'] - self.used_captures),
            jobs_sha256=core.digest(self.full[len(head['complete_ids']):]), sources=self.next_sources,
            output_root='/fixture/next-output-' + str(self.serial), source_epoch='next')
        p['contract'] = self.contract(p)
        p['native_source_migration'] = self.put(dict(schema=lineage.CONTINUATION, kind=lineage.KIND,
            predecessor_plan=head['plan'], previous_migration=prior_plan['native_source_migration'],
            original_migration=self.migration, previous_source=prior_plan['sources']['native_queue'],
            source=p['sources']['native_queue'],
            previous_runtime_completed_actor_source=prior_plan['sources']['completed_actor'],
            completed_actor_source=p['sources']['completed_actor'],
            driver_source=p['sources']['driver'], binder_source=self.binder))
        return p

    def append(self, *, claims=0, attempts=2, captures=1, counter=0):
        value = self.hold(self.put(self.plan), claims=claims, attempts=attempts, captures=captures, counter=counter)
        self.head = self.closed_peer(value)
        self.used_attempts += attempts; self.used_captures += captures
        self.plan = self.next_plan()

    def change_head(self, **changes):
        head = self.value(self.head['actual']); head.update(changes)
        old = self.head; self.head = self.closed_peer(head)
        if self.origin == old:
            self.origin = self.head
        self.plan = self.next_plan()

    def run(self, **limits):
        return budget.resolve(self.plan, origin=self.origin, read=self.read, **limits)


class OriginalBudgetTests(unittest.TestCase):
    def refuse(self, f, reason=None, **limits):
        with self.assertRaises((core.IntegrityError, lineage.SourceLineageError)) as caught:
            f.run(**limits)
        if reason:
            self.assertIn(reason, str(caught.exception))

    def test_single_hold_and_existing_validators_without_mutation(self):
        f = Fixture(); before = copy.deepcopy((f.data, f.plan, f.origin))
        result = f.run()
        self.assertEqual((result['used_attempts'], result['used_captures']), (3, 1))
        self.assertEqual((result['remaining_attempts'], result['remaining_captures']), (20, 5))
        self.assertEqual(result['jobs'], f.full[3:])
        self.assertEqual(result['links'], [f.origin])
        self.assertEqual(before, (f.data, f.plan, f.origin))
        self.assertEqual(len(f.reads), len(set(f.reads)))
        self.assertEqual(result['read_bytes'], sum(p['bytes'] for p in result['required_pins']))

    def test_repeated_zero_claim_holds_consume_attempts_and_captures(self):
        f = Fixture(claims=0, attempts=5, captures=2)
        f.append(attempts=4, captures=1); f.append(attempts=3, captures=1)
        result = f.run()
        self.assertEqual((result['used_attempts'], result['used_captures']), (12, 4))
        self.assertEqual((result['remaining_attempts'], result['remaining_captures']), (12, 2))
        self.assertEqual(len(result['jobs']), 6)
        self.assertEqual(len(result['links']), 3)

    def test_different_sizes_ratios_and_capture_uses(self):
        for count, prior, a, c in [(3, 1, 2, 1), (7, 3, 5, 2)]:
            with self.subTest(count=count):
                f = Fixture(count=count, prior_count=prior, attempt_rate=a, capture_rate=c,
                            claims=1, attempts=2, captures=1)
                r = f.run()
                self.assertEqual(r['remaining_attempts'], min(a * (count - 1), a * count - 2))
                self.assertEqual(r['remaining_captures'], min(c * (count - 1), c * count - 1))
                self.assertEqual(r['maximum_capture_uses'], 3)

    def test_nonzero_failure_counters_preserved(self):
        for counter in (1, 2):
            f = Fixture(counter=counter); f.append(counter=counter)
            self.assertEqual(f.run()['jobs'], f.full[3:])
            f.plan['initial_failures'] = 0
            self.refuse(f, 'failure_carry')

    def test_inherited_invalid_is_retained_but_new_invalid_refused(self):
        f = Fixture(); self.assertTrue(f.run()['jobs'])
        f.change_head(new_invalid_ids=['new-invalid']); self.refuse(f, 'hold_history')

    def test_attempt_or_capture_exhaustion_refuses_even_with_all_jobs_untouched(self):
        for attempts, captures in [(24, 1), (6, 6)]:
            f = Fixture(claims=0, attempts=attempts, captures=captures)
            self.refuse(f, 'remaining_ceiling')

    def test_budget_reset_and_changed_capture_uses_refuse(self):
        for key in ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'):
            f = Fixture(claims=0, attempts=5, captures=2); f.append()
            f.plan[key] += 1; self.refuse(f, 'remaining_ceiling')

    def test_fractional_or_boolean_origin_policy_is_not_invented(self):
        for value in (25, True, 0):
            f = Fixture(first_change=lambda p: p.update(maximum_admission_attempts=value))
            self.refuse(f, 'integral_origin_policy')

    def test_duplicate_identity_and_original_physical_alias_refuse(self):
        f = Fixture(jobs_change=lambda jobs: jobs.__setitem__(-1, copy.deepcopy(jobs[0])))
        self.refuse(f, 'duplicate_identity')
        def alias(jobs):
            jobs[-1].update(family='another-original-family', run_id=jobs[0]['run_id'])
            jobs[-1]['measurement_id'] = '/'.join(jobs[-1][k] for k in
                ('family', 'model', 'run_id', 'stage', 'question_id'))
        f = Fixture(jobs_change=alias); self.refuse(f, 'physical_alias')

    def test_original_namespace_and_origin_extension_digest_refuse(self):
        f = Fixture(jobs_change=lambda jobs: jobs[-1].update(measurement_id='derived-alias'))
        self.refuse(f, 'original_identity')
        f = Fixture(); extension = f.value(f.head['actual'])['original_bank_extension']
        extension['appended_jobs_sha256'] = '0' * 64
        f.change_head(original_bank_extension=extension); self.refuse(f, 'origin_extension')

    def test_changed_original_prefix_is_type_sensitive(self):
        f = Fixture(prior_count=1)
        base = f.value(f.base['actual']); old = f.value(base['full_jobs']); old[0]['seed'] = False
        base['full_jobs'] = f.put(old); new_base = f.closed_peer(base)
        first = copy.deepcopy(f.first); first['predecessor_custody'] = new_base
        new_first = f.put(first); head = f.value(f.head['actual'])
        head.update(plan=new_first, prior_closed=new_base['actual'], prior_closed_review=new_base['review'])
        head['native_invocations'][-1]['plan'] = new_first
        f.first = first; f.change_head(**head)
        self.refuse(f, 'origin_bank')

    def test_later_hold_cannot_be_substituted_as_origin(self):
        f = Fixture(); f.append(); f.origin = f.head
        self.refuse(f, 'first_transfer_required')

    def test_omitted_native_history_and_swapped_native_result_refuse(self):
        for kind in ('history', 'result'):
            f = Fixture(); f.append(); head = f.value(f.head['actual'])
            if kind == 'history':
                f.change_head(native_invocations=head['native_invocations'][1:])
                self.refuse(f, 'invocation_prefix')
            else:
                f.change_head(native_result=f.value(f.origin['actual'])['native_result'])
                self.refuse(f, 'invocation')

    def test_reordered_native_history_refuses(self):
        f = Fixture(); f.append(); head = f.value(f.head['actual'])
        f.change_head(native_invocations=list(reversed(head['native_invocations'])))
        self.refuse(f, 'invocation_prefix')

    def test_omitted_closure_refuses(self):
        f = Fixture(); f.append(); f.append()
        origin = f.value(f.origin['actual'])
        f.change_head(prior_closed=origin['prior_closed'], prior_closed_review=origin['prior_closed_review'])
        self.refuse(f, 'predecessor')

    def test_derived_or_second_extension_refuses(self):
        f = Fixture(); f.plan['derived_bank'] = {'forged': True}
        self.refuse(f, 'same_bank_scope')
        f = Fixture(); f.append(); f.change_head(original_bank_extension=f.value(f.origin['actual'])['original_bank_extension'])
        self.refuse(f, 'second_extension')
        f = Fixture(); f.change_head(derived_bank_extension=None)
        self.refuse(f, 'unknown_extension')

    def test_hold_eligibility_and_counter_types(self):
        for key, value in [('quarantined', True), ('active', 1), ('ambiguous', ['x']),
                           ('unresolved_claimed_ids', ['x']), ('consecutive_failures', True),
                           ('consecutive_failures', 3), ('stop_reason', 'operator_pause')]:
            with self.subTest(key=key, value=value):
                f = Fixture(); f.change_head(**{key: value}); self.refuse(f)

    def test_claimed_prefix_replay_or_shortened_selection_refuses(self):
        f = Fixture(); f.plan['occupied_ids'] = f.ids[:2]; self.refuse(f)
        f = Fixture(); f.plan['maximum_new_requests'] -= 1; self.refuse(f)

    def test_source_or_fixed_policy_change_refuses(self):
        f = Fixture(); f.plan['sources']['native_queue'] = f.put(b'other queue'); self.refuse(f, 'dependency_sources')
        f = Fixture(); f.plan['constructor'] = {'seed': 'changed'}; self.refuse(f, 'policy_changed')
        f = Fixture(); f.append(); f.plan['sources'] = dict(f.plan['sources'], driver=f.put(b'new driver'))
        self.refuse(f, 'suffix_sources')

    def test_source_lineage_is_not_stubbed_or_bypassed(self):
        f = Fixture(); record = f.value(f.plan['native_source_migration'])
        record['previous_migration'] = f.put({'schema': 'forged'})
        f.plan['native_source_migration'] = f.put(record)
        self.refuse(f, 'previous_record')

    def test_contract_custody_and_counter_checks_are_not_bypassed(self):
        f = Fixture(); contract = f.value(f.plan['contract']); contract['active_actor_roles'] = ['active']
        f.plan['contract'] = f.put(contract); self.refuse(f, 'custody_required')
        f = Fixture(); f.plan['initial_failures'] = True; self.refuse(f, 'failure_carry')

    def test_peer_actual_source_and_findings_joins(self):
        for field, change in [('actual', 'other'), ('source', 'other'), ('findings', ['finding'])]:
            f = Fixture(); peer = f.value(f.origin['review']); peer[field] = change
            f.origin = dict(f.origin, review=f.put(peer)); self.refuse(f, 'closed_peer')

    def test_earlier_suffix_ceiling_reset_is_checked_too(self):
        f = Fixture(claims=0, attempts=8, captures=1)
        f.plan['maximum_admission_attempts'] += 1
        f.append(attempts=2, captures=1)
        self.refuse(f, 'prior_ceiling')

    def test_inherited_finding_and_native_prefix_types_remain_exact(self):
        f = Fixture(); f.change_head(inherited_qualification_findings=[])
        self.refuse(f, 'hold_history')
        f = Fixture(); f.append(); head = f.value(f.head['actual'])
        head['native_invocations'][-2]['process_absent'] = 1
        f.change_head(**head); self.refuse(f, 'invocation_prefix')

    def test_native_attempt_capture_and_result_counter_checks(self):
        for key, val in [('attempts', True), ('attempts', -1), ('attempts', 25), ('captures', 0), ('captures', 4)]:
            f = Fixture(); h = f.value(f.head['actual']); result = f.value(h['native_result']); result[key] = val
            ref = f.put(result); entry = copy.deepcopy(h['native_invocations']); entry[-1]['result'] = ref
            f.change_head(native_result=ref, native_invocations=entry); self.refuse(f, 'native_consumption')

    def test_depth_limit_and_bound_types_before_reads(self):
        f = Fixture(); f.append(); self.refuse(f, 'cycle_or_depth', max_links=1)
        for limits in [{'max_links': True}, {'maximum_total_bytes': 0}, {'maximum_metadata_bytes': True}]:
            f = Fixture(); self.refuse(f, **limits); self.assertEqual(f.reads, [])

    def test_declared_per_file_and_total_bounds_before_callback(self):
        f = Fixture(); ref = dict(f.origin['actual'], bytes=8 * 1024**2 + 1)
        f.origin = dict(f.origin, actual=ref); self.refuse(f, 'file_bound'); self.assertEqual(f.reads, [])
        f = Fixture(); self.refuse(f, 'total_bound', maximum_metadata_bytes=20000,
                                 maximum_bank_bytes=20000, maximum_total_bytes=20000)

    def test_wrong_bytes_and_conflicting_path_pins(self):
        f = Fixture(); f.data[f.origin['actual']['path']] += b' '
        self.refuse(f, 'pin_bytes')
        f = Fixture(); f.origin = dict(f.origin, review=dict(f.origin['actual'], sha256='0' * 64))
        self.refuse(f, 'conflicting_pin')

    def test_duplicate_and_nonfinite_json_including_exponent(self):
        for raw in [b'{"x":0,"x":1}', b'{"x":NaN}', b'{"x":1e999}']:
            f = Fixture(); f.origin = dict(f.origin, actual=f.put(raw)); self.refuse(f)


if __name__ == '__main__':
    unittest.main()
