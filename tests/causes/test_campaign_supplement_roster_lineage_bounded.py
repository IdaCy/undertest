"""Versioned, exact-pin read policy using only synthetic saved metadata."""
import copy
import hashlib
import unittest

from causes import campaign_supplement_roster_lineage as default
from causes import campaign_supplement_roster_lineage_bounded as bounded
from tests.causes.test_campaign_supplement_roster_lineage import TransitionFixture, encoded


MIB = 1024**2


def policy(refs):
    return dict(schema='causes.immutable_roster_parent_read_policy.v1',
        large_refs=copy.deepcopy(refs), maximum_reads_per_large_pin=2,
        ordinary_per_ref_bytes=8*MIB, ordinary_requested_bytes=64*MIB,
        maximum_calls=4096,
        maximum_requested_bytes=64*MIB+2*sum(ref['bytes'] for ref in refs))


class BoundedFixture(TransitionFixture):
    def __init__(self):
        self.policy_changes = {}
        self.policy_bindings = {}
        super().__init__()

    def build(self):
        if not hasattr(self, 'large_refs'):
            self.large_refs = []
            for i in range(2):
                body = bytes([65+i]) * (8*MIB+i+1)
                ref = dict(path='/fixture/parent_bank_'+str(i), bytes=len(body),
                           sha256=hashlib.sha256(body).hexdigest())
                self.initial_data[ref['path']] = body
                self.baseline['execution_pins'].append(ref)
                self.large_refs.append(ref)
        super().build()
        s = self.saved
        read_policy = policy(self.large_refs)
        read_policy.update(self.policy_changes)
        self.parent_policy = s.put(read_policy)

        def replace(ref, label, **changes):
            value = s.value(ref)
            value.update(changes)
            value['parent_read_policy'] = self.policy_bindings.get(label, self.parent_policy)
            del s.data[ref['path']]
            return s.put(value)

        derivation = replace(self.transition['source_derivation'], 'derivation')
        self.prep_ref = replace(self.prep_ref, 'preparation', source_derivation=derivation)
        self.peer_ref = replace(self.peer_ref, 'peer', source_derivation=derivation,
                                source_preparation=self.prep_ref)
        self.adoption_ref = replace(self.adoption_ref, 'adoption', source_derivation=derivation,
            source_preparation=self.prep_ref, source_review=self.peer_ref)
        self.reference = replace(self.reference, 'transition', schema=bounded.TRANSITION,
            source_derivation=derivation, source_preparation=self.prep_ref,
            source_review=self.peer_ref, root_source_adoption=self.adoption_ref)
        self.transition = s.value(self.reference)
        original = {ref['path'] for ref in self.baseline['execution_pins']}
        self.plan.update(native_source_migration=self.reference,
            immutable_roster_reuse=dict(enabled=True, policy=self.policy_ref,
                source_review=self.peer_ref, root_adoption=self.adoption_ref),
            execution_pins=self.baseline['execution_pins'] +
                [self.pin(path) for path in sorted(s.data) if path not in original])

    def parent(self, baseline, *, read):
        result = super().parent(baseline, read=read)
        for ref in self.large_refs:
            read(ref)
        return dict(result, required_pins=result['required_pins']+self.large_refs)

    def check(self, **kwargs):
        options = dict(reference=self.reference, plan=self.plan, read=self.read,
                       validate_parent=self.parent, parent_read_policy=self.parent_policy)
        options.update(kwargs)
        return bounded.validate_transition(**options)


