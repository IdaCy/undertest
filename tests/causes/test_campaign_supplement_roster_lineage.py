"""Saved synthetic ancestry only; no operational plan or executor is created."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from causes import campaign_supplement_roster_lineage as reuse
from causes import campaign_supplement_source_lineage as lineage
from tests.causes.test_campaign_supplement_source_lineage import fixture


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class TransitionFixture:
    def __init__(self):
        self.saved, self.ancestry, self.origin = fixture()
        s = self.saved
        old_actual = s.value(self.ancestry['predecessor']['actual'])
        old_actual.update(source=s.put(b'saved auditor'), active=0, clean_native_ending=True,
                          ambiguous=[], unresolved_claimed_ids=[], quarantined=False,
                          all_original_attempts_retained=True, original_denominator_preserved=True,
                          qualification_findings=['one retained failed schema'])
        actual = s.put(old_actual)
        self.closed_peer = dict(accepted_finite_supplement_closed_custody=True,
            accepted_completed_actor_saved_closure=True, actual=actual, source=old_actual['source'],
            all_original_attempts_retained=True, original_denominator_preserved=True,
            qualification_findings=old_actual['qualification_findings'], findings=[])
        self.descriptor = dict(actual=actual, review=s.put(self.closed_peer))
        self.sources = dict(self.ancestry['sources'], owned_suffix=s.put(b'old suffix'),
                            source_lineage=s.put(Path(lineage.__file__).read_bytes()),
                            budget=s.put(b'unchanged budgets'))
        self.payload = s.put(b'unchanged inherited input')
        self.second_payload = s.put(b'another immutable input')
        self.constructor_input = s.put(b'constructor input remains fully checked')
        self.constructor = s.put(dict(science=self.constructor_input, same_summary_both_arms=True))
        self.baseline = dict(sources=self.sources, constructor=self.constructor,
            native_source_migration=self.ancestry['reference'], predecessor_custody=self.descriptor,
            retain_admission_after_healthy_harvest=True, retain_admission_after_ready_harvest=False,
            output_root='/fixture/future_output', occupied_ids=['old/failed', 'old/success'],
            ordered_jobs=['next/one', 'next/two'], initial_failures=1, attempts_consumed=7,
            captures_consumed=3, remaining_attempts=8, remaining_captures=2,
            retained_failures=['old/failed'], max_clients=8, storage_floor_bytes=80*1024**3,
            window_bytes=5*1024**3, exclusive_locks=['/fixture/lock'], execution_pins=[])
        self.baseline['execution_pins'] = [self.pin(path) for path in sorted(s.data)]
        self.initial_data = copy.deepcopy(s.data)
        self.new_sources = dict(self.sources)
        for key in reuse.OPERATIONAL_ROLES:
            self.new_sources[key] = b'new reviewed ' + key.encode()
        self.changes = {}
        self.parent_calls = 0
        self.reads = []
        self.build()

    def pin(self, path):
        body = self.saved.data[path]
        return dict(path=path, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

    def change(self, record, **fields):
        self.changes.setdefault(record, {}).update(fields)
        self.build()

    def build(self):
        s = self.saved
        s.data = copy.deepcopy(self.initial_data)
        s.serial = max(int(path.rsplit('_', 1)[1]) for path in s.data
                       if path.startswith('/fixture/record_'))
        self.baseline_ref = s.put(self.baseline)
        self.baseline_peer = dict(accepted_original_owned_suffix_baseline=True,
            plan=self.baseline_ref, validator_source=self.sources['source_lineage'],
            runtime_authorized=False, findings=[])
        self.baseline_peer.update(self.changes.get('baseline_peer', {}))
        self.baseline_peer_ref = s.put(self.baseline_peer)
        sources = {key: s.put(value) if type(value) is bytes else value
                   for key, value in self.new_sources.items()}
        self.helper = s.put(b'reviewed immutable roster proof')
        self.binder = s.put(b'reviewed metadata binder')
        self.builder = s.put(b'reviewed metadata builder')
        policy = dict(schema='causes.immutable_roster_reuse_policy.v1', helper=self.helper,
                      allowlist=[self.payload, self.second_payload],
                      roster_sha256=hashlib.sha256(encoded([self.payload, self.second_payload])).hexdigest())
        policy.update(self.changes.get('policy', {}))
        self.policy_ref = s.put(policy)
        shared = dict(baseline_plan=self.baseline_ref, baseline_review=self.baseline_peer_ref,
            parent_validator_source=self.sources['source_lineage'], sources=sources,
            parent_sources=self.sources, helper_source=self.helper, binder_source=self.binder,
            builder_source=self.builder, policy=self.policy_ref, runtime_authorized=False)
        derivation = dict(only_immutable_roster_proof_reuse_changed=True,
            unchanged_scientific_inputs_and_limits=True, default_opt_in=False,
            **{k: shared[k] for k in ('sources', 'parent_sources', 'helper_source',
                                      'binder_source', 'builder_source', 'baseline_plan')})
        derivation.update(self.changes.get('derivation', {}))
        shared['source_derivation'] = s.put(derivation)
        prep = dict(shared); prep.update(self.changes.get('preparation', {}))
        self.prep_ref = s.put(prep)
        peer = dict(shared, source_preparation=self.prep_ref,
                    accepted_immutable_roster_reuse_source=True, findings=[],
                    source=self.helper, driver=sources['driver'])
        peer.update(self.changes.get('peer', {}))
        self.peer_ref = s.put(peer)
        adoption = dict(shared, schema='causes.immutable_roster_reuse_root_adoption.v1',
            root_authorized=True, accepted_immutable_roster_reuse_source=True, findings=[],
            source=self.helper, driver=sources['driver'], source_preparation=self.prep_ref,
            source_review=self.peer_ref)
        adoption.update(self.changes.get('adoption', {}))
        self.adoption_ref = s.put(adoption)
        self.transition = {key: shared[key] for key in ('baseline_plan', 'baseline_review',
            'parent_validator_source', 'sources', 'helper_source', 'binder_source',
            'builder_source', 'source_derivation', 'policy')}
        self.transition.update(schema=reuse.TRANSITION, runtime_authorized=False,
            source_preparation=self.prep_ref, source_review=self.peer_ref,
            root_source_adoption=self.adoption_ref)
        self.transition.update(self.changes.get('transition', {}))
        self.reference = s.put(self.transition)
        original_paths = {ref['path'] for ref in self.baseline['execution_pins']}
        additions = [self.pin(path) for path in sorted(s.data) if path not in original_paths]
        self.plan = copy.deepcopy(self.baseline)
        self.plan.update(sources=sources, native_source_migration=self.reference,
            immutable_roster_reuse=dict(enabled=True, policy=self.policy_ref,
                source_review=self.peer_ref, root_adoption=self.adoption_ref),
            execution_pins=self.baseline['execution_pins'] + additions)

    def parent(self, baseline, *, read):
        self.parent_calls += 1
        return lineage.validate_continuation(baseline['native_source_migration'],
            predecessor=baseline['predecessor_custody'], sources=baseline['sources'], read=read)

    def read(self, ref):
        self.reads.append(ref['path'])
        return self.saved.read(ref)

    def check(self, **kwargs):
        options = dict(reference=self.reference, plan=self.plan, read=self.read,
                       validate_parent=self.parent)
        options.update(kwargs)
        return reuse.validate_transition(**options)


class RosterLineageTests(unittest.TestCase):
    def refuse(self, f, reason, **kw):
        with self.assertRaises(reuse.RosterLineageError) as caught:
            f.check(**kw)
        self.assertIn(reason, str(caught.exception))

    def test_exact_transition_preserves_real_parent_validator_and_failed_history(self):
        f = TransitionFixture(); before = copy.deepcopy(f.saved.data)
        result = f.check()
        self.assertEqual(result['kind'], 'authenticated_immutable_roster_source_transition')
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(f.parent_calls, 1)
        self.assertEqual(result['parent_validation']['original_migration'], f.origin)
        self.assertEqual(f.saved.data, before)
        self.assertEqual(f.plan['retained_failures'], ['old/failed'])
        self.assertEqual(f.plan['initial_failures'], 1)
        self.assertNotIn(f.payload['path'], f.reads)
        self.assertNotIn(f.second_payload['path'], f.reads)
        self.assertEqual(f.plan['execution_pins'][:len(f.baseline['execution_pins'])],
                         f.baseline['execution_pins'])

    def test_parent_refusal_happens_before_new_sources_are_read(self):
        f = TransitionFixture()
        def refuse(baseline, *, read):
            self.assertEqual(baseline, f.baseline)
            raise lineage.SourceLineageError('original_parent_refusal')
        with self.assertRaisesRegex(lineage.SourceLineageError, 'original_parent_refusal'):
            f.check(validate_parent=refuse)
        self.assertNotIn(f.helper['path'], f.reads)
        self.assertNotIn(f.policy_ref['path'], f.reads)

    def test_parent_validator_cannot_mutate_baseline(self):
        f = TransitionFixture()
        def mutate(baseline, *, read):
            result = f.parent(baseline, read=read)
            baseline['initial_failures'] = 0
            return result
        self.refuse(f, 'parent_validator_changed_baseline', validate_parent=mutate)

    def test_missing_parent_validation_is_refused(self):
        self.refuse(TransitionFixture(), 'parent_validator_required', validate_parent=None)
        self.refuse(TransitionFixture(), 'parent_validation_result',
                    validate_parent=lambda baseline, read: True)

    def test_original_ancestry_is_rehashed_not_a_boolean_acceptance(self):
        f = TransitionFixture()
        original = f.saved.value(f.origin)
        f.saved.data[original['previous_source']['path']] = b'changed original source'
        self.refuse(f, 'pin_bytes')

    def test_queues_actor_and_budget_cannot_be_reviewed_away(self):
        for role in ('native_queue', 'completed_actor', 'budget'):
            with self.subTest(role=role):
                f = TransitionFixture(); f.new_sources[role] = b'new forbidden source'; f.build()
                self.refuse(f, 'operational_source_delta')

    def test_source_roles_cannot_be_added_or_silently_retained(self):
        f = TransitionFixture(); f.new_sources['extra'] = b'extra'; f.build()
        self.refuse(f, 'operational_source_delta')
        f = TransitionFixture(); f.new_sources['driver'] = f.sources['driver']; f.build()
        self.refuse(f, 'operational_source_delta')

    def test_constructor_order_counters_locks_and_closure_are_exact(self):
        changes = {'constructor': {'unexpected': 'constructor'}, 'ordered_jobs': ['next/two', 'next/one'],
            'occupied_ids': ['old/success'], 'initial_failures': 0, 'attempts_consumed': 6,
            'captures_consumed': 2, 'remaining_attempts': 9, 'remaining_captures': 3,
            'retained_failures': [], 'exclusive_locks': [], 'predecessor_custody': {},
            'max_clients': 9, 'storage_floor_bytes': 1, 'window_bytes': 1,
            'retain_admission_after_ready_harvest': True, 'unreviewed_flag': True}
        for key, value in changes.items():
            with self.subTest(key=key):
                f = TransitionFixture(); f.plan[key] = value
                self.refuse(f, 'candidate_differs_from_exact_projection')

    def test_numeric_equality_does_not_hide_bool_or_float_counter_changes(self):
        for value in (True, 1.0):
            f = TransitionFixture(); f.plan['initial_failures'] = value
            self.refuse(f, 'candidate_differs_from_exact_projection')

    def test_roster_cannot_drop_reorder_duplicate_or_add_unreviewed_pin(self):
        for edit in ('drop', 'reorder', 'duplicate', 'extra'):
            f = TransitionFixture(); roster = f.plan['execution_pins']
            if edit == 'drop': roster.pop(0)
            elif edit == 'reorder': roster[0], roster[1] = roster[1], roster[0]
            elif edit == 'duplicate': roster.append(roster[0])
            else: roster.append(f.saved.put(b'unreviewed evidence'))
            self.refuse(f, 'candidate_differs_from_exact_projection')

    def test_policy_requires_exact_order_digest_and_no_added_knobs(self):
        f = TransitionFixture(); f.change('policy', allowlist=[f.second_payload, f.payload])
        self.refuse(f, 'allowlist_digest_or_bound')
        f = TransitionFixture(); f.change('policy', refresh=True)
        self.refuse(f, 'exact_policy')
        for refs in ([], None):
            f = TransitionFixture(); f.change('policy', allowlist=refs)
            self.refuse(f, 'allowlist_bound')

    def test_allowlist_cannot_admit_duplicates_or_current_inputs(self):
        for selection in ('duplicate', 'constructor', 'nested', 'driver', 'unlisted'):
            f = TransitionFixture()
            refs = {'duplicate': [f.payload, f.payload], 'constructor': [f.constructor],
                    'nested': [f.constructor_input], 'driver': [f.sources['driver']],
                    'unlisted': [dict(f.payload, path='/fixture/not_in_roster')]}[selection]
            f.change('policy', allowlist=refs, roster_sha256=hashlib.sha256(encoded(refs)).hexdigest())
            self.refuse(f, 'allowlist_unprotected_input')

    def test_current_output_and_other_python_source_are_not_reusable(self):
        for path in ('/fixture/future_output/current.json', '/fixture/other_source.py'):
            f = TransitionFixture(); body=b'never reusable'
            ref=dict(path=path,bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
            f.initial_data[path]=body;f.baseline['execution_pins'].append(ref)
            f.changes['policy']={'allowlist':[ref],'roster_sha256':hashlib.sha256(encoded([ref])).hexdigest()}
            f.build()
            self.refuse(f, 'allowlist_unprotected_input')

    def test_peer_adoption_and_derivation_cannot_waive_changed_bindings(self):
        cases=[('baseline_peer', 'accepted_original_owned_suffix_baseline', False, 'baseline_acceptance'),
            ('baseline_peer', 'validator_source', {}, 'baseline_acceptance'),
            ('peer', 'accepted_immutable_roster_reuse_source', False, 'transition_acceptance'),
            ('peer', 'findings', ['unresolved'], 'transition_acceptance'),
            ('adoption', 'root_authorized', False, 'transition_acceptance'),
            ('adoption', 'runtime_authorized', True, 'transition_acceptance'),
            ('adoption', 'source_review', {}, 'transition_acceptance'),
            ('preparation', 'parent_sources', {}, 'transition_acceptance'),
            ('derivation', 'default_opt_in', True, 'transition_derivation'),
            ('derivation', 'unchanged_scientific_inputs_and_limits', False, 'transition_derivation')]
        for record,key,value,reason in cases:
            with self.subTest(record=record,key=key):
                f=TransitionFixture();f.change(record,**{key:value});self.refuse(f,reason)

    def test_closed_owner_and_failed_qualification_are_not_relaxed(self):
        for key,value in [('active',1),('active',False),('clean_native_ending',False),
                          ('unresolved_claimed_ids',['pending']),('quarantined',True)]:
            f=TransitionFixture();s=f.saved;actual=s.value(f.descriptor['actual']);actual[key]=value
            self._replace_closure(f,actual)
            self.refuse(f,'closed_owner')
        f=TransitionFixture();s=f.saved;peer=s.value(f.descriptor['review'])
        peer['qualification_findings']=[]
        f.baseline['predecessor_custody']['review']=s.put(peer)
        f.initial_data=copy.deepcopy(s.data);f.build()
        self.refuse(f,'closed_peer')

    @staticmethod
    def _replace_closure(f,actual):
        s=f.saved;new=s.put(actual);peer=s.value(f.descriptor['review']);peer['actual']=new
        f.baseline['predecessor_custody']=dict(actual=new,review=s.put(peer))
        f.initial_data=copy.deepcopy(s.data);f.build()

    def test_invalid_pin_and_oversize_record_refuse_before_reader(self):
        f=TransitionFixture();bad=dict(f.reference,bytes=8*1024**2+1)
        self.refuse(f,'pin_shape_or_bound',reference=bad);self.assertEqual(f.reads,[])
        for path in ('relative','/fixture/../other','//fixture/record','/fixture//record'):
            f=TransitionFixture();self.refuse(f,'pin_shape_or_bound',reference=dict(f.reference,path=path))
            self.assertEqual(f.reads,[])

    def test_duplicate_nonfinite_and_conflicting_metadata_refused(self):
        for body,reason in [(b'{"schema":1,"schema":2}','duplicate_json_key'),
                            (b'{"schema":NaN}','nonfinite_json')]:
            f=TransitionFixture();self.refuse(f,reason,reference=f.saved.put(body))
        f=TransitionFixture();f.saved.data[f.peer_ref['path']]+=b' '
        self.refuse(f,'pin_bytes')

    def test_false_opt_in_or_changed_migration_cannot_enter_default_path(self):
        for field,value in [('enabled',False),('enabled',1),('root_adoption',{})]:
            f=TransitionFixture();f.plan['immutable_roster_reuse'][field]=value
            self.refuse(f,'candidate_differs_from_exact_projection')
        f=TransitionFixture();f.plan['native_source_migration']=f.baseline['native_source_migration']
        self.refuse(f,'candidate_differs_from_exact_projection')

    def test_large_allowlist_pins_are_bounded_without_reading_their_bodies(self):
        f = TransitionFixture()
        refs = [dict(path='/fixture/large_' + str(i), bytes=128*1024**2,
                     sha256=hashlib.sha256(str(i).encode()).hexdigest()) for i in range(5)]
        f.baseline['execution_pins'].extend(refs)
        f.change('policy', allowlist=refs, roster_sha256=hashlib.sha256(encoded(refs)).hexdigest())
        self.refuse(f, 'allowlist_digest_or_bound')
        self.assertTrue(all(ref['path'] not in f.reads for ref in refs))
        f.change('policy', allowlist=refs[:4],
                 roster_sha256=hashlib.sha256(encoded(refs[:4])).hexdigest())
        self.assertFalse(f.check()['runtime_authorized'])
        self.assertTrue(all(ref['path'] not in f.reads for ref in refs))

    def test_metadata_reader_call_and_requested_byte_bounds_precede_io(self):
        empty = dict(path='/fixture/empty', bytes=0, sha256=hashlib.sha256(b'').hexdigest())
        calls = []
        reader = reuse._Reader(lambda ref: calls.append(ref) or b'')
        for _ in range(4096):
            reader.raw(empty)
        with self.assertRaisesRegex(reuse.RosterLineageError, 'metadata_read_bound'):
            reader.raw(empty)
        self.assertEqual(len(calls), 4096)
        block = b'x' * (8 * 1024**2)
        ref = dict(path='/fixture/metadata', bytes=len(block), sha256=hashlib.sha256(block).hexdigest())
        calls = []
        reader = reuse._Reader(lambda pin: calls.append(pin) or block)
        for _ in range(8):
            reader.raw(ref)
        with self.assertRaisesRegex(reuse.RosterLineageError, 'metadata_read_bound'):
            reader.raw(ref)
        self.assertEqual(len(calls), 8)

    def test_conflicting_execution_pin_and_missing_baseline_authority_refuse(self):
        f = TransitionFixture()
        conflict = dict(f.baseline['execution_pins'][0], sha256='f'*64)
        f.baseline['execution_pins'].append(conflict); f.build()
        self.refuse(f, 'baseline_roster_conflict')
        f = TransitionFixture(); f.change('baseline_peer', findings=['unresolved closure'])
        self.refuse(f, 'baseline_acceptance')
        f = TransitionFixture(); f.baseline['immutable_roster_reuse'] = {'enabled': True}; f.build()
        self.refuse(f, 'baseline_already_reuses')

    def test_duplicate_equal_baseline_pins_are_preserved_in_their_original_order(self):
        f = TransitionFixture(); f.baseline['execution_pins'].insert(2, f.baseline['execution_pins'][0])
        f.build(); result = f.check()
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(f.plan['execution_pins'][:len(f.baseline['execution_pins'])],
                         f.baseline['execution_pins'])


if __name__ == '__main__':
    unittest.main()
