"""Ready source transitions over a real, RAM-only mixed-owner history."""
import copy
import types
import unittest
from unittest import mock

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from causes import campaign_supplement_mixed_owner as mixed
from causes import campaign_supplement_source_lineage as lineage
from test_campaign_derived_ready_lineage import Case as OriginalReadyCase
from test_campaign_supplement_mixed_owner import fixture as mixed_fixture


class ReadyAfterCase:
    def __init__(self, *, resumed=False, partial=False, change=lambda stage, value: None):
        self.original = OriginalReadyCase()
        self.m = self.original.m
        old, descriptor, _, _ = mixed_fixture()
        self.m.data.update(old.files)
        old_proof = self.m.value(descriptor['actual'])
        old_base = self.m.value(old_proof['original_base']['actual'])
        old_first = self.m.value(old_proof['links'][0]['actual'])
        old_plan = self.m.value(old_first['plan'])
        self.original.actual.update(old_base)
        self.original.actual.update(consecutive_failures=1,
            qualification_findings=['historical failure retained'], cumulative_invalid_ids=[])
        change('original', self.original.actual)
        self.original.refresh()
        prior_ref = self.original.predecessor
        prior = self.m.value(prior_ref['actual'])
        bank = self.m.value(old_plan['derived_bank']['actual'])
        bank['execution_predecessor'] = prior_ref
        self.bank = self.accept(bank, 'accepted_derived_supplement_bank')
        full = self.m.value(old_first['full_jobs'])
        derived = full[len(prior['complete_ids']):]
        extension = copy.deepcopy(old_first['derived_bank_extension'])
        extension.update(adoption=self.bank, execution_predecessor=prior_ref,
            previous_closed=prior_ref, previous_full_jobs=prior['full_jobs'])
        self.links = []
        self.plans = []
        for index in range(2 if resumed else 1):
            first = index == 0
            selected = full[len(prior['complete_ids']):]
            plan = copy.deepcopy(old_plan)
            plan.update(sources=copy.deepcopy(self.original.sources),
                predecessor_custody=prior_ref, occupied_ids=prior['complete_ids'],
                initial_failures=prior['consecutive_failures'], derived_bank=self.bank,
                continuation_scope='adopted_derived_bank' if first else 'derived_bank_remaining',
                native_source_migration=self.original.reference,
                retain_admission_after_healthy_harvest=True,
                retain_admission_after_ready_harvest=True,
                maximum_new_requests=len(selected), jobs_sha256=mixed.digest(selected),
                output_root='/synthetic/output/ready-derived-' + str(index))
            if not first:
                plan['sources']['driver'] = self.m.put(b'synthetic accepted suffix driver')
            change('first' if first else 'suffix', plan)
            plan_ref = self.m.put(plan)
            self.plans.append(plan_ref)
            done = len(full) - (1 if (first and resumed) or partial else 0)
            newly_done = [j['measurement_id'] for j in full[len(prior['complete_ids']):done]]
            result = self.m.put(dict(attempts=12 if first else 4, captures=3 if first else 1,
                result=dict(active=0, consecutive_failures=0,
                    new_final=len(newly_done), completed_ids=newly_done)))
            invocation = dict(plan=plan_ref, source=plan['sources']['driver'], result=result,
                process_absent=True, pid=42 + index, family=plan['family'], model=plan['model'],
                jobs_sha256=plan['jobs_sha256'], derived_bank=self.bank)
            saved = copy.deepcopy(old_first)
            saved.update(source=plan['sources']['driver'], plan=plan_ref, output=plan['output_root'],
                prior_closed=prior_ref['actual'], prior_closed_review=prior_ref['review'],
                complete_ids=[j['measurement_id'] for j in full[:done]],
                unstarted_ids=[j['measurement_id'] for j in full[done:]],
                terminal=done, occupied=done, consecutive_failures=0,
                inherited_qualification_findings=prior['qualification_findings'],
                current_qualification_findings=[], qualification_findings=prior['qualification_findings'],
                prior_invalid_ids=prior['cumulative_invalid_ids'], new_invalid_ids=[],
                cumulative_invalid_ids=prior['cumulative_invalid_ids'],
                native_invocations=prior['native_invocations'] + [invocation], native_result=result,
                derived_bank_extension=extension)
            change('first_closed' if first else 'suffix_closed', saved)
            prior_ref = self.close(saved)
            prior = saved
            self.links.append(prior_ref)
        self.head = prior_ref
        self.sources = dict(native_queue=self.original.sources['native_queue'],
            completed_actor=self.m.put(b'synthetic mixed ready actor'),
            mixed_owner=self.m.put(b'synthetic accepted mixed helper'),
            source_lineage=self.m.put(b'synthetic successor lineage'),
            driver=self.m.put(b'synthetic successor driver'),
            ready_migration=self.original.sources['ready_migration'])
        self.binder = self.m.put(b'synthetic successor binder')
        proof = dict(schema=mixed.SCHEMA, head=self.head,
            original_base=self.original.predecessor, links=self.links, model='mi27',
            counts=dict(owner=len(full), original=len(full)-len(derived), derived=len(derived)),
            owner_ids_sha256=mixed.digest([j['measurement_id'] for j in full]),
            original_ids_sha256=mixed.digest([j['measurement_id'] for j in full[:-len(derived)]]),
            derived_ids_sha256=mixed.digest([j['measurement_id'] for j in derived]))
        change('partition', proof)
        self.partition = self.accept(proof, 'accepted_mixed_supplement_owner_partition',
            source=self.sources['mixed_owner'], head=self.head)
        selected = dict(self.sources, binder=self.binder)
        peer = dict(accepted_original_after_derived_source=True,
            accepted_original_after_derived_ready_source=True, findings=[], sources=selected,
            previous_completed_actor_source=plan['sources']['completed_actor'])
        change('peer', peer)
        peer_ref = self.m.put(peer)
        adoption = dict(schema='causes.original_after_derived_ready_source_adoption.v1',
            accepted_original_after_derived_ready_source=True,
            accepted_source_for_prospective_transition=True, root_authorized=True,
            runtime_authorized=False, sources=selected, independent_review=peer_ref, findings=[])
        change('adoption', adoption)
        self.transition = dict(schema=lineage.AFTER_DERIVED_READY,
            kind='retain_admission_after_ready_harvest', owner_partition=self.partition,
            predecessor_plan=plan_ref, previous_lineage=plan['native_source_migration'],
            previous_source=plan['sources']['native_queue'],
            previous_runtime_completed_actor_source=plan['sources']['completed_actor'],
            source=self.sources['native_queue'], completed_actor_source=self.sources['completed_actor'],
            mixed_owner_source=self.sources['mixed_owner'], lineage_source=self.sources['source_lineage'],
            driver_source=self.sources['driver'], binder_source=self.binder,
            migration_validator_source=self.sources['ready_migration'],
            source_review=peer_ref, root_source_adoption=self.m.put(adoption))
        change('transition', self.transition)
        self.reference = self.m.put(self.transition)

    def accept(self, value, flag, **extra):
        ref = self.m.put(value)
        return dict(actual=ref, review=self.m.put({flag: True, 'actual': ref, 'findings': [], **extra}))

    def close(self, saved):
        return self.accept(saved, 'accepted_finite_supplement_closed_custody',
            accepted_completed_actor_saved_closure=True, source=saved['source'],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            qualification_findings=saved['qualification_findings'])

    def run(self, **changes):
        arguments = dict(reference=self.reference, predecessor=self.head, sources=self.sources,
            read=self.m.read, mixed_owner=mixed, validate_migration=self.original.validator)
        arguments.update(changes)
        return lineage.validate_after_derived_ready(**arguments)


