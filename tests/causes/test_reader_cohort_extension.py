import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest

from causes import campaign_reader_cohort_extension as e
from causes import campaign_reader_closed_audit as audit


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0
        self.old_ready = [dict(run_id='old', measurement_key=e.measurement_key('old'),
                              stage='early', disposition='prepared', payloads={'unchanged': True})]
        self.old_missing = [dict(run_id='unavailable', measurement_key=e.measurement_key('unavailable'),
                                stage='early', disposition='unavailable', reason='missing_context')]
        self.protocol = dict(reader_source=self.put('reader'), checker_config=self.put({}),
                             prompts={}, prompts_directory=str(self.root))
        self.base = dict(schema='causes.fresh_candidate_reader_inputs.v1', source=self.put('binder'),
                         cohort=self.put('old-cohort'), ready=self.put(self.old_ready),
                         unavailable=self.put(self.old_missing), pending=self.put([]),
                         self_jobs=self.put([]), counts={'selected': 2}, **self.protocol)
        base_ref = self.put(self.base)
        base_review = self.put(dict(accepted_fresh_candidate_reader_inputs=True,
            preparation=base_ref, findings=[], **{k: self.base[k] for k in
            ('source', 'cohort', 'ready', 'unavailable', 'pending', 'self_jobs', 'reader_source')}))
        self.jobs = [dict(allocation=dict(run_id=run, continue_to_end=False, instruments=e.SELF,
                        reader_stages=['early'], reader_thresholds=e.READERS), spec={}, variant={})
                     for run in ('new-a', 'new-b')]
        self.selection = dict(global_jobs=self.put(self.jobs), contexts=2, reader_instruments=e.READERS,
            rows=[dict(run_id=j['allocation']['run_id'], job_sha256=self.sha(j)) for j in self.jobs])
        selection_ref = self.put(self.selection)
        cohort = dict(schema='causes.extended_reader_cohort.v1', base_preparation=base_ref,
                      base_cohort=self.base['cohort'], selection=selection_ref,
                      new_jobs=self.selection['global_jobs'], new_run_ids=['new-a', 'new-b'], count=4)
        self.binding = dict(base_preparation=base_ref, base_review=base_review, selection=selection_ref,
            selection_review=self.put(dict(accepted_requested_actor_reader_selection_preparation=True,
                selection=selection_ref, global_jobs=self.selection['global_jobs'], findings=[])),
            cohort=self.put(cohort), known=self.put('new-known'), row_materializer=self.base['source'])
        self.new_row = dict(run_id='new-a', measurement_key=e.measurement_key('new-a'), stage='early',
                           disposition='prepared', native_job_sha256=self.sha(self.jobs[0]))
        self.new_pending = dict(run_id='new-b', measurement_key=e.measurement_key('new-b'),
                                stage='early', disposition='pending')
        self.prep = dict(schema='causes.fresh_candidate_reader_inputs.v1', source=e.pin(e.__file__),
            known=self.binding['known'], cohort=self.binding['cohort'],
            row_materializer=self.binding['row_materializer'], ready=self.put(self.old_ready + [self.new_row]),
            unavailable=self.base['unavailable'], pending=self.put([self.new_pending]), self_jobs=self.put([]),
            counts=dict(selected=4, copied=3, reader_ready=2, reader_requests=6, unavailable=1,
                        pending=1, self_requests=0), **self.protocol)

    def sha(self, value):
        return hashlib.sha256(e.encoded(value)).hexdigest()

    def put(self, value):
        self.serial += 1
        return e.save(self.root / f'{self.serial}.json', value)

    def check(self, prep=None, previous=None):
        return e.validate_cumulative(self.binding, prep or self.prep, previous)

    def test_extension_preserves_ready_missing_and_pending(self):
        ready, unavailable, pending = self.check(previous=self.base)
        self.assertEqual(ready, self.old_ready + [self.new_row])
        self.assertEqual(unavailable, self.old_missing)
        self.assertEqual(pending, [self.new_pending])

    def test_removed_old_ready_refused(self):
        p = dict(self.prep, ready=self.put([self.new_row]))
        with self.assertRaisesRegex(ValueError, 'base_rows_removed'):
            self.check(p)

    def test_relabelled_old_missing_refused(self):
        changed = copy.deepcopy(self.old_missing); changed[0]['reason'] = 'now_available'
        with self.assertRaisesRegex(ValueError, 'base_rows_removed'):
            self.check(dict(self.prep, unavailable=self.put(changed)))

    def test_json_distinct_old_row_refused(self):
        changed = copy.deepcopy(self.old_ready); changed[0]['payloads']['unchanged'] = 1
        with self.assertRaisesRegex(ValueError, 'base_rows_removed'):
            self.check(dict(self.prep, ready=self.put(changed + [self.new_row])))

    def test_duplicate_new_run_refused(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_cumulative_run'):
            self.check(dict(self.prep, ready=self.put(self.old_ready + [self.new_row, self.new_row])))

    def test_duplicate_measurement_key_refused(self):
        changed = dict(self.new_pending, measurement_key=self.new_row['measurement_key'])
        with self.assertRaisesRegex(ValueError, 'duplicate_measurement_key'):
            self.check(dict(self.prep, pending=self.put([changed])))

    def test_missing_new_assignment_refused(self):
        with self.assertRaisesRegex(ValueError, 'cumulative_membership_changed'):
            self.check(dict(self.prep, pending=self.put([])))

    def test_changed_new_job_refused(self):
        changed = dict(self.new_row, native_job_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'new_input_job_changed'):
            self.check(dict(self.prep, ready=self.put(self.old_ready + [changed])))

    def test_self_requests_refused(self):
        with self.assertRaisesRegex(ValueError, 'input_source_or_scope_changed'):
            self.check(dict(self.prep, self_jobs=self.put([{'call': 'forbidden'}])))

    def test_changed_reader_protocol_refused(self):
        with self.assertRaisesRegex(ValueError, 'reader_protocol_changed'):
            self.check(dict(self.prep, checker_config=self.put({'changed': True})))

    def test_boolean_count_is_not_an_integer_count(self):
        counts = dict(self.prep['counts'], self_requests=False)
        with self.assertRaisesRegex(ValueError, 'input_counts_changed'):
            self.check(dict(self.prep, counts=counts))

    def test_previous_added_row_cannot_change(self):
        prior = dict(self.prep)
        changed = dict(self.new_row, arbitrary_replacement=True)
        with self.assertRaisesRegex(ValueError, 'previous_input_removed_or_relabelled'):
            self.check(dict(self.prep, ready=self.put(self.old_ready + [changed])), prior)

    def history(self):
        source = self.put('native-source'); old_cut = {'actual': self.put('old-cut'), 'review': self.put('peer')}
        prior = dict(source=source, prepared=self.put(self.prep), intervening_reader_cuts=[old_cut])
        prior_ref = self.put(prior)
        closure = dict(plan=prior_ref, root='root', journal=self.put('journal'), runtime_result=self.put({}),
                       all_claims_accounted=True, active=0, unclaimed=0, quarantine=None)
        cr = self.put(closure)
        review = self.put(dict(accepted_finite_reader_route_closed_cut=True, actual=cr, plan=prior_ref,
            reader_source=source, root=closure['root'], journal=closure['journal'],
            runtime_result=closure['runtime_result'], findings=[]))
        binding = dict(self.binding, previous_plan=prior_ref, previous_closed=cr,
                       previous_closed_review=review, fixed_plan_fields={'immutable_history': True})
        plan = dict(intervening_reader_cuts=[old_cut, dict(actual=cr, review=review)],
                    route_predecessor=cr, immutable_history=True)
        return binding, plan

    def test_complete_history_required(self):
        b, p = self.history()
        e.validate_history(b, p)
        p['intervening_reader_cuts'].pop(0)
        with self.assertRaisesRegex(ValueError, 'prior_history_changed'):
            e.validate_history(b, p)

    def test_reordered_or_reset_history_refused(self):
        for change in ('reverse', 'reset'):
            with self.subTest(change=change):
                b, p = self.history()
                if change == 'reverse':
                    p['intervening_reader_cuts'].reverse()
                else:
                    p['route_predecessor'] = None
                with self.assertRaisesRegex(ValueError, 'prior_history_changed'):
                    e.validate_history(b, p)

    def namespace_fixture(self):
        old_known = dict(schema='old', allocation=self.base['cohort'], global_count=2,
                         reader_instruments=e.READERS, checker_config=self.protocol['checker_config'])
        old_ref = self.put(old_known)
        known = dict(old_known, schema='causes.extended_reader_known.v1', parent=old_ref,
                     allocation=self.binding['cohort'], global_count=4)
        new_ref = self.put(known)
        constants = dict(materializer=self.binding['row_materializer'], fresh_known=old_ref,
                         plan_fields={'frozen': True}, modules={})
        source = self.root / 'runtime.py'
        source.write_text('KNOWN = ' + repr(constants) + '\n'
                          'def load(*args): return args\n'
                          'def gates(): return KNOWN, __file__\n'
                          'def run_finite(): return gates()\n')
        b = dict(self.binding, schema='causes.reader_cohort_extension_binding.v1', source=e.pin(e.__file__),
                 parent_runtime=e.pin(source), parent_known=old_ref, known=new_ref,
                 fixed_plan_fields=constants['plan_fields'])
        ref = self.put(b)
        review = self.put(dict(accepted_reader_cohort_extension_binding=True, binding=ref,
            source=b['source'], parent_runtime=b['parent_runtime'], row_materializer=b['row_materializer'],
            known=b['known'], findings=[]))
        return b, ref, review

    def test_native_code_objects_and_parent_globals_unchanged(self):
        b, ref, review = self.namespace_fixture()
        parent = e.module(b['parent_runtime']); before = copy.deepcopy(parent.KNOWN)
        bound = e.runtime_namespace(ref, review)
        for name in ('gates', 'run_finite'):
            self.assertIs(bound[name].__code__, getattr(parent, name).__code__)
            self.assertIsNot(bound[name].__globals__, getattr(parent, name).__globals__)
        known, source = bound['run_finite']()
        self.assertEqual(source, e.__file__)
        self.assertEqual(known['materializer'], e.pin(e.__file__))
        self.assertEqual(parent.KNOWN, before)
        self.assertEqual(parent.run_finite(), (before, parent.__file__))

    def test_changed_native_protocol_refused_before_runtime(self):
        b, _, _ = self.namespace_fixture()
        known = e.checked(b['known']); known['reader_instruments'] = ['presence']
        b['known'] = self.put(known); ref = self.put(b)
        peer = self.put(dict(accepted_reader_cohort_extension_binding=True, binding=ref, source=b['source'],
            parent_runtime=b['parent_runtime'], row_materializer=b['row_materializer'], known=b['known'], findings=[]))
        with self.assertRaisesRegex(ValueError, 'native_protocol_binding_changed'):
            e.runtime_namespace(ref, peer)

    def test_legacy_collector_runtime_remains_identical(self):
        runtime = object()
        self.assertIs(audit.runtime_for_plan(runtime, {'source': 'legacy'}), runtime)

    def test_explicit_collector_binding_and_missing_join_refusals(self):
        b, ref, review = self.namespace_fixture()
        plan = dict(source=e.pin(e.__file__), cohort=b['cohort'],
                    cohort_extension=ref, cohort_extension_review=review)
        runtime = audit.runtime_for_plan(e, plan)
        self.assertIs(runtime.run_finite.__code__, e.module(b['parent_runtime']).run_finite.__code__)
        for field in ('source', 'cohort', 'cohort_extension', 'cohort_extension_review'):
            with self.subTest(field=field):
                changed = dict(plan); changed[field] = None
                with self.assertRaisesRegex(ValueError, 'plan_extension_required|consumer_extension_join'):
                    audit.runtime_for_plan(e, changed)

    def copy_fixture(self):
        selection = copy.deepcopy(self.selection)
        for index, row in enumerate(selection['rows']):
            row.update(relative_run='runs/' + row['run_id'], provider_role='route',
                       stage='first8', execution_host='producer')
        selection['stages'] = [dict(role='route', stage='first8', output_root='/frozen/runs')]
        config = self.put({'immutable': True}); source = self.put('actor-source')
        plan = dict(runtime_authorized=True, role='route', output='/frozen/runs',
                    execution_host='producer', jobs=self.selection['global_jobs'], config=config, source=source)
        pref = self.put(plan)
        peer = self.put(dict(accepted_requested_fixed8_actor_plan=True, plan=pref, source=source, findings=[]))
        receipt = dict(run='runs/new-a', schema='caus.reader_input_receipt.v1', custody_kind='actor_reader_inputs')
        request = dict(producers=[dict(role='route', stage='first8', plan=pref, review=peer)])
        jobs = {j['allocation']['run_id']: {'job': j} for j in self.jobs}
        return selection, request, jobs, receipt

    def copy_request(self, request, receipts):
        self.serial += 1; path = self.root / f'index-{self.serial}.jsonl'
        path.write_bytes(b''.join(e.encoded(r) + b'\n' for r in receipts)); ref = e.pin(path)
        cut = dict(selection=self.binding['selection'], configuration=self.put({}), destination=str(self.root),
                   accepted=len(receipts), index=dict(saved=ref, bytes=ref['bytes'], sha256=ref['sha256'],
                   original_path=str(self.root / 'reader_inputs.accepted.jsonl')))
        cr = self.put(cut)
        cp = self.put(dict(accepted_requested_actor_reader_input_cut=True, actual=cr,
            selection=self.binding['selection'], configuration=cut['configuration'], index=ref,
            accepted=len(receipts), findings=[]))
        return dict(request, feed_cut=cr, feed_cut_review=cp)

    def test_copy_resolves_only_exact_selected_producer(self):
        selection, request, jobs, receipt = self.copy_fixture()
        rows = e.copied_rows(self.binding, self.copy_request(request, [receipt]), selection, jobs)
        self.assertEqual(list(rows), ['new-a'])
        self.assertEqual(rows['new-a']['job'], self.jobs[0])

    def test_copy_duplicate_foreign_or_unbound_refused(self):
        for mode in ('duplicate', 'foreign', 'unbound'):
            with self.subTest(mode=mode):
                selection, request, jobs, receipt = self.copy_fixture()
                receipts = [receipt]
                if mode == 'duplicate':
                    receipts.append(receipt)
                elif mode == 'foreign':
                    receipt['run'] = 'runs/foreign'
                else:
                    request['producers'] = []
                with self.assertRaisesRegex(ValueError, 'copy_duplicate_or_unbound|foreign_copy'):
                    e.copied_rows(self.binding, self.copy_request(request, receipts), selection, jobs)

    def test_copy_producer_job_change_refused(self):
        selection, request, jobs, receipt = self.copy_fixture()
        item = request['producers'][0]; p = e.checked(item['plan'])
        changed = copy.deepcopy(self.jobs); changed[0]['allocation']['continue_to_end'] = 0
        p['jobs'] = self.put(changed); item['plan'] = self.put(p)
        item['review'] = self.put(dict(accepted_requested_fixed8_actor_plan=True,
                                      plan=item['plan'], source=p['source'], findings=[]))
        with self.assertRaisesRegex(ValueError, 'producer_fixed_jobs_changed'):
            e.copied_rows(self.binding, self.copy_request(request, [receipt]), selection, jobs)


if __name__ == '__main__':
    unittest.main()
