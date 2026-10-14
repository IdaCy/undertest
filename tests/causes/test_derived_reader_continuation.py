import ast
import copy
import importlib.util
import json
import tempfile
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/derived_reader_continuation.py'
spec = importlib.util.spec_from_file_location('continuation_tests', SOURCE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ContinuationTests(unittest.TestCase):
    def data(self):
        ready = [dict(measurement_key='derived_reader_' + str(i)) for i in range(4)]
        ids = c.keys(ready)
        q = dict(quarantine_id='original-q', key=ids[1], reason='unresolved_current_reader_attempt')
        prior = dict(output='/old', source={'old': 'source'}, prepared={'inputs': 'frozen'},
                     route_predecessor={'older': 'cut'})
        events = [dict(event='reader_route_state_carried', consecutive_failed_contexts=0)]
        for key in ids[:3]:
            events += [dict(event='claimed', key=key)]
        events += [dict(event='finished', key=ids[2]),
                   dict(event='unresolved_current_reader_attempt', key=ids[1]),
                   dict(event='finished', key=ids[0]),
                   dict(event='watcher_paused', new_admissions_stopped=True)]
        cut = dict(claimed=3, active=0, unclaimed=1, ready=4, all_claims_accounted=True,
                   final_for_reader_analysis=False, root='/old', reader_source=prior['source'],
                   prepared=prior['prepared'], route_predecessor=prior['route_predecessor'],
                   claimed_keys=ids[:3], unclaimed_keys=ids[3:], quarantine=q,
                   stop_reason='reader_route_quarantined', final_consecutive_failed_contexts=0)
        result = dict(claimed=ids[:3], unclaimed=ids[3:], finished_or_unresolved=[ids[2], ids[1], ids[0]],
                      ready=4, prepared=prior['prepared'], route_predecessor=prior['route_predecessor'],
                      quarantine=q, stop_reason='reader_route_quarantined', consecutive_failed_contexts=0)
        return prior, cut, result, events, ready

    def test_original_attempts_and_quarantine_are_retained(self):
        values = self.data(); saved = copy.deepcopy(values)
        occupied, suffix, quarantine = c.stopped_partition(*values)
        self.assertEqual(occupied, ['derived_reader_0', 'derived_reader_1', 'derived_reader_2'])
        self.assertEqual(suffix, ['derived_reader_3'])
        self.assertEqual(quarantine['key'], 'derived_reader_1')
        self.assertEqual(values, saved)

    def test_claimed_and_unresolved_cannot_reenter_suffix(self):
        for change in ('reorder', 'duplicate', 'drop', 'replay'):
            prior, cut, result, events, ready = self.data()
            if change == 'reorder':
                ready[0], ready[3] = ready[3], ready[0]
            elif change == 'duplicate':
                ready.append(copy.deepcopy(ready[0]))
            elif change == 'drop':
                ready.pop()
            else:
                result['unclaimed'] = cut['unclaimed_keys'] = ['derived_reader_1']
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.stopped_partition(prior, cut, result, events, ready)

    def test_exact_integer_counts_and_stopped_status(self):
        for field, value in [('active', False), ('active', 1), ('claimed', True), ('unclaimed', 0),
                             ('unclaimed', -1), ('ready', True), ('final_for_reader_analysis', True),
                             ('all_claims_accounted', False), ('final_consecutive_failed_contexts', True)]:
            args = list(self.data()); args[1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                c.stopped_partition(*args)

    def test_changed_quarantine_or_missing_terminal_refuses(self):
        for change in ('quarantine', 'unresolved', 'terminal', 'prepared', 'event'):
            args = list(self.data())
            if change == 'quarantine':
                args[1]['quarantine']['reason'] = 'route_failure'
            elif change == 'unresolved':
                args[3][5]['event'] = 'finished'
            elif change == 'terminal':
                args[3].pop(4)
            elif change == 'prepared':
                args[2]['prepared'] = {'changed': 'input'}
            else:
                args[3][-1]['event'] = 'finite_selection_complete'
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.stopped_partition(*args)

    def resolution(self):
        prior, cut, result, events, ready = self.data()
        cut.update(runtime_result={'result': 'pin'}, journal_original={'journal': 'pin'})
        binding = dict(source={'new': 'runtime'}, fixed_runtime={'fixed': 'runtime'},
            parent_runtime={'native': 'runtime'}, input_adapter={'adapter': 'source'},
            previous_plan={'previous': 'plan'}, stopped_actual={'stopped': 'actual'},
            stopped_review={'stopped': 'peer'}, fixed_runtime_review={'fixed': 'peer'},
            qualification={'qualification': 'pin'})
        qualification = dict(schema='causes.derived_reader_context_fix_saved_qualification.v1',
            source=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'],
            derived_input_adapter=binding['input_adapter'], plan=binding['previous_plan'],
            native_result=cut['runtime_result'], journal=cut['journal_original'], key=cut['quarantine']['key'],
            classification='completed_citation_invalid_annotation_missing', affected_instruments=['strongly_favors'],
            expected_counter_action='preserve', all_original_records_unchanged=True,
            original_row_and_result_unchanged=True, original_unresolved_and_quarantine_retained=True,
            scientific_missingness_changed=False, model_calls=0, host_calls=0, parser_calls=0, route_resolutions_written=0)
        plan = dict(output='/new')
        suffix = cut['unclaimed_keys']
        resolution = dict(schema='causes.derived_reader_new_route_resolution.v1', status='qualified',
            root_authorized=True, new_route_only=True, original_route_mutation_authorized=False,
            model_calls_authorized=False, stopped_actual=binding['stopped_actual'],
            stopped_review=binding['stopped_review'], previous_plan=binding['previous_plan'],
            source=binding['source'], fixed_runtime=binding['fixed_runtime'],
            fixed_runtime_review=binding['fixed_runtime_review'], qualification=binding['qualification'],
            quarantine=cut['quarantine'], quarantine_id=cut['quarantine']['quarantine_id'], output=plan['output'],
            unclaimed_keys=suffix, consecutive_failed_contexts=0, counter_action='preserve', new_quarantine=None,
            at='saved-fixture', root_author='fixture')
        return binding, plan, cut, qualification, resolution, suffix

    def test_only_explicit_new_route_resolution_accepts(self):
        args = self.resolution(); before = copy.deepcopy(args)
        c.validate_resolution(*args)
        self.assertEqual(args, before)
        for field, value in [('root_authorized', False), ('new_route_only', False),
                             ('original_route_mutation_authorized', True), ('model_calls_authorized', True),
                             ('consecutive_failed_contexts', False), ('consecutive_failed_contexts', 1),
                             ('quarantine_id', 'different'), ('output', '/old'), ('unclaimed_keys', []),
                             ('new_quarantine', {'unexpected': 'quarantine'})]:
            args = list(self.resolution()); args[4][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_resolution(*args)

    def test_unqualified_evidence_or_scientific_relabel_refuses(self):
        for field, value in [('source', {'different': 'source'}), ('native_result', {'different': 'result'}),
                             ('key', 'unrelated'), ('classification', 'validated'), ('affected_instruments', []),
                             ('affected_instruments', ['unknown']), ('expected_counter_action', 'reset'),
                             ('scientific_missingness_changed', True), ('parser_calls', 1),
                             ('route_resolutions_written', 1), ('model_calls', False),
                             ('original_unresolved_and_quarantine_retained', False)]:
            args = list(self.resolution()); args[3][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_resolution(*args)

    def test_context_classifier_is_exact_accepted_fix(self):
        original = ast.parse((SOURCE.parent / 'derived_reader_runtime.py').read_text())
        successor = ast.parse(SOURCE.read_text())
        def fn(tree, name):
            return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        for name in ('classify_secondary_result', 'raw', 'module', 'accepted', 'pin', 'checked'):
            self.assertEqual(ast.dump(fn(original, name)), ast.dump(fn(successor, name)), name)

    def test_complete_union_requires_every_old_and_new_key(self):
        _, old, _, _, ready = self.data()
        tail = dict(claimed_keys=old['unclaimed_keys'], active=0, unclaimed=0, ready=1, claimed=1,
                    all_claims_accounted=True, quarantine=None, stop_reason=None, unclaimed_keys=[], root='/new')
        result = dict(claimed=tail['claimed_keys'], unclaimed=[], finished_or_unresolved=tail['claimed_keys'],
                      stop_reason=None, quarantine=None)
        qual = self.resolution()[3]
        union = c.validate_union(ready, old, tail, result, qual)
        self.assertEqual(union['roots'], ['/old', '/old', '/old', '/new'])
        self.assertEqual(union['original_missing_instruments'], ['strongly_favors'])
        for field, value in [('claimed_keys', ['derived_reader_1']), ('active', True), ('unclaimed', 1),
                             ('quarantine', {}), ('stop_reason', 'paused'), ('all_claims_accounted', False)]:
            changed = dict(tail, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                c.validate_union(ready, old, changed, result, qual)


    def test_union_rehashes_original_journal_and_result(self):
        prior, cut, result, events, ready = self.data()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def save(name, value):
                path = root / name
                path.write_text(json.dumps(value))
                return c.pin(path)
            plan_ref = save('plan.json', prior)
            cut['plan'] = plan_ref
            cut['runtime_result'] = save('result.json', result)
            journal = b''.join(json.dumps(row).encode() + b'\n' for row in events)
            original = root / 'original.jsonl'; original.write_bytes(journal)
            saved = root / 'closed.jsonl'; saved.write_bytes(journal)
            cut.update(journal=c.pin(saved), journal_original=c.pin(original))
            binding = dict(previous_plan=plan_ref)
            self.assertEqual(c.recheck_stopped_original(binding, cut, ready)[1], ['derived_reader_3'])
            original.write_bytes(journal + b'{}\n')
            with self.assertRaisesRegex(ValueError, 'immutable_pin_changed'):
                c.recheck_stopped_original(binding, cut, ready)
            original.write_bytes(journal)
            (root / 'result.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'immutable_pin_changed'):
                c.recheck_stopped_original(binding, cut, ready)


if __name__ == '__main__':
    unittest.main()