class ReadyAfterDerivedTests(unittest.TestCase):
    def test_complete_real_partition_keeps_historical_first_sources(self):
        c = ReadyAfterCase()
        before = copy.deepcopy(c.m.data)
        result = c.run()
        self.assertEqual(result['kind'], 'original_after_derived_ready')
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(result['original_migration'], c.original.migration)
        self.assertEqual(c.original.calls, [c.original.prior])
        self.assertNotEqual(c.sources['completed_actor'], c.original.sources['completed_actor'])
        self.assertEqual(c.m.data, before)

    def test_same_adoption_suffix_and_failed_history_are_preserved(self):
        c = ReadyAfterCase(resumed=True)
        result = c.run()
        self.assertEqual(result['derived_invocations'], 2)
        self.assertEqual(result['first_derived_plan'], c.plans[0])
        for plan_ref, custody in zip(c.plans, c.links):
            saved = c.m.value(custody['actual'])
            self.assertEqual(saved['qualification_findings'], ['historical failure retained'])
            self.assertIn(saved['native_result'], result['required_pins'])
            self.assertIn(c.m.value(plan_ref)['sources']['driver'], result['required_pins'])
        self.assertEqual(c.m.value(c.plans[0])['initial_failures'], 1)
        self.assertEqual(c.m.value(c.plans[1])['initial_failures'], 0)

    def test_nonterminal_derived_head_refuses_before_migration_validation(self):
        c = ReadyAfterCase(partial=True)
        with self.assertRaisesRegex(lineage.SourceLineageError, 'incomplete_execution'):
            c.run()
        self.assertEqual(c.original.calls, [])

    def test_each_historical_option_requires_literal_true(self):
        for stage in ('first', 'suffix'):
            for key in ('retain_admission_after_healthy_harvest', 'retain_admission_after_ready_harvest'):
                for value in (None, False, 1):
                    def change(where, data):
                        if where == stage:
                            if value is None:
                                data.pop(key)
                            else:
                                data[key] = value
                    with self.subTest(stage=stage, key=key, value=value):
                        c = ReadyAfterCase(resumed=True, change=change)
                        with self.assertRaises(lineage.SourceLineageError):
                            c.run()

    def test_changed_suffix_queue_actor_or_validator_refuses(self):
        for key in ('native_queue', 'completed_actor', 'ready_migration'):
            def change(stage, data):
                if stage == 'suffix':
                    data['sources'][key] = data['sources']['driver']
            with self.subTest(key=key), self.assertRaises(lineage.SourceLineageError):
                ReadyAfterCase(resumed=True, change=change).run()

    def test_source_adoption_and_previous_actor_are_exact(self):
        changes = [('peer', 'accepted_original_after_derived_ready_source', False),
            ('peer', 'findings', ['unresolved']), ('peer', 'previous_completed_actor_source', {}),
            ('adoption', 'root_authorized', False), ('adoption', 'runtime_authorized', True)]
        for stage, field, value in changes:
            def change(where, data):
                if where == stage:
                    data[field] = value
            with self.subTest(stage=stage, field=field), self.assertRaisesRegex(
                    lineage.SourceLineageError, 'source_acceptance'):
                ReadyAfterCase(change=change).run()

    def test_changed_source_bytes_are_rejected(self):
        for key in ('driver', 'completed_actor', 'ready_migration'):
            c = ReadyAfterCase()
            c.m.data[c.sources[key]['path']] = b'changed bytes'
            with self.subTest(key=key), self.assertRaises(lineage.SourceLineageError):
                c.run()

    def test_immediate_head_and_partition_cannot_be_substituted(self):
        for field in ('predecessor_plan', 'owner_partition', 'previous_lineage'):
            c = ReadyAfterCase(resumed=True)
            c.transition[field] = c.original.migration
            c.reference = c.m.put(c.transition)
            with self.subTest(field=field), self.assertRaises(lineage.SourceLineageError):
                c.run()

    def test_failed_counter_and_qualification_history_cannot_reset(self):
        for field, value in [('consecutive_failures', 2), ('inherited_qualification_findings', [])]:
            def change(stage, data):
                if stage == 'suffix_closed':
                    data[field] = value
            with self.subTest(field=field), self.assertRaises(lineage.SourceLineageError):
                ReadyAfterCase(resumed=True, change=change).run()

    def test_missing_or_non_callable_migration_validator_refuses(self):
        with self.assertRaisesRegex(lineage.SourceLineageError, 'ready_validator'):
            ReadyAfterCase().run(validate_migration=None)

    def test_missing_or_substituted_current_validator_source_refuses(self):
        for missing in (True, False):
            c = ReadyAfterCase()
            if missing:
                c.sources.pop('ready_migration')
            else:
                c.sources['ready_migration'] = c.sources['driver']
            with self.subTest(missing=missing), self.assertRaises(lineage.SourceLineageError):
                c.run()
            self.assertEqual(c.original.calls, [])

    def test_healthy_or_extra_transition_fields_are_not_guessed(self):
        for key, value in [('schema', lineage.AFTER_DERIVED), ('unexpected', True)]:
            c = ReadyAfterCase()
            c.transition[key] = value
            c.reference = c.m.put(c.transition)
            with self.subTest(key=key), self.assertRaisesRegex(lineage.SourceLineageError, 'ready_transition'):
                c.run()

    def test_conflicting_returned_required_pin_refuses(self):
        c = ReadyAfterCase()
        original_resolve = mixed.resolve
        def resolve(*args, **kwargs):
            result = original_resolve(*args, **kwargs)
            result['required_pins'].append(dict(c.sources['driver'], sha256='0' * 64))
            return result
        with mock.patch.object(mixed, 'resolve', side_effect=resolve):
            with self.assertRaisesRegex(lineage.SourceLineageError, 'conflicting_pin'):
                c.run()


