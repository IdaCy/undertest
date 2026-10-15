"""RAM-only handoff proof tests; injected validator never executes a queue."""
import copy
import hashlib
import json
import unittest

from causes import campaign_supplement_source_lineage as lineage


class Saved:
    def __init__(self):
        self.data = {}

    def put(self, obj):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, sort_keys=True).encode()
        path = '/fixture/ready/' + str(len(self.data))
        self.data[path] = body
        return dict(path=path, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

    def read(self, ref):
        return self.data[ref['path']]

    def value(self, ref):
        return json.loads(self.read(ref))


class Case:
    def __init__(self):
        self.m = Saved()
        self.sources = {k: self.m.put(k.encode()) for k in (
            'native_queue', 'completed_actor', 'driver', 'source_lineage',
            'derived_runtime', 'ready_migration')}
        self.binder = self.m.put(b'binder')
        self.migration = self.m.put(dict(original_ready_migration=True))
        self.prior = dict(sources=dict(self.sources, source_lineage=self.sources['ready_migration']),
            native_source_migration=self.migration,
            retain_admission_after_healthy_harvest=True, retain_admission_after_ready_harvest=True)
        self.actual = dict(schema='causes.finite_supplement_tranche_closed.v1',
            source=self.m.put(b'original driver'), active=0, unstarted_ids=[], ambiguous=[],
            unresolved_claimed_ids=[], quarantined=False, clean_native_ending=True,
            all_original_attempts_retained=True, original_denominator_preserved=True,
            qualification_findings=['inherited failed measurement retained'],
            complete_ids=['old/failure', 'old/success'], consecutive_failures=1)
        self.closed_peer = dict(accepted_finite_supplement_closed_custody=True,
            accepted_completed_actor_saved_closure=True, findings=[],
            all_original_attempts_retained=True, original_denominator_preserved=True)
        self.prep = {}
        self.derivation = dict(only_ready_compatibility_and_lineage_changed=True,
                               scientific_identity_preserved=True)
        self.peer = dict(accepted_derived_ready_harvest_runtime_sources=True, findings=[])
        self.adoption = dict(self.peer, root_authorized=True, runtime_authorized=False)
        self.calls = []
        self.result_changes = {}
        self.refresh()

    def refresh(self):
        m = self.m
        prior = m.put(self.prior)
        self.actual['plan'] = prior
        self.actual['native_invocations'] = [dict(plan=prior)]
        actual = m.put(self.actual)
        peer = dict(self.closed_peer, source=self.actual['source'], actual=actual,
                    qualification_findings=self.actual['qualification_findings'])
        self.predecessor = dict(actual=actual, review=m.put(peer))
        derivation = m.put(self.derivation)
        selected = dict(self.sources, binder=self.binder)
        prep = m.put(dict(self.prep, first_runtime_sources=selected, source_derivation=derivation))
        joins = dict(first_runtime_sources=selected, source_preparation=prep, source_derivation=derivation)
        review = m.put(dict(joins, **self.peer))
        adoption = m.put(dict(joins, independent_review=review, **self.adoption))
        self.step = dict(schema=lineage.READY_CONTINUATION,
            kind='retain_admission_after_ready_harvest', predecessor=self.predecessor,
            predecessor_plan=prior, previous_migration=self.migration,
            source=self.sources['native_queue'], completed_actor_source=self.sources['completed_actor'],
            driver_source=self.sources['driver'], binder_source=self.binder,
            lineage_source=self.sources['source_lineage'],
            migration_validator_source=self.sources['ready_migration'],
            source_preparation=prep, source_derivation=derivation,
            source_review=review, root_source_adoption=adoption)
        self.reference = m.put(self.step)

    def validator(self, plan, *, read):
        self.calls.append(copy.deepcopy(plan))
        if plan != self.prior:
            raise ValueError('not the actual prior plan')
        # The separately qualified callback must receive the authenticating reader.
        if json.loads(read(self.migration)) != dict(original_ready_migration=True):
            raise ValueError('wrong migration')
        return dict(dict(kind='authenticated_ready_harvest_migration', runtime_authorized=False,
                         required_pins=[self.migration]), **self.result_changes)

    def run(self):
        return lineage.validate_ready_continuation(self.reference, predecessor=self.predecessor,
            sources=self.sources, read=self.m.read, validate_migration=self.validator)

    def change_step(self, **changes):
        self.step.update(changes)
        self.reference = self.m.put(self.step)


class ReadyLineageTests(unittest.TestCase):
    def test_exact_handoff_calls_pinned_validator_and_preserves_failures(self):
        c = Case()
        before = copy.deepcopy(c.m.data)
        result = c.run()
        self.assertEqual(c.calls, [c.prior])
        self.assertEqual(result['kind'], 'same_source_ready_continuation')
        self.assertEqual(result['original_migration'], c.migration)
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(c.m.data, before)
        for pin in (c.migration, c.reference, *c.sources.values(), *c.predecessor.values()):
            self.assertIn(pin, result['required_pins'])
        self.assertEqual(c.m.value(c.predecessor['actual'])['consecutive_failures'], 1)

    def test_only_terminal_original_owner_can_handoff(self):
        for key, value in (('active', 1), ('active', False), ('unstarted_ids', ['unattempted']),
                           ('ambiguous', ['maybe']), ('unresolved_claimed_ids', ['pending']),
                           ('quarantined', True), ('clean_native_ending', False),
                           ('all_original_attempts_retained', False)):
            c = Case()
            c.actual[key] = value
            c.refresh()
            with self.subTest(key=key, value=value), self.assertRaises(lineage.SourceLineageError):
                c.run()
            self.assertEqual(c.calls, [])

    def test_both_prior_options_must_be_literal_true(self):
        for key in ('retain_admission_after_healthy_harvest', 'retain_admission_after_ready_harvest'):
            for value in (False, 1, None):
                c = Case()
                c.prior[key] = value
                c.refresh()
                with self.subTest(key=key, value=value), self.assertRaises(lineage.SourceLineageError):
                    c.run()

    def test_derived_or_transfer_owner_is_not_a_first_handoff(self):
        for key in ('derived_bank', 'original_reservation_transfer'):
            c = Case()
            c.prior[key] = {}
            c.refresh()
            with self.subTest(key=key), self.assertRaises(lineage.SourceLineageError):
                c.run()

    def test_source_substitution_and_validator_substitution_refuse(self):
        for key in ('native_queue', 'completed_actor', 'ready_migration'):
            c = Case()
            c.sources[key] = c.m.put(c.m.read(c.sources[key]))
            c.refresh()
            with self.subTest(key=key), self.assertRaisesRegex(lineage.SourceLineageError, 'endpoints'):
                c.run()

    def test_exact_closed_peer_and_actual_invocation_join_required(self):
        c = Case()
        peer = c.m.value(c.predecessor['review'])
        peer['actual'] = c.migration
        c.predecessor['review'] = c.m.put(peer)
        c.change_step(predecessor=c.predecessor)
        with self.assertRaisesRegex(lineage.SourceLineageError, 'closed_peer'):
            c.run()
        c = Case()
        actual = c.m.value(c.predecessor['actual'])
        actual['native_invocations'] = []
        actual_ref = c.m.put(actual)
        peer = c.m.value(c.predecessor['review'])
        peer['actual'] = actual_ref
        c.predecessor = dict(actual=actual_ref, review=c.m.put(peer))
        c.change_step(predecessor=c.predecessor)
        with self.assertRaisesRegex(lineage.SourceLineageError, 'closed_original'):
            c.run()

    def test_previous_migration_and_descriptor_shape_cannot_be_forged(self):
        for change in ({'previous_migration': None}, {'ignored_field': True},
                       {'schema': lineage.CONTINUATION}):
            c = Case()
            c.change_step(**change)
            with self.subTest(change=change), self.assertRaises(lineage.SourceLineageError):
                c.run()

    def test_unaccepted_or_runtime_source_adoption_refuses(self):
        for target, change in (('peer', {'accepted_derived_ready_harvest_runtime_sources': False}),
                               ('adoption', {'root_authorized': False}),
                               ('adoption', {'runtime_authorized': True}),
                               ('peer', {'findings': ['issue']}),
                               ('derivation', {'scientific_identity_preserved': False})):
            c = Case()
            getattr(c, target).update(change)
            c.refresh()
            with self.subTest(target=target, change=change), self.assertRaises(lineage.SourceLineageError):
                c.run()

    def test_callback_failure_or_unproved_migration_is_not_authority(self):
        for change in ({'kind': 'other'}, {'runtime_authorized': True}, {'required_pins': []}):
            c = Case()
            c.result_changes = change
            with self.subTest(change=change), self.assertRaisesRegex(lineage.SourceLineageError, 'migration_proof'):
                c.run()
        c = Case()
        def refused(*args, **kwargs):
            raise ValueError('fixed ancestry refused')
        c.validator = refused
        with self.assertRaisesRegex(ValueError, 'fixed ancestry refused'):
            c.run()

    def test_pin_bytes_conflicts_and_oversize_refuse_before_unbounded_read(self):
        c = Case()
        c.m.data[c.migration['path']] = b'changed'
        with self.assertRaisesRegex(lineage.SourceLineageError, 'pin_bytes'):
            c.run()
        c = Case()
        c.prep['extra'] = dict(c.sources['native_queue'], sha256='0' * 64)
        c.refresh()
        with self.assertRaisesRegex(lineage.SourceLineageError, 'conflicting_pin'):
            c.run()
        c = Case()
        c.reference = dict(c.reference, bytes=8 * 1024**2 + 1)
        with self.assertRaisesRegex(lineage.SourceLineageError, 'pin_bound'):
            c.run()

    def test_duplicate_keys_and_nonfinite_json_refuse(self):
        for body in (b'{"schema": 1, "schema": 2}', b'{"value": NaN}'):
            c = Case()
            c.reference = c.m.put(body)
            with self.subTest(body=body), self.assertRaises(lineage.SourceLineageError):
                c.run()


if __name__ == '__main__':
    unittest.main()
