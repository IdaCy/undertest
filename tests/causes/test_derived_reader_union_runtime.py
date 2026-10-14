import copy
import importlib.util
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


if __name__ == '__main__':
    unittest.main()
