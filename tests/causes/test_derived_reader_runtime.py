import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

from causes import campaign_reader as reader, measurement_view, reader_operational_classification


SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/derived_reader_runtime.py'
SPEC = importlib.util.spec_from_file_location('test_bound_derived_runtime', SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class DerivedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous_modules = set(sys.modules)

    def tearDown(self):
        for name in set(sys.modules) - self.previous_modules:
            if name.startswith('derived_reader_bound_'):
                del sys.modules[name]
        self.temp.cleanup()

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return runtime.pin(path)

    def rows(self):
        assignments = [dict(run_id='r' + str(i), view_kind='neutral', stage='early') for i in range(3)]
        rows = [dict(a, measurement_key='derived_reader_' + str(i),
                     disposition='unavailable' if i == 1 else 'prepared') for i, a in enumerate(assignments)]
        return assignments, [rows[0], rows[2]], [rows[1]]

    def test_complete_original_order_keeps_unavailable_arm(self):
        assignments, ready, missing = self.rows()
        self.assertEqual([r['run_id'] for r in runtime.ordered_rows(assignments, ready, missing)], ['r0', 'r1', 'r2'])

    def test_omitted_missing_duplicate_and_reordered_rows_refuse(self):
        a, r, m = self.rows()
        for args in ((a, r, []), (a, r + [r[0]], m), (a, list(reversed(r)), m), (a + [a[0]], r, m)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                runtime.ordered_rows(*args)

    def test_identity_and_disposition_changes_refuse(self):
        a, r, m = self.rows()
        for field, value in [('measurement_key', r[0]['measurement_key']), ('disposition', 'pending'),
                             ('measurement_key', 'fresh_candidate_old')]:
            changed = copy.deepcopy(m)
            changed[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                runtime.ordered_rows(a, r, changed)

    def history(self):
        fixed = dict(free_space_floor_bytes=100, env_file='credential-location')
        prior = dict(fixed, source={'source': 'old'}, output=str(self.root / 'old'),
                     intervening_reader_cuts=[{'actual': 'earlier', 'review': 'accepted'}],
                     operational_policy='unchanged', revocation_file='global',
                     pause_file=str(self.root / 'old/PAUSE'),
                     pause_paths=[str(self.root / 'old/PAUSE'), 'campaign-pause', 'global'])
        prior_ref = self.save('prior.json', prior)
        closed = dict(plan=prior_ref, root=prior['output'], reader_source=prior['source'],
                      journal={'journal': 'saved'}, runtime_result={'result': 'saved'},
                      all_claims_accounted=True, active=0, unclaimed=0, quarantine=None)
        closed_ref = self.save('closed.json', closed)
        peer = dict(accepted_finite_reader_route_closed_cut=True, findings=[], actual=closed_ref,
                    **{k: closed[k] for k in ['plan', 'reader_source', 'root', 'journal', 'runtime_result']})
        peer_ref = self.save('closed-review.json', peer)
        binding = dict(previous_plan=prior_ref, previous_closed=closed_ref,
                       previous_closed_review=peer_ref, fixed_plan_fields=fixed)
        plan = dict(prior, output=str(self.root / 'new'), route_predecessor=closed_ref,
                    intervening_reader_cuts=prior['intervening_reader_cuts'] + [dict(actual=closed_ref, review=peer_ref)],
                    pause_file=str(self.root / 'new/PAUSE'),
                    pause_paths=[str(self.root / 'new/PAUSE'), 'campaign-pause', 'global'])
        return binding, plan, dict(plan_fields=fixed)

    def test_complete_history_and_native_policy_preserved(self):
        binding, plan, known = self.history()
        self.assertEqual(runtime.validate_history(binding, plan, known)['output'], str(self.root / 'old'))
        for field, value in [('intervening_reader_cuts', plan['intervening_reader_cuts'][1:]),
                             ('pause_paths', [plan['pause_file']]), ('operational_policy', 'reset-counter'),
                             ('free_space_floor_bytes', 0), ('output', str(self.root / 'old'))]:
            changed = dict(plan, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                runtime.validate_history(binding, changed, known)

    def test_active_or_quarantined_predecessor_refuses(self):
        for field, value in [('active', 1), ('active', False), ('unclaimed', 1), ('quarantine', {})]:
            binding, plan, known = self.history()
            closed = runtime.checked(binding['previous_closed'])
            closed[field] = value
            ref = self.save('closed-altered.json', closed)
            review = runtime.checked(binding['previous_closed_review'])
            review['actual'] = ref
            binding.update(previous_closed=ref, previous_closed_review=self.save('review-altered.json', review))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                runtime.validate_history(binding, plan, known)

    def test_storage_binds_count_roots_and_native_free_floor(self):
        plan = dict(output=str(self.root / 'output'), expected_unclaimed_contexts=2,
                    workers=4, free_space_floor_bytes=100)
        grant = dict(root_authorized=True, runtime_root=plan['output'], runtime_operational_reserve_bytes=1000,
                     category_cap_bytes=2000, category_outstanding_reserved_bytes=10,
                     category_roots=[str(self.root)], minimum_free_bytes=100,
                     maximum_contexts=2, maximum_reader_requests=6, workers=4)
        ref = self.save('storage.json', grant)
        plan['storage_authority'] = ref
        self.assertEqual(runtime.validate_storage(dict(storage_authority=ref), plan), grant)
        for field, value in [('root_authorized', False), ('runtime_root', str(self.root / 'elsewhere')),
                             ('category_roots', [str(self.root / 'other')]), ('category_cap_bytes', True),
                             ('minimum_free_bytes', 1), ('maximum_contexts', 3), ('workers', True)]:
            changed_ref = self.save('storage-changed.json', dict(grant, **{field: value}))
            changed_plan = dict(plan, storage_authority=changed_ref)
            with self.subTest(field=field), self.assertRaises(ValueError):
                runtime.validate_storage(dict(storage_authority=changed_ref), changed_plan)

    def test_changed_pin_and_symlink_refuse(self):
        ref = self.save('value.json', {'a': 1})
        (self.root / 'link.json').symlink_to(ref['path'])
        with self.assertRaises(ValueError):
            runtime.pin(self.root / 'link.json')
        Path(ref['path']).write_text('{"a":2}')
        with self.assertRaises(ValueError):
            runtime.checked(ref)
        with self.assertRaises(ValueError):
            runtime.raw(dict(ref, bytes=True))

    def test_namespace_preserves_native_code_and_parent_globals(self):
        parent_path = self.root / 'parent.py'
        parent_path.write_text("KNOWN={'plan_fields': {}, 'modules': {}, 'materializer': 'old'}\n"
                               "def gates(*args): return 'native-gates'\n"
                               "def classify_secondary_result(*args): return ()\n"
                               "def run_finite(*args): return KNOWN['materializer']\n"
                               "def main(): raise RuntimeError('must not execute')\n")
        parent_ref = runtime.pin(parent_path)
        source_ref = runtime.pin(SOURCE)
        source_peer = self.save('source-peer.json', dict(accepted_derived_reader_runtime_source=True,
                               findings=[], source=source_ref, parent_runtime=parent_ref))
        binding = dict(schema='causes.derived_reader_runtime_binding.v1', source=source_ref,
                       parent_runtime=parent_ref, fixed_plan_fields={}, input_adapter={'test': 'adapter'},
                       inputs={'test': 'inputs'}, source_review=source_peer)
        binding_ref = self.save('binding.json', binding)
        review_ref = self.save('binding-review.json', dict(accepted_derived_reader_runtime_binding=True,
                   findings=[], binding=binding_ref, **{k: binding[k] for k in ['source', 'parent_runtime', 'inputs', 'input_adapter']}))
        bound = runtime.runtime_namespace(binding_ref, review_ref)
        parent = runtime.module(parent_ref)
        self.assertIs(bound['run_finite'].__code__, parent.run_finite.__code__)
        self.assertIs(bound['main'].__code__, parent.main.__code__)
        self.assertIsNot(bound['gates'].__code__, parent.gates.__code__)
        self.assertEqual(parent.KNOWN['materializer'], 'old')
        self.assertEqual(bound['KNOWN']['materializer'], binding['input_adapter'])
        self.assertEqual(bound['run_finite'](), binding['input_adapter'])
        self.assertEqual(parent.run_finite(), 'old')

    def classification_fixture(self):
        initial = [dict(role='system', content='Base.'), dict(role='user', content='Task.')]
        view = dict(messages=initial + [dict(role='assistant', content='Recorded answer.')],
                    initial_context=initial, prefix_context=None, phase='prefix')
        view_ref = self.save('view.json', view)
        context, _ = measurement_view.prepare_judge_context(view)
        adapter = self.root / 'adapter.py'
        adapter.write_text("import json\nfrom pathlib import Path\n"
                           "def read_view(ref): return json.loads(Path(ref['path']).read_bytes())\n")
        parent_path = self.root / 'classification-parent.py'
        parent_path.write_text("KNOWN={'plan_fields': {}, 'modules': {}, 'materializer': 'old'}\n"
            "def gates(*args): pass\n"
            "def classify_secondary_result(key, result, row, output, modules):\n"
            " item=dict(output,expected=dict(output['expected'],context_sha256=row['context_canonical_sha256']))\n"
            " return modules['reader_operational_classification'].citation_only_missing(result,{'strongly_favors':item})\n")
        parent_ref, source_ref = runtime.pin(parent_path), runtime.pin(SOURCE)
        source_peer = self.save('classification-source-peer.json',
            dict(accepted_derived_reader_runtime_source=True, findings=[], source=source_ref,
                 parent_runtime=parent_ref))
        binding = dict(schema='causes.derived_reader_runtime_binding.v1', source=source_ref,
            parent_runtime=parent_ref, fixed_plan_fields={}, input_adapter=runtime.pin(adapter),
            inputs={'test': 'inputs'}, source_review=source_peer)
        binding_ref = self.save('classification-binding.json', binding)
        peer_ref = self.save('classification-binding-peer.json',
            dict(accepted_derived_reader_runtime_binding=True, findings=[], binding=binding_ref,
                 **{k: binding[k] for k in ['source', 'parent_runtime', 'inputs', 'input_adapter']}))
        bound = runtime.runtime_namespace(binding_ref, peer_ref)
        row = dict(view=view_ref, context_canonical_sha256=reader.digest(context),
                   measurement_key='derived_reader_fixture', payloads={'fixed': 'identities'})
        expected = {k: 'a' * 64 for k in ('context_sha256', 'request_sha256', 'source_sha256',
                    'rubric_sha256', 'threshold_sha256', 'schema_sha256', 'config_sha256')}
        expected['context_sha256'] = context['context_sha256']
        attempt = dict(expected, instrument='strongly_favors', max_retries=0, store=False)
        receipt = dict(attempt, status='technical_missing', api_call_performed=True,
                       http_status=200, response_status='completed', response_sha256='b' * 64)
        evidence = dict(attempt=attempt, receipt=receipt, expected=expected,
                        actual_request_sha256=expected['request_sha256'], actual_response_sha256='b' * 64)
        result = dict(context_sha256=context['context_sha256'], instruments={
            i: dict(status='validated', instrument=i, context_sha256=context['context_sha256'])
            for i in reader.INSTRUMENTS})
        result['instruments']['strongly_favors'].update(status='technical_missing',
            technical_reason='ValueError', validation_error='evidence_not_exact_channel_substring:capability')
        modules = dict(campaign_reader=reader, measurement_view=measurement_view,
                       reader_operational_classification=reader_operational_classification)
        return bound, row, result, evidence, modules, adapter

    def test_citation_missing_uses_native_message_digest_without_changing_frozen_row(self):
        bound, row, result, evidence, modules, _ = self.classification_fixture()
        before = copy.deepcopy((row, result, evidence))
        self.assertNotEqual(row['context_canonical_sha256'], result['context_sha256'])
        self.assertEqual(bound['classify_secondary_result']('key', result, row, evidence, modules),
                         ('strongly_favors',))
        self.assertEqual((row, result, evidence), before)

    def test_changed_prepared_context_digest_refuses(self):
        bound, row, result, evidence, modules, _ = self.classification_fixture()
        row['context_canonical_sha256'] = result['context_sha256']
        with self.assertRaisesRegex(ValueError, 'derived_classification_context_changed'):
            bound['classify_secondary_result']('key', result, row, evidence, modules)

    def test_native_receipt_contradiction_still_refuses(self):
        bound, row, result, evidence, modules, _ = self.classification_fixture()
        evidence['receipt']['context_sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'reader_citation_evidence_mismatch'):
            bound['classify_secondary_result']('key', result, row, evidence, modules)

    def test_noncompleted_response_is_not_annotation_missing(self):
        bound, row, result, evidence, modules, _ = self.classification_fixture()
        evidence['receipt']['response_status'] = 'incomplete'
        self.assertEqual(bound['classify_secondary_result']('key', result, row, evidence, modules), ())

    def test_classification_rechecks_adapter_source_pin(self):
        bound, row, result, evidence, modules, adapter = self.classification_fixture()
        adapter.write_text(adapter.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'immutable_pin_changed'):
            bound['classify_secondary_result']('key', result, row, evidence, modules)


if __name__ == '__main__':
    unittest.main()
