import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_reader_continuation as adapter


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.serial = 0
        self.source = self.put({'native': 'unchanged'})
        self.cohort = self.put({'jobs': ['old', 'failed', 'new']})
        history = self.put({'claimed_keys': ['old'], 'final_consecutive_failed_contexts': 1})
        history_peer = self.put({'accepted_finite_reader_route_closed_cut': True, 'actual': history, 'findings': []})
        self.history = [{'actual': history, 'review': history_peer}]
        previous_prep = self.put({'counts': {'selected': 3}, 'reader_source': self.source})
        self.previous = dict(source=self.source, cohort=self.cohort, previous_cohort_claimed_keys=['old'],
                             expected_unclaimed_contexts=1, intervening_reader_cuts=self.history,
                             prepared=previous_prep)
        previous = self.put(self.previous)
        previous_peer = self.put(dict(accepted_fresh_candidate_reader_plan=True, plan=previous,
                                      source=self.source, findings=[]))
        self.carry = self.put({'consecutive_failed_contexts': 1, 'quarantine': None})
        self.rows = [dict(event='reader_route_state_carried', consecutive_failed_contexts=1,
                          quarantine=None, carry=self.carry),
                     dict(event='claimed', key='failed'),
                     dict(event='reader_route_failure', consecutive_failed_contexts=2),
                     dict(event='unresolved_prior_reader_attempt', key='failed')]
        self.actual = dict(plan=previous, plan_review=previous_peer, reader_source=self.source,
                           root=str(self.root/'previous-output'), journal=self.journal(self.rows),
                           runtime_result=self.put({'result': 'closed'}), claimed_keys=['failed'],
                           claimed=1, finished_or_unresolved=1, ready=1, active=0, unclaimed=0,
                           all_claims_accounted=True, initial_consecutive_failed_contexts=1,
                           final_consecutive_failed_contexts=2, quarantine=None, route_state_carry=self.carry)
        self.pending = dict(schema='causes.fresh_candidate_reader_plan.v1', source=self.source,
                            materializer=self.source, cohort=self.cohort, measurements_only=True,
                            workers=4, output=str(self.root/'runtime'), pause_file=str(self.root/'runtime/PAUSE'),
                            pause_paths=[str(self.root/'PAUSE')], revocation_file=str(self.root/'REVOKED'),
                            preserved_policy={'no_retry': True}, intervening_reader_cuts=self.history +
                            [{'actual': None, 'review': None}], root_authorized=False, runtime_authorized=False)
        for key in adapter.LATE_FIELDS - {'intervening_reader_cuts', 'root_authorized', 'runtime_authorized'}:
            self.pending[key] = None
        self.ready = ['new', 'failed', 'old']
        self.prepare()

    def put(self, value):
        self.serial += 1
        path = self.root / f'{self.serial}.json'
        path.write_text(json.dumps(value, sort_keys=True) + '\n')
        return adapter.pin(path)

    def journal(self, rows):
        self.serial += 1
        path = self.root / f'{self.serial}.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return adapter.pin(path)

    def prepare(self, counts=None):
        actual_ref = self.put(self.actual)
        actual_peer = self.put(dict(accepted_finite_reader_route_closed_cut=True, actual=actual_ref, findings=[],
                                    **{k: self.actual[k] for k in ('plan', 'reader_source', 'root', 'journal', 'runtime_result')}))
        self.preparation = dict(source=self.source, reader_source=self.source, cohort=self.cohort,
                                ready=self.put([{'measurement_key': key} for key in self.ready]),
                                self_jobs=self.put([]), unavailable=self.put([]), pending=self.put([]),
                                counts=counts or dict(reader_ready=len(self.ready), unavailable=0,
                                                     copied=len(self.ready), pending=3-len(self.ready), selected=3))
        prep_ref = self.put(self.preparation)
        prep_peer = self.put(dict(accepted_fresh_candidate_reader_inputs=True, preparation=prep_ref, findings=[],
                                  preceding_closed_actual=actual_ref, preceding_closed_review=actual_peer,
                                  **self.preparation))
        self.descriptor = dict(schema=adapter.SCHEMA, pending_plan=self.put(self.pending),
                               preparation=prep_ref, preparation_review=prep_peer,
                               predecessor_actual=actual_ref, predecessor_review=actual_peer,
                               outputs={k: str(self.root/(k+'.json')) for k in ('plan','binding','review')})
        self.refresh()

    def refresh(self):
        self.descriptor_ref = self.put(self.descriptor)
        self.review = dict(accepted_reader_continuation_descriptor=True, descriptor=self.descriptor_ref,
                           source=adapter.pin(Path(adapter.__file__).resolve()), findings=[],
                           root_authorized_plan_binding=True, authorized_plan_flags={
                               'root_authorized': True, 'runtime_authorized': True},
                           maximum_invocations=1, automatic_retry=False)
        self.review_ref = self.put(self.review)

    def test_failed_attempt_excluded_in_ready_order_and_counter_preserved(self):
        _, plan, facts = adapter.derive_plan(self.descriptor_ref)
        self.assertEqual(plan['previous_cohort_claimed_keys'], ['failed','old'])
        self.assertEqual(facts['new_keys'], ['new'])
        self.assertEqual(facts['final_consecutive_failed_contexts'], 2)
        self.assertEqual(plan['preserved_policy'], self.pending['preserved_policy'])
        self.assertFalse(plan['runtime_authorized'])

    def test_native_unresolved_current_attempt_remains_claimed_and_excluded(self):
        self.rows[-1]['event']='unresolved_current_reader_attempt'
        self.actual['journal']=self.journal(self.rows);self.prepare()
        _, plan, facts=adapter.derive_plan(self.descriptor_ref)
        self.assertEqual(plan['previous_cohort_claimed_keys'], ['failed','old'])
        self.assertEqual(facts['new_keys'], ['new'])
        self.assertEqual(facts['final_consecutive_failed_contexts'], 2)

    def test_bind_then_independent_saved_validation_and_readonly_parity(self):
        result = adapter.bind(self.descriptor_ref, self.review_ref)
        peer = adapter.validate_saved(self.descriptor_ref, self.review_ref, result['plan'])
        self.assertTrue(adapter.load(peer)['accepted_reader_continuation_plan_binding'])
        native_peer = self.put(dict(accepted_fresh_candidate_reader_plan=True, findings=[],
                                   plan=result['plan'], source=self.source))
        with patch.object(adapter, 'save_exclusive', side_effect=AssertionError('parity wrote')):
            self.assertTrue(adapter.parity(self.descriptor_ref, result['plan'], native_peer)['exact_plan_parity'])
        self.assertFalse((self.root/'runtime').exists())
        with self.assertRaisesRegex(ValueError, 'output_already_exists'):
            adapter.bind(self.descriptor_ref, self.review_ref)

    def test_duplicate_ready_rejected(self):
        self.ready=['old','failed','failed']; self.prepare()
        with self.assertRaisesRegex(ValueError,'ready_duplicate'):
            adapter.derive_plan(self.descriptor_ref)

    def test_missing_previous_failed_claim_rejected(self):
        self.ready=['old','new']; self.prepare()
        with self.assertRaisesRegex(ValueError,'ready_lost_previous'):
            adapter.derive_plan(self.descriptor_ref)

    def test_duplicate_terminal_rejected(self):
        self.actual['journal']=self.journal(self.rows+[self.rows[-1]]);self.prepare()
        with self.assertRaisesRegex(ValueError,'journal_terminal_duplicate'):
            adapter.derive_plan(self.descriptor_ref)

    def test_unfinished_claim_rejected(self):
        self.actual['journal']=self.journal(self.rows[:-1]);self.prepare()
        with self.assertRaisesRegex(ValueError,'closed_journal_membership'):
            adapter.derive_plan(self.descriptor_ref)

    def test_changed_counter_rejected(self):
        self.actual['final_consecutive_failed_contexts']=0;self.prepare()
        with self.assertRaisesRegex(ValueError,'closed_route_counter'):
            adapter.derive_plan(self.descriptor_ref)

    def test_quarantine_rejected(self):
        self.actual['journal']=self.journal(self.rows+[{'event':'reader_route_quarantined'}]);self.prepare()
        with self.assertRaisesRegex(ValueError,'closed_route_held'):
            adapter.derive_plan(self.descriptor_ref)

    def test_history_order_and_missing_cut_rejected(self):
        self.pending['intervening_reader_cuts'].reverse();self.prepare()
        with self.assertRaisesRegex(ValueError,'history_order'):
            adapter.derive_plan(self.descriptor_ref)

    def test_replayed_historical_key_rejected(self):
        extra=self.put({'claimed_keys':['new'], 'final_consecutive_failed_contexts':1});peer=self.put(dict(accepted_finite_reader_route_closed_cut=True,actual=extra,findings=[]))
        self.previous['intervening_reader_cuts'].append(dict(actual=extra,review=peer))
        self.actual['plan']=self.put(self.previous)
        self.actual['plan_review']=self.put(dict(accepted_fresh_candidate_reader_plan=True,plan=self.actual['plan'],source=self.source,findings=[]))
        self.pending['intervening_reader_cuts']=self.previous['intervening_reader_cuts']+[dict(actual=None,review=None)]
        self.prepare()
        with self.assertRaisesRegex(ValueError,'no_new_or_replayed'):
            adapter.derive_plan(self.descriptor_ref)

    def test_changed_pin_rejected(self):
        Path(self.descriptor['preparation']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'pin_changed'):
            adapter.derive_plan(self.descriptor_ref)

    def test_authorization_and_source_join_required(self):
        self.review['authorized_plan_flags']['runtime_authorized']=False
        with self.assertRaisesRegex(ValueError,'plan_flags_not_authorized'):
            adapter.bind(self.descriptor_ref,self.put(self.review))
        self.review['source']=self.source
        with self.assertRaisesRegex(ValueError,'descriptor_joins'):
            adapter.bind(self.descriptor_ref,self.put(self.review))

    def test_existing_runtime_and_pause_refuse_before_any_metadata_write(self):
        (self.root/'runtime').mkdir()
        with self.assertRaisesRegex(ValueError,'runtime_output_already_exists'):
            adapter.bind(self.descriptor_ref,self.review_ref)
        (self.root/'runtime').rmdir();(self.root/'PAUSE').touch()
        with self.assertRaisesRegex(ValueError,'paused_or_revoked'):
            adapter.bind(self.descriptor_ref,self.review_ref)
        self.assertFalse(Path(self.descriptor['outputs']['plan']).exists())

    def test_output_alias_rejected_before_write(self):
        self.descriptor['outputs']['binding']=self.descriptor['outputs']['plan'];self.refresh()
        with self.assertRaisesRegex(ValueError,'output_alias'):
            adapter.bind(self.descriptor_ref,self.review_ref)

    def test_saved_order_mutation_rejected_even_with_new_pin(self):
        result=adapter.bind(self.descriptor_ref,self.review_ref)
        p=Path(result['plan']['path']);doc=json.loads(p.read_bytes())
        doc['previous_cohort_claimed_keys'].reverse();p.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,'saved_plan_changed'):
            adapter.validate_saved(self.descriptor_ref,self.review_ref,adapter.pin(p))

    def test_false_count_partition_rejected(self):
        self.prepare(dict(reader_ready=3,unavailable=1,copied=3,pending=0,selected=3))
        with self.assertRaisesRegex(ValueError,'input_count_partition'):
            adapter.derive_plan(self.descriptor_ref)

    def test_inherited_failure_cannot_be_reset(self):
        self.rows[0]['consecutive_failed_contexts']=0
        self.actual['initial_consecutive_failed_contexts']=0
        self.actual['journal']=self.journal(self.rows);self.prepare()
        with self.assertRaisesRegex(ValueError,'closed_carry_counter'):
            adapter.derive_plan(self.descriptor_ref)

    def test_empty_next_batch_is_not_bound(self):
        self.ready=['failed','old'];self.prepare()
        with self.assertRaisesRegex(ValueError,'no_new_or_replayed_selection'):
            adapter.derive_plan(self.descriptor_ref)

    def test_missing_output_parent_does_not_leave_partial_plan(self):
        self.descriptor['outputs']['binding']=str(self.root/'missing/binding.json');self.refresh()
        with self.assertRaisesRegex(ValueError,'output_parent_missing'):
            adapter.bind(self.descriptor_ref,self.review_ref)
        self.assertFalse(Path(self.descriptor['outputs']['plan']).exists())

    def test_metadata_cannot_create_pause_control(self):
        self.descriptor['outputs']['binding']=str(self.root/'PAUSE');self.refresh()
        with self.assertRaisesRegex(ValueError,'metadata_control_path_collision'):
            adapter.bind(self.descriptor_ref,self.review_ref)
        self.assertFalse((self.root/'PAUSE').exists())


if __name__ == '__main__':
    unittest.main()