class MixedActorReadyWrapperTests(unittest.TestCase):
    def admission(self, healthy=True, ready=True):
        options = dict(retain_admission_after_healthy_harvest=healthy,
                       retain_admission_after_ready_harvest=ready)
        plan = dict(options, output_root='/synthetic/output', family='synthetic',
            model='synthetic', capacity_limit={'synthetic': True}, pause_paths=[],
            local_free_space_floor_bytes=80 * 1024**3)
        return types.SimpleNamespace(plan=plan, contract=dict(options),
            read=mock.Mock(), source_check=mock.Mock(), check=mock.Mock(), prepare=mock.Mock())

    def run_wrapper(self, admission, **options):
        with mock.patch.object(direct, 'validate_contract') as validate, \
             mock.patch.object(core, 'run_queue', return_value={'synthetic': True}) as queue:
            result = direct.run_queue('/synthetic/output', [], mock.Mock(), admission, [],
                family='synthetic', model='synthetic', initial_failures=1,
                max_clients=8, capacity_limit={'synthetic': True},
                local_free_space_floor_bytes=80 * 1024**3, **options)
        return result, validate, queue

    def test_both_options_forward_with_existing_floor_and_failures(self):
        _, validate, queue = self.run_wrapper(self.admission(),
            retain_admission_after_healthy_harvest=True, retain_admission_after_ready_harvest=True)
        self.assertTrue(validate.called)
        kwargs = queue.call_args.kwargs
        self.assertTrue(kwargs['retain_admission_after_healthy_harvest'])
        self.assertTrue(kwargs['retain_admission_after_ready_harvest'])
        self.assertEqual(kwargs['initial_failures'], 1)
        self.assertEqual(kwargs['local_free_space_floor_bytes'], 80 * 1024**3)

    def test_disabled_ready_option_preserves_old_call_shape(self):
        _, _, queue = self.run_wrapper(self.admission(ready=False),
            retain_admission_after_healthy_harvest=True)
        self.assertNotIn('retain_admission_after_ready_harvest', queue.call_args.kwargs)

    def test_missing_options_preserve_legacy_defaults(self):
        a = self.admission()
        a.plan.pop('retain_admission_after_healthy_harvest')
        a.plan.pop('retain_admission_after_ready_harvest')
        a.contract.clear()
        _, _, queue = self.run_wrapper(a)
        self.assertNotIn('retain_admission_after_ready_harvest', queue.call_args.kwargs)
        self.assertNotIn('retain_admission_after_healthy_harvest', queue.call_args.kwargs)

    def test_mismatched_or_non_bool_options_refuse_before_native_queue(self):
        for target in ('plan', 'contract'):
            for key in ('retain_admission_after_healthy_harvest', 'retain_admission_after_ready_harvest'):
                for value in (None, False, 1):
                    a = self.admission()
                    if value is None:
                        getattr(a, target).pop(key)
                    else:
                        getattr(a, target)[key] = value
                    with self.subTest(target=target, key=key, value=value), \
                         mock.patch.object(core, 'run_queue') as queue:
                        with self.assertRaises(core.IntegrityError):
                            direct.run_queue('/synthetic/output', [], mock.Mock(), a, [],
                                family='synthetic', model='synthetic', initial_failures=1,
                                max_clients=8, capacity_limit=None,
                                retain_admission_after_healthy_harvest=True,
                                retain_admission_after_ready_harvest=True)
                        queue.assert_not_called()

    def test_ready_requires_healthy_and_literal_bool_argument(self):
        for healthy, ready in [(False, True), (True, 1)]:
            with self.subTest(healthy=healthy, ready=ready), self.assertRaises(core.IntegrityError):
                self.run_wrapper(self.admission(healthy=healthy, ready=ready),
                    retain_admission_after_healthy_harvest=healthy,
                    retain_admission_after_ready_harvest=ready)


if __name__ == '__main__':
    unittest.main()
