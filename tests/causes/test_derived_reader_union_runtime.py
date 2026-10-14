import copy
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/derived_reader_union_runtime.py'
spec = importlib.util.spec_from_file_location('union_runtime_tests', SOURCE)
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def ref(name):
    return dict(path='/fixture/' + name, bytes=1, sha256='a' * 64)


class UnionRuntimeTests(unittest.TestCase):
    def data(self):
        source, fixed, parent, adapter = (ref(k) for k in ('continuation.py', 'fixed.py', 'parent.py', 'inputs.py'))
        union = dict(schema='causes.derived_reader_closed_union.v1', source=source,
            source_review=ref('continuation_peer'), binding=ref('old_binding'), binding_review=ref('old_binding_peer'),
            continuation_actual=ref('tail'), continuation_review=ref('tail_peer'),
            context_locations=[dict(key='old-a', root='/old'), dict(key='old-b', root='/old'),
                               dict(key='tail', root='/tail')], original_quarantine={'id': 'original'})
        extension = dict(schema='causes.derived_reader_closed_union_history.v1', union=ref('union'), review=ref('union_peer'))
        binding = dict(history_extension=extension, fixed_runtime=fixed, parent_runtime=parent,
            input_adapter=adapter, previous_plan=ref('prior_plan'), previous_closed=ref('tail'),
            previous_closed_review=ref('tail_peer'), fixed_plan_fields={'frozen': ref('frozen')})
        original = dict(intervening_reader_cuts=[dict(actual=ref('older'), review=ref('older_peer'))],
                        frozen=ref('frozen'))
        prior = dict(frozen=ref('frozen'), operational_policy=ref('policy'), revocation_file='/revoked',
                     pause_file='/tail/PAUSE', pause_paths=['/tail/PAUSE', '/old/PAUSE', '/global/PAUSE'])
        tail = dict(plan=ref('prior_plan'), active=0, unclaimed=0, quarantine=None)
        values = {'union': union, 'union_peer': dict(accepted_derived_reader_closed_union=True,
            union=ref('union'), findings=[], **{k: union[k] for k in ('source', 'binding', 'binding_review',
            'continuation_actual', 'continuation_review')}),
            'continuation_peer': dict(accepted_derived_reader_continuation_source=True,
                                     source=source, fixed_runtime=fixed, findings=[]),
            'old_binding': dict(fixed_runtime=fixed, parent_runtime=parent, input_adapter=adapter,
                                previous_plan=ref('original_plan')),
            'original_plan': original, 'prior_plan': prior, 'tail': tail}
        rebuilt = copy.deepcopy(union)
        continuation = types.SimpleNamespace(closed_union=lambda *args: copy.deepcopy(rebuilt))
        plan = dict(history_extension=extension, intervening_reader_cuts=original['intervening_reader_cuts'],
            route_predecessor=ref('tail'), frozen=ref('frozen'), operational_policy=ref('policy'),
            revocation_file='/revoked', output='/new_union_test', pause_file='/new_union_test/PAUSE',
            pause_paths=['/new_union_test/PAUSE', '/old/PAUSE', '/global/PAUSE'])
        return binding, plan, values, continuation

    def run_plan(self, binding, plan, values, continuation):
        with mock.patch.object(u, 'checked', lambda r: values[Path(r['path']).name]), \
             mock.patch.object(u, 'module', lambda r: continuation):
            return u.validate_union_plan(binding, plan, {'plan_fields': binding['fixed_plan_fields']})

    def test_exact_union_and_both_native_roots(self):
        b, p, v, c = self.data()
        self.assertEqual(self.run_plan(b, p, v, c), v['prior_plan'])

    def test_unaccepted_union_is_refused(self):
        b, p, v, c = self.data(); v['union_peer']['accepted_derived_reader_closed_union'] = False
        with self.assertRaisesRegex(ValueError, 'accepted_derived_reader_closed_union'):
            self.run_plan(b, p, v, c)

    def test_unaccepted_source_cannot_import_module(self):
        b, p, v, c = self.data()
        v['continuation_peer']['accepted_derived_reader_continuation_source'] = False
        with mock.patch.object(u, 'checked', lambda r: v[Path(r['path']).name]), \
             mock.patch.object(u, 'module') as imported:
            with self.assertRaisesRegex(ValueError, 'accepted_derived_reader_continuation_source'):
                u.verified_union(b)
        imported.assert_not_called()

    def test_changed_union_membership_is_refused(self):
        b, p, v, c = self.data(); v['union']['context_locations'].pop()
        with self.assertRaisesRegex(ValueError, 'whole_closed_union_changed'):
            self.run_plan(b, p, v, c)

    def test_original_quarantine_cannot_disappear(self):
        b, p, v, c = self.data(); v['union']['original_quarantine'] = None
        with self.assertRaisesRegex(ValueError, 'whole_closed_union_changed'):
            self.run_plan(b, p, v, c)

    def test_unknown_extension_field_is_refused(self):
        b, p, v, c = self.data(); b['history_extension']['skip_old'] = True
        with self.assertRaisesRegex(ValueError, 'union_extension'):
            self.run_plan(b, p, v, c)

    def test_wrong_fixed_runtime_is_refused(self):
        b, p, v, c = self.data(); v['old_binding']['fixed_runtime'] = ref('different.py')
        with self.assertRaisesRegex(ValueError, 'union_native_source_chain'):
            self.run_plan(b, p, v, c)

    def test_tail_cannot_replace_stopped_membership(self):
        b, p, v, c = self.data(); b['previous_closed'] = ref('stopped')
        with self.assertRaisesRegex(ValueError, 'union_latest_predecessor'):
            self.run_plan(b, p, v, c)

    def test_ordinary_history_is_retained(self):
        b, p, v, c = self.data(); p['intervening_reader_cuts'] = []
        with self.assertRaisesRegex(ValueError, 'typed_union_plan_history'):
            self.run_plan(b, p, v, c)

    def test_predecessor_must_be_the_completed_tail(self):
        b, p, v, c = self.data(); p['route_predecessor'] = ref('older')
        with self.assertRaisesRegex(ValueError, 'typed_union_plan_history'):
            self.run_plan(b, p, v, c)

    def test_fixed_native_ancestry_is_not_changed(self):
        b, p, v, c = self.data(); p['frozen'] = ref('other')
        with self.assertRaisesRegex(ValueError, 'fixed_native_history'):
            self.run_plan(b, p, v, c)

    def test_existing_native_output_cannot_be_reused(self):
        for output in ('/old', '/tail'):
            b, p, v, c = self.data(); p['output'] = output
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'new_union_successor_output'):
                self.run_plan(b, p, v, c)

    def test_old_and_global_pause_paths_remain(self):
        b, p, v, c = self.data(); p['pause_paths'] = ['/new_union_test/PAUSE']
        with self.assertRaisesRegex(ValueError, 'pause_scope_changed'):
            self.run_plan(b, p, v, c)

    def test_active_or_quarantined_tail_is_refused(self):
        for key, value in (('active', 1), ('unclaimed', 1), ('quarantine', {'id': 'new'})):
            b, p, v, c = self.data(); v['tail'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'completed_tail_required'):
                self.run_plan(b, p, v, c)