class BoundedLineageTests(unittest.TestCase):
    def refuse(self, fixture, reason, **options):
        with self.assertRaisesRegex(bounded.RosterLineageError, reason):
            fixture.check(**options)

    def reader(self, changes=None):
        f = BoundedFixture()
        p = policy(f.large_refs)
        p.update(changes or {})
        return f, bounded._Reader(f.read, f.saved.put(p))

    def test_exact_two_parent_reads_are_rehashed_and_protected(self):
        f = BoundedFixture()
        before = copy.deepcopy(f.saved.data)
        result = f.check()
        self.assertEqual(f.parent_calls, 1)
        self.assertEqual(result['large_reads'], {ref['path']: 2 for ref in f.large_refs})
        self.assertEqual(result['requested_bytes'], result['ordinary_requested_bytes'] +
                         2*sum(ref['bytes'] for ref in f.large_refs))
        self.assertIn(f.parent_policy, result['required_pins'])
        for ref in f.large_refs:
            self.assertIn(ref, result['required_pins'])
            self.assertEqual(f.reads.count(ref['path']), 2)
        self.assertEqual(f.saved.data, before)
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(f.plan['retained_failures'], ['old/failed'])
        self.assertEqual(f.plan['initial_failures'], 1)

    def test_default_api_still_rejects_large_parent_reads(self):
        f = TransitionFixture()
        self.assertFalse(f.check()['runtime_authorized'])
        s = f.saved
        large = s.put(b'x'*(8*MIB+1))
        reader = default._Reader(f.read)
        f.reads.clear()
        with self.assertRaisesRegex(default.RosterLineageError, 'pin_shape_or_bound'):
            reader.raw(large)
        self.assertEqual(f.reads, [])
        with self.assertRaises(TypeError):
            default.validate_transition(f.reference, plan=f.plan, read=f.read,
                validate_parent=f.parent, parent_read_policy=s.put(policy([large])))

    def test_parent_refusal_still_precedes_new_source_acceptance(self):
        f = BoundedFixture()
        def refuse(baseline, *, read):
            self.assertEqual(baseline, f.baseline)
            raise RuntimeError('literal parent refused')
        with self.assertRaisesRegex(RuntimeError, 'literal parent refused'):
            f.check(validate_parent=refuse)
        self.assertNotIn(f.helper['path'], f.reads)

    def test_policy_must_bind_every_acceptance_and_derivation(self):
        for label, reason in [('transition', 'explicit_transition'),
                              ('preparation', 'transition_acceptance'),
                              ('peer', 'transition_acceptance'),
                              ('adoption', 'transition_acceptance'),
                              ('derivation', 'transition_derivation')]:
            with self.subTest(label=label):
                f = BoundedFixture()
                f.policy_bindings[label] = dict(f.parent_policy, sha256='0'*64)
                f.build()
                self.refuse(f, reason)

    def test_policy_exact_fixed_bounds_and_total(self):
        for key, value in [('maximum_reads_per_large_pin', 3), ('maximum_reads_per_large_pin', True),
            ('ordinary_per_ref_bytes', 9*MIB), ('ordinary_requested_bytes', 65*MIB),
            ('maximum_calls', 4097), ('maximum_requested_bytes', 1000*MIB)]:
            with self.subTest(key=key):
                f = BoundedFixture(); f.policy_changes[key] = value; f.build()
                self.refuse(f, 'parent_read_policy_(fixed_bounds|exact_total)')

    def test_policy_requires_one_or_two_distinct_large_pins(self):
        f = BoundedFixture()
        variants = [[], f.large_refs*2, [f.large_refs[0]]*2,
                    [dict(f.large_refs[0], bytes=8*MIB)],
                    [dict(f.large_refs[0], bytes=128*MIB+1)]]
        for refs in variants:
            with self.subTest(refs=refs):
                with self.assertRaises(bounded.RosterLineageError):
                    bounded._Reader(f.read, f.saved.put(policy(refs)))

    def test_policy_is_hashed_and_shape_bounded(self):
        f = BoundedFixture()
        f.saved.data[f.parent_policy['path']] += b' '
        self.refuse(f, 'pin_bytes')
        f = BoundedFixture(); f.policy_changes['unreviewed_override'] = True; f.build()
        self.refuse(f, 'parent_read_policy_shape')
        f.reads.clear()
        with self.assertRaisesRegex(bounded.RosterLineageError, 'pin_shape_or_bound'):
            bounded._Reader(f.read, dict(f.parent_policy, bytes=32769))
        self.assertEqual(f.reads, [])

    def test_exception_identity_and_third_read_refuse_before_io(self):
        for field, value in [('bytes', 8*MIB+10), ('sha256', '0'*64)]:
            f, reader = self.reader()
            before = len(f.reads)
            with self.assertRaisesRegex(bounded.RosterLineageError, 'parent_large_pin_changed'):
                reader.raw(dict(f.large_refs[0], **{field:value}))
            self.assertEqual(len(f.reads), before)
        f, reader = self.reader()
        reader.raw(f.large_refs[0]); reader.raw(f.large_refs[0])
        before = len(f.reads)
        with self.assertRaisesRegex(bounded.RosterLineageError, 'parent_large_pin_read_bound'):
            reader.raw(f.large_refs[0])
        self.assertEqual(len(f.reads), before)

    def test_unnamed_large_path_refuses_before_io(self):
        f, reader = self.reader()
        before = len(f.reads)
        with self.assertRaisesRegex(bounded.RosterLineageError, 'pin_shape_or_bound'):
            reader.raw(dict(f.large_refs[0], path='/fixture/unlisted_bank'))
        self.assertEqual(len(f.reads), before)

    def test_large_body_hash_and_second_read_mutation_are_checked(self):
        f, reader = self.reader(); ref = f.large_refs[0]
        reader.raw(ref)
        f.saved.data[ref['path']] = b'Z'*ref['bytes']
        with self.assertRaisesRegex(bounded.RosterLineageError, 'pin_bytes'):
            reader.raw(ref)

    def test_ordinary_aggregate_budget_is_not_increased(self):
        f, reader = self.reader()
        ref = f.saved.put(b'z'*(8*MIB))
        for _ in range(7):
            reader.raw(ref)
        before = len(f.reads)
        with self.assertRaisesRegex(bounded.RosterLineageError, 'ordinary_metadata_read_bound'):
            reader.raw(ref)
        self.assertEqual(len(f.reads), before)

    def test_zero_byte_reads_still_consume_call_budget(self):
        f, reader = self.reader(); ref = f.saved.put(b'')
        for _ in range(4095):
            reader.raw(ref)
        before = len(f.reads)
        with self.assertRaisesRegex(bounded.RosterLineageError, 'metadata_read_bound'):
            reader.raw(ref)
        self.assertEqual(len(f.reads), before)

    def test_unused_or_unreturned_exception_does_not_finish(self):
        for omit in ('all', 'return'):
            f = BoundedFixture()
            def parent(baseline, *, read):
                result = TransitionFixture.parent(f, baseline, read=read)
                if omit == 'return':
                    for ref in f.large_refs:
                        read(ref)
                return result
            self.refuse(f, 'parent_large_pin_callback_evidence', validate_parent=parent)

    def test_callback_double_read_cannot_replace_required_pin_rehash(self):
        f = BoundedFixture()
        def parent(baseline, *, read):
            result = TransitionFixture.parent(f, baseline, read=read)
            for ref in f.large_refs:
                read(ref); read(ref)
            return result
        self.refuse(f, 'parent_large_pin_callback_evidence', validate_parent=parent)

    def test_large_prephase_metadata_cannot_consume_callback_read(self):
        for target in ('baseline_plan', 'parent_validator_source'):
            with self.subTest(target=target):
                saved = {}
                def put(body):
                    path = '/fixture/prephase_' + str(len(saved))
                    saved[path] = body
                    return dict(path=path, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                baseline = b'{"immutable_roster_reuse":null}'
                source = b'synthetic callback source'
                if target == 'baseline_plan':
                    baseline += b' '*(8*MIB+1-len(baseline))
                else:
                    source += b' '*(8*MIB+1-len(source))
                refs = dict(baseline_plan=put(baseline), parent_validator_source=put(source))
                policy_ref = put(encoded(policy([refs[target]])))
                transition = dict.fromkeys(('baseline_review', 'sources', 'source_preparation',
                    'source_derivation', 'source_review', 'root_source_adoption', 'policy',
                    'helper_source', 'binder_source', 'builder_source'))
                transition.update(refs, schema=bounded.TRANSITION, runtime_authorized=False,
                                  parent_read_policy=policy_ref)
                called = []
                def parent(baseline, *, read):
                    called.append(True)
                    raise AssertionError('callback must not run')
                with self.assertRaisesRegex(bounded.RosterLineageError,
                                             'parent_large_pin_read_before_callback'):
                    bounded.validate_transition(put(encoded(transition)), plan={},
                        read=lambda ref:saved[ref['path']], validate_parent=parent,
                        parent_read_policy=policy_ref)
                self.assertEqual(called, [])

    def test_callback_large_evidence_cannot_be_missing_duplicate_or_changed(self):
        for change in ('missing', 'duplicate', 'changed'):
            f = BoundedFixture()
            def parent(baseline, *, read):
                result = f.parent(baseline, read=read)
                refs = result['required_pins']
                if change == 'missing':
                    refs.remove(f.large_refs[0])
                elif change == 'duplicate':
                    refs.append(f.large_refs[0])
                else:
                    refs[refs.index(f.large_refs[0])] = dict(f.large_refs[0], sha256='0'*64)
                return result
            self.refuse(f, 'parent_large_pin_callback_evidence', validate_parent=parent)

    def test_large_parent_evidence_cannot_enter_reuse_allowlist(self):
        f = BoundedFixture()
        f.change('policy', allowlist=[f.large_refs[0]],
                 roster_sha256=hashlib.sha256(encoded([f.large_refs[0]])).hexdigest())
        self.refuse(f, 'allowlist_unprotected_input')

    def test_failed_history_queues_and_constructor_remain_exact(self):
        for key, value in [('initial_failures', 0), ('retained_failures', []),
                           ('ordered_jobs', ['next/two', 'next/one']),
                           ('constructor', {}), ('predecessor_custody', {})]:
            f = BoundedFixture(); f.plan[key] = value
            self.refuse(f, 'candidate_differs_from_exact_projection')
        f = BoundedFixture(); f.new_sources['native_queue'] = b'changed queue'; f.build()
        self.refuse(f, 'operational_source_delta')


if __name__ == '__main__':
    unittest.main()