class CompletedTailTests(unittest.TestCase):
    def data(self, count=1):
        b, p, v, c = UnionRuntimeTests().data()
        entries = [dict(actual=ref('closed' + str(i)), review=ref('closed_peer' + str(i)))
                   for i in range(count)]
        extension = dict(b['history_extension'], schema='causes.derived_reader_closed_union_tails.v1',
                         closed_tails=entries)
        b['history_extension'] = extension
        v['tail']['plan'] = ref('prior_plan')
        previous, previous_plan = v['tail'], v['prior_plan']
        previous_ref, previous_review = ref('tail'), ref('tail_peer')
        for i, entry in enumerate(entries):
            suffix = str(i)
            source, binding_ref = ref('union_runtime.py'), ref('binding' + suffix)
            plan_ref, prep_ref = ref('plan' + suffix), ref('prep' + suffix)
            plan = dict(previous_plan, schema='causes.derived_reader_plan.v1', source=source,
                derived_binding=binding_ref, derived_binding_review=ref('binding_peer' + suffix),
                prepared=prep_ref, history_extension=u.history_prefix(extension, i),
                intervening_reader_cuts=copy.deepcopy(v['original_plan']['intervening_reader_cuts']),
                route_predecessor=previous_ref, output='/closed' + suffix,
                pause_file='/closed' + suffix + '/PAUSE')
            plan['pause_paths'] = [plan['pause_file']] + [x for x in previous_plan['pause_paths']
                                                         if x != previous_plan['pause_file']]
            old_binding = dict(b, source=source, source_review=ref('runtime_peer'), inputs=prep_ref,
                inputs_review=ref('inputs_peer' + suffix), history_extension=plan['history_extension'],
                previous_closed=previous_ref, previous_closed_review=previous_review,
                previous_plan=previous['plan'])
            actual = dict(schema='causes.finite_reader_route_closed_cut.v1', source=ref('capture.py'),
                plan=plan_ref, plan_review=ref('plan_peer' + suffix), reader_source=source,
                root=plan['output'], journal=ref('journal' + suffix), journal_original=ref('journal_original' + suffix),
                runtime_result=ref('result' + suffix), route_state_carry=ref('carry' + suffix),
                prepared=prep_ref, route_predecessor=previous_ref, active=0, unclaimed=0,
                assigned=1, ready=1, claimed=1, claimed_keys=['new' + suffix],
                all_claims_accounted=True, stop_reason=None, quarantine=None,
                initial_consecutive_failed_contexts=0, final_consecutive_failed_contexts=0)
            ready = [dict(measurement_key='new' + suffix, disposition='prepared')]
            prep = dict(source=b['input_adapter'], **{k: ref(k + suffix)
                        for k in ('ready', 'unavailable', 'assignments', 'pairs')})
            v.update({Path(binding_ref['path']).name: old_binding, Path(plan_ref['path']).name: plan,
                Path(entry['actual']['path']).name: actual, Path(prep_ref['path']).name: prep,
                'ready' + suffix: ready, 'unavailable' + suffix: [], 'assignments' + suffix: [{}],
                'result' + suffix: dict(stop_reason=None), 'carry' + suffix: dict(prior_routes='native states'),
                'runtime_peer': dict(accepted_derived_reader_union_runtime_source=True, source=source,
                                    fixed_runtime=b['fixed_runtime'], parent_runtime=b['parent_runtime'], findings=[]),
                'capture_peer': dict(accepted_finite_reader_priority_custody_preparation=True,
                                    source=ref('capture.py'), findings=[]),
                'closed_peer' + suffix: dict(accepted_finite_reader_route_closed_cut=True, actual=entry['actual'],
                    source_review=ref('capture_peer'), findings=[], **{k: actual[k] for k in ('source', 'plan',
                    'plan_review', 'reader_source', 'root', 'journal', 'runtime_result', 'prepared', 'route_predecessor')}),
                'binding_peer' + suffix: dict(accepted_derived_reader_runtime_binding=True, binding=binding_ref,
                    source=source, findings=[], **{k: old_binding[k] for k in ('parent_runtime', 'inputs', 'input_adapter')}),
                'plan_peer' + suffix: dict(accepted_derived_reader_plan=True, plan=plan_ref,
                    source=source, binding=binding_ref, inputs=prep_ref, findings=[]),
                'inputs_peer' + suffix: dict(accepted_frozen_derived_reader_inputs=True, preparation=prep_ref,
                    source=b['input_adapter'], findings=[], **{k: prep[k] for k in ('ready', 'unavailable', 'assignments', 'pairs')})})
            previous, previous_plan, previous_ref, previous_review = actual, plan, entry['actual'], entry['review']
        b.update(previous_plan=previous['plan'], previous_closed=previous_ref, previous_closed_review=previous_review)
        p.update(history_extension=extension, route_predecessor=previous_ref)
        p['pause_paths'] = [p['pause_file']] + [x for x in previous_plan['pause_paths'] if x != previous_plan['pause_file']]
        return b, p, v, c

    def check(self, b, p, v, c):
        return UnionRuntimeTests().run_plan(b, p, v, c)

    def test_one_and_two_ordered_completed_tails(self):
        for count in (1, 2):
            b, p, v, c = self.data(count)
            self.assertEqual(self.check(b, p, v, c), v['plan' + str(count - 1)])

    def test_empty_or_unknown_extension_is_refused(self):
        for change in ({'closed_tails': []}, {'extra': True}):
            b, p, v, c = self.data(); b['history_extension'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'union_extension'):
                self.check(b, p, v, c)

    def test_reordered_removed_or_duplicate_tail_is_refused(self):
        for mutation in ('reverse', 'remove', 'duplicate'):
            b, p, v, c = self.data(2); entries = b['history_extension']['closed_tails']
            if mutation == 'reverse': entries.reverse()
            elif mutation == 'remove': entries.pop(0)
            else: entries.append(copy.deepcopy(entries[-1]))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.check(b, p, v, c)

    def test_unaccepted_tail_or_changed_peer_identity_is_refused(self):
        for field, value in (('accepted_finite_reader_route_closed_cut', False), ('actual', ref('other')),
                             ('source', ref('other.py')), ('findings', ['unresolved'])):
            b, p, v, c = self.data(); v['closed_peer0'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'accepted_finite_reader_route_closed_cut'):
                self.check(b, p, v, c)

    def test_unaccepted_validator_or_runtime_source_is_refused(self):
        for key, flag in (('capture_peer', 'accepted_finite_reader_priority_custody_preparation'),
                          ('runtime_peer', 'accepted_derived_reader_union_runtime_source')):
            b, p, v, c = self.data(); v[key][flag] = False
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, flag):
                self.check(b, p, v, c)

    def test_incomplete_or_quarantined_or_stopped_tail_is_refused(self):
        for key, value in (('active', 1), ('active', False), ('unclaimed', 1), ('quarantine', {}),
                           ('stop_reason', 'pause'), ('final_consecutive_failed_contexts', 3)):
            b, p, v, c = self.data(); v['closed0'][key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'completed_post_union_tail'):
                self.check(b, p, v, c)

    def test_union_and_prior_prefix_cannot_be_replaced(self):
        for field, value in (('previous_closed', ref('different')),
                             ('previous_plan', ref('different_plan')),
                             ('history_extension', dict(schema='bad')),
                             ('fixed_runtime', ref('different_runtime'))):
            b, p, v, c = self.data(); v['binding0'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(b, p, v, c)

    def test_policy_pauses_and_intervening_history_stay_literal(self):
        for field, value in (('operational_policy', 'reset'), ('revocation_file', '/different'),
                             ('pause_paths', []), ('intervening_reader_cuts', [])):
            b, p, v, c = self.data(); v['plan0'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(b, p, v, c)

    def test_previous_tail_root_cannot_be_reused(self):
        b, p, v, c = self.data(); p['output'] = '/closed0'
        with self.assertRaisesRegex(ValueError, 'new_union_successor_output'):
            self.check(b, p, v, c)

    def history(self, b, v, *, occupied=(), validator=None, body=None):
        actual = v['closed0']; plan = v['plan0']
        events = [dict(carry=actual['route_state_carry'])]
        raw = json.dumps(events[0]).encode() + b'\n' if body is None else body
        validation = dict(claims=['new0'], unclaimed=[], quarantine=None, inherited=0, final_counter=0)
        check = validator or mock.Mock(return_value=validation)
        fixed = types.SimpleNamespace(ordered_rows=lambda a, r, m: r + m)
        def loaded(ref_):
            return fixed if ref_ == b['fixed_runtime'] else types.SimpleNamespace(validate_history=check)
        with mock.patch.object(u, 'checked', lambda r: v[Path(r['path']).name]), \
             mock.patch.object(u, 'raw', return_value=raw), mock.patch.object(u, 'module', loaded):
            result = u.post_union_history(b, [(b['history_extension']['closed_tails'][0], actual, plan)],
                                          {'prior': {'consecutive_failed_contexts': 0}}, occupied, {})
        return result, check

    def test_native_history_validator_receives_unchanged_saved_evidence(self):
        b, p, v, c = self.data()
        (states, occupied), check = self.history(b, v, occupied={'old-a'})
        self.assertEqual(occupied, {'old-a', 'new0'})
        self.assertEqual(states['completed_reader_chain']['retained_previous_history'],
                         {'prior': {'consecutive_failed_contexts': 0}})
        args = check.call_args.args
        self.assertIs(args[1], v['result0']); self.assertIs(args[2], v['plan0'])
        self.assertIs(args[5], v['carry0'])
        self.assertEqual(args[6], {'prior': {'consecutive_failed_contexts': 0}})

    def test_native_validator_refusal_is_not_translated_to_success(self):
        b, p, v, c = self.data()
        with self.assertRaisesRegex(ValueError, 'native_counter_changed'):
            self.history(b, v, validator=mock.Mock(side_effect=ValueError('native_counter_changed')))

    def test_existing_claim_cannot_be_replayed(self):
        b, p, v, c = self.data()
        with self.assertRaisesRegex(ValueError, 'post_union_native_state'):
            self.history(b, v, occupied={'new0'})

    def test_recorded_counter_and_carry_pin_are_not_replaced(self):
        b, p, v, c = self.data(); v['closed0']['initial_consecutive_failed_contexts'] = 1
        with self.assertRaisesRegex(ValueError, 'post_union_native_state'):
            self.history(b, v)
        b, p, v, c = self.data()
        with self.assertRaisesRegex(ValueError, 'post_union_carry_pin'):
            self.history(b, v, body=json.dumps(dict(carry=ref('other'))).encode()+b'\n')

    def test_nonzero_failure_counter_is_preserved(self):
        b, p, v, c = self.data(); v['closed0']['final_consecutive_failed_contexts'] = 2
        validator = mock.Mock(return_value=dict(claims=['new0'], unclaimed=[], quarantine=None,
                                                inherited=0, final_counter=2))
        (states, _), _ = self.history(b, v, validator=validator)
        self.assertEqual(states['completed_reader_chain']['consecutive_failed_contexts'], 2)

    def test_unaccepted_input_rows_are_refused_before_validator(self):
        b, p, v, c = self.data(); v['inputs_peer0']['accepted_frozen_derived_reader_inputs'] = False
        validator = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'accepted_frozen_derived_reader_inputs'):
            self.history(b, v, validator=validator)
        validator.assert_not_called()

    def test_native_live_gates_cover_both_old_and_new_owners_and_locks(self):
        b, p, v, c = self.data(); base = v['tail']; actual = v['closed0']
        for i, row in enumerate((base, actual)):
            row.update(actor_process=ref('actor' + str(i)), supervisor_intent=ref('supervisor' + str(i)),
                       after={'lock': dict(path='/fixture/lock' + str(i), device=4, inode=10+i)},
                       journal=ref('j' + str(i)), journal_original=ref('jo' + str(i)),
                       runtime_result=ref('r' + str(i)))
            who = dict(pid=50+i, start_ticks=100+i, argv=['reader'])
            v['actor' + str(i)] = dict(identity=who); v['supervisor' + str(i)] = dict(supervisor=who)
        c.stopped_gates = mock.Mock()
        h = types.SimpleNamespace(retirement_state=mock.Mock(return_value={'absent': True}),
                                  lock_free=mock.Mock(return_value=True))
        original_gates = mock.Mock()
        records = [(b['history_extension']['closed_tails'][0], actual, v['plan0'])]
        with mock.patch.object(u, 'verified_union', return_value=(c, v['union'], {}, v['prior_plan'], v['original_plan'], base)), \
             mock.patch.object(u, 'post_union_records', return_value=records), \
             mock.patch.object(u, 'checked', lambda r: v[Path(r['path']).name]), \
             mock.patch.object(u, 'raw', return_value=b'unchanged\n'), \
             mock.patch.object(Path, 'stat', lambda path: types.SimpleNamespace(st_dev=4, st_ino=10+int(path.name[-1]))):
            u.union_gates(b, p, {'campaign_handoff': h}, original_gates)
            self.assertEqual(h.retirement_state.call_count, 4)
            self.assertEqual(h.lock_free.call_count, 2)
            original_gates.assert_called_once(); c.stopped_gates.assert_called_once()
            h.lock_free.return_value = False
            with self.assertRaisesRegex(ValueError, 'tail_reader_lock_changed_or_occupied'):
                u.union_gates(b, p, {'campaign_handoff': h}, original_gates)
            h.lock_free.return_value = True; h.retirement_state.return_value = {'absent': False}
            with self.assertRaisesRegex(ValueError, 'tail_reader_process_present'):
                u.union_gates(b, p, {'campaign_handoff': h}, original_gates)

    def test_history_result_refs_include_original_stopped_and_every_tail(self):
        b, p, v, c = self.data(2)
        p.update(schema='causes.derived_reader_plan.v1', source=ref('candidate.py'), derived_binding=ref('future'), derived_binding_review=ref('future_peer'))
        v['future'] = b
        old_binding = dict(stopped_actual=ref('stopped'))
        v['stopped'] = dict(runtime_result=ref('stopped_result'))
        v['tail']['runtime_result'] = ref('base_result')
        records = [(entry, v['closed' + str(i)], v['plan' + str(i)])
                   for i, entry in enumerate(b['history_extension']['closed_tails'])]
        with mock.patch.object(u, 'pin', return_value=p['source']), \
             mock.patch.object(u, 'checked', lambda r: v[Path(r['path']).name]), \
             mock.patch.object(u, 'verified_union', return_value=(c, v['union'], old_binding,
                 v['prior_plan'], v['original_plan'], v['tail'])), \
             mock.patch.object(u, 'post_union_records', return_value=records), mock.patch.object(u, 'raw') as raw:
            expected = [ref('stopped_result'), ref('base_result'), ref('result0'), ref('result1')]
            self.assertEqual(u.history_result_refs(p), expected)
            self.assertEqual(raw.call_args_list, [mock.call(r) for r in expected])
            p['route_predecessor'] = ref('tail')
            with self.assertRaisesRegex(ValueError, 'union_result_history'):
                u.history_result_refs(p)

    def test_auditor_retains_all_rows_and_rejects_cross_tail_overlap(self):
        b, p, v, c = self.data(2)
        p.update(schema='causes.derived_reader_plan.v1', source=ref('candidate.py'), derived_binding=ref('future'), derived_binding_review=ref('future_peer'))
        v['future'] = b
        v['tail']['claimed_keys'] = ['base-completion']
        c.audit_previous_history = lambda *a: ([{'old': 'literal'}], {'stopped-claim'})
        records = [(entry, v['closed' + str(i)], v['plan' + str(i)])
                   for i, entry in enumerate(b['history_extension']['closed_tails'])]
        with mock.patch.object(u, 'pin', return_value=p['source']), \
             mock.patch.object(u, 'checked', lambda r: v[Path(r['path']).name]), \
             mock.patch.object(u, 'runtime_namespace'), \
             mock.patch.object(u, 'verified_union', return_value=(c, v['union'], {},
                 v['prior_plan'], v['original_plan'], v['tail'])), \
             mock.patch.object(u, 'post_union_records', return_value=records):
            rows, keys = u.audit_previous_history(p, None)
            self.assertEqual(rows, [{'old': 'literal'}, v['tail'], v['closed0'], v['closed1']])
            self.assertEqual(keys, {'stopped-claim', 'base-completion', 'new0', 'new1'})
            v['closed1']['claimed_keys'] = ['new0']
            with self.assertRaisesRegex(ValueError, 'post_union_audit_overlap'):
                u.audit_previous_history(p, None)


if __name__ == '__main__':
    unittest.main()
