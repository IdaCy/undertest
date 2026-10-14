import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes import saved_original_registry as m


class Fixture:
    """Explicitly synthetic accepted records; never used as real authority."""
    def __init__(self, root):
        self.root = root
        self.runs = ['baseline', 'treatment', 'missing']
        self.config = dict(model='provider/model', endpoint='http://first', questions=[
            dict(id='evaluation_' + n, threshold=n, awareness_type='evaluation', text='Question ' + n)
            for n in m.THRESHOLDS])
        self.reader_config = {'model': 'reader', 'temperature': 0}
        self.contexts = {r: m.digest([{'role': 'user', 'content': r}]) for r in self.runs[:2]}
        self.allocation = {r: dict(run_id=r, canonical_variant='B00L00' if r == 'baseline' else 'F01L01',
            seed=4, repetition=0, instruments=['evaluation_' + n for n in
                (m.THRESHOLDS if r == 'baseline' else ['presence'])],
            reader_stages=['early'] if r == 'baseline' else [],
            reader_thresholds=list(m.THRESHOLDS)) for r in self.runs}
        self.native = {r: dict(run_id=r, model='campaign', variant_id=self.allocation[r]['canonical_variant'],
            allocation=self.allocation[r], source=str(root / 'snapshots' / r), summary_sha256=m.digest(r),
            context_sources={} if r == 'missing' else {'early': dict(path=str(root / r / 'early.json'),
                bytes_sha256=m.digest(['bytes', r]), context_sha256=self.contexts[r])},
            self={'early': {'evaluation_' + n: dict(label=None if r == 'missing' else 'no',
                technical_status='missing_or_mismatched_context' if r == 'missing' else 'complete',
                reason=None, **({} if r == 'missing' else {'context_sha256': self.contexts[r]}))
                for n in (m.THRESHOLDS if r == 'baseline' else ['presence'])}}, reader={}) for r in self.runs}
        self.prepared = dict(messages=[dict(role='user', content='baseline', message_index=0)],
            context_sha256=self.contexts['baseline'], observation=dict(phase='prefix'))
        self.result = dict(schema='caus.reader_context.v1', context_sha256=self.contexts['baseline'],
            observation=self.prepared['observation'], independent_instrument_calls=True,
            threshold_results_inferred_from_other_calls=False, instruments={n: dict(instrument=n,
                context_sha256=self.contexts['baseline'], status='validated',
                judgments={'evaluation': {'admissible_label': 'no'}}) for n in m.THRESHOLDS})
        self.proof = dict(schema='causes.neutral_summary_pair_derivation.v1', arms={
            arm: dict(contexts={'messages': dict(original_sha256=self.contexts[r], exact_inverse_verified=True)})
            for arm, r in [('baseline', 'baseline'), ('treatment', 'treatment')]})
        def arm(r):
            return dict(original_run_id=r, direct_key='key_' + r,
                input_disposition='unavailable' if r == 'missing' else 'prepared', neutral_view='neutral')
        self.membership = dict(comparisons=[dict(stage='early', members=[
            dict(task_id='task1', draw_key=0, disposition='included', arms=dict(baseline=arm('baseline'), treatment=arm('treatment'))),
            dict(task_id='task2', draw_key=0, disposition='unsupported', reason='frozen unsupported')]),
            dict(stage='early', members=[dict(task_id='task3', draw_key=0, disposition='included',
                arms=dict(baseline=arm('baseline'), treatment=arm('missing')))])])
        self.transforms = {}

    def write(self, name, value, raw=False):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        b = value if raw else m.encoded(value)
        p.write_bytes(b)
        return dict(path=str(p), bytes=len(b), sha256=hashlib.sha256(b).hexdigest())

    def change(self, name, value):
        callback = self.transforms.get(name)
        if callback:
            callback(value)
        return value

    def freeze(self):
        runner = self.write('runner.py', b'# synthetic runner\n', raw=True)
        reader = self.write('reader.py', b'# synthetic reader\n', raw=True)
        config = self.write('config.json', self.config)
        reader_config = self.write('reader_config.json', self.reader_config)
        prompts = {n: self.write('prompts/' + n, ('prompt ' + n).encode(), raw=True)
                   for n in ['rubric', *m.THRESHOLDS]}
        protocols = {n: dict(schema='causes.saved_reader_instrument_protocol.v1',
            reader_source_sha256=reader['sha256'], config_sha256=m.digest(self.reader_config),
            rubric_sha256=prompts['rubric']['sha256'], threshold_sha256=prompts[n]['sha256'],
            schema_sha256=m.digest(['schema', n]), instrument=n, phase='prefix', awareness_type='evaluation')
            for n in m.THRESHOLDS}
        protocol = dict(schema='causes.original_saved_instrument_binding.v1', direct_config=config,
            runner_source=runner, reader_config=reader_config, reader_source=reader, reader_prompts=prompts,
            self={n: m.direct_instrument_protocol(self.config, runner, q) for n, q in zip(m.THRESHOLDS, self.config['questions'])},
            reader=protocols)
        protocol['assembly_binding'] = self.write('assembly_binding.json', self.change('assembly',
            dict(model='campaign', model_config=config, required_pins=[runner], reader_config=reader_config)))
        self.change('protocol', protocol)
        complete = self.write('COMPLETE.json', self.change('complete', dict(schema='causes.reader_artifact_freeze_complete.v1',
            provenance=dict(namespace='original', reader_pins=dict(reader_source_sha256=reader['sha256'],
                reader_config_sha256=reader_config['sha256'], prompts={k: v['sha256'] for k, v in prompts.items()})))))
        result = self.write('reader/baseline/RESULT.json', self.result)
        context = self.write('reader/baseline/context.json', self.prepared)
        self.native['baseline']['reader']['early'] = dict(path=result['path'], result_sha256=result['sha256'],
            instruments={n: dict(status=row['status'], labels={'evaluation':
                ((row.get('judgments') or {}).get('evaluation') or {}).get('admissible_label')
                if row['status'] in ('validated', 'validated_partial_observation') else None})
                for n, row in self.result['instruments'].items()})
        self.change('native', self.native)
        selected_rows = []
        for r, native in self.native.items():
            assignments = [dict(method=method, threshold=n, disposition='assigned' if
                (('evaluation_' + n in native['allocation']['instruments']) if method == 'self'
                 else 'early' in native['allocation']['reader_stages']) else 'not_assigned')
                for method in ['self', 'reader'] for n in m.THRESHOLDS]
            selected_rows.append(dict(run_id=r, native=native, assignments=assignments,
                provenance=dict(payload={'path': '/synthetic/accepted.jsonl', 'bytes': 1, 'sha256': 'a'*64},
                    row_index=len(selected_rows), byte_offset=len(selected_rows), line_bytes=1,
                    line_sha256=m.digest(r), native_canonical_sha256=m.digest(native))))
        selected_value = dict(schema='causes.saved_original_fixed_trajectory_subset.v1', selected=selected_rows,
            native_rows_unchanged=True, assignment_counts={name: sum(s['disposition'] == name
                for r in selected_rows for s in r['assignments']) for name in ['assigned', 'not_assigned']})
        self.change('selected', selected_value)
        data = self.write('selected.json', selected_value)
        actual = self.write('subset.json', dict(data=data))
        peer = self.write('subset_peer.json', self.change('subset_peer', dict(actual=actual, data=data,
            accepted_saved_original_fixed_trajectory_subset=True, findings=[])))
        subset = dict(actual=actual, review=peer)
        members = self.write('membership.json', self.membership)
        membership_peer = self.write('membership_peer.json', dict(actual=members,
            accepted_frozen_neutral_matched_membership=True, findings=[]))
        membership = dict(actual=members, review=membership_peer)
        pins, indexes = [], {}
        def add(ref):
            if ref['path'] not in indexes:
                indexes[ref['path']] = len(pins)
                pins.append(ref)
            return indexes[ref['path']]
        proof = self.write('proof.json', self.proof)
        contexts = []
        for r in self.runs:
            row = dict(run_id=r, measurement_key='key_' + r, view_kind='neutral',
                       disposition='unavailable' if r == 'missing' else 'prepared')
            if r == 'missing':
                row['reason'] = 'no original early context'
            else:
                c = copy.deepcopy(self.config)
                c['endpoint'] = 'http://harmless-other-endpoint'
                self.change('origin_config', c)
                row.update(allocation=add(self.write(r + '/allocation.json', self.allocation[r])),
                    config=add(self.write(r + '/config.json', c)), proof=add(proof),
                    proof_selector=['arms', 'baseline' if r == 'baseline' else 'treatment', 'contexts', 'messages'],
                    original_context=self.native[r]['context_sources'].get('early'))
            contexts.append(row)
        inventory = dict(schema='causes.original_saved_registry_reached_metadata.v1', selected=data,
            membership=members, pins=pins, contexts=contexts, readers=[dict(run_id='baseline', namespace='original',
                result=add(result), context=add(context), context_source={**context, 'path': '/synthetic/original/context.json'},
                complete=complete, native_context_sha256=self.contexts['baseline'])])
        self.change('inventory', inventory)
        inv = self.write('inventory.json', inventory)
        binding_value = dict(schema='causes.original_saved_registry_binding.v1', subset=subset,
                             membership=membership, inventory=inv, protocol=protocol)
        binding = self.write('binding.json', self.change('binding', binding_value))
        binding_peer = self.write('binding_peer.json', self.change('binding_peer', dict(actual=binding,
            accepted_original_saved_registry_bindings=True, subset=subset, membership=membership,
            inventory=inv, findings=[])))
        return subset, membership, dict(actual=binding, review=binding_peer)


class OriginalRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    def resolve(self, **limits):
        return m.resolve_saved_originals(*self.f.freeze(), **limits)

    def refused(self, message):
        with self.assertRaisesRegex(ValueError, message):
            self.resolve()

    def test_full_ledger_shared_originals_and_domains(self):
        out = self.resolve()
        self.assertEqual(out['counts'], dict(assigned=8, not_assigned=10, present=8, absent_instances=0))
        self.assertEqual(len(out['assignments']), 18)
        self.assertEqual(len(out['comparisons']), 2)
        self.assertEqual(out['comparisons'][0]['members'][1]['disposition'], 'unsupported')
        self.assertTrue(all(x['measurement_id'] is None for x in out['assignments'] if x['disposition'] == 'not_assigned'))
        reader = next(x for x in out['measurements'] if x['method'] == 'reader')
        self.assertEqual(reader['context_sha256'], m.digest(self.f.prepared))
        self.assertNotEqual(reader['context_sha256'], self.f.contexts['baseline'])
        self.assertEqual(out['input_unique_bytes'], out['input_finish_recheck_bytes'])
        self.assertFalse(out['four_cell_analysis_authorized'])

    def test_present_unavailable_record_is_not_request(self):
        out = self.resolve()
        row = next(x for x in out['measurements'] if x['original_run_id'] == 'missing')
        self.assertEqual((row['status'], row['context_sha256'], row['label']), ('unavailable', None, None))
        provenance = next(x for x in out['provenance'] if x['id'] == row['id'])
        self.assertEqual(provenance['physical']['kind'], 'native_snapshot_measurement')

    def test_assigned_absent_record_has_no_invented_id(self):
        del self.f.native['treatment']['self']['early']['evaluation_presence']
        out = self.resolve()
        slot = next(x for x in out['assignments'] if x['original_run_id'] == 'treatment' and x['threshold'] == 'presence' and x['method'] == 'self')
        self.assertIsNone(slot['measurement_id'])
        self.assertEqual(slot['record_state'], 'absent_instance')

    def test_single_arm_proof(self):
        proof = self.f.proof
        # One paired proof remains for baseline; treatment's indexed proof changes.
        def change(inv):
            single = dict(schema='causes.neutral_summary_single_arm_derivation.v1', arm='treatment',
                counterpart_context_supplied=False, derivation=proof['arms']['treatment'])
            inv['pins'].append(self.f.write('single.json', single))
            inv['contexts'][1]['proof'] = len(inv['pins']) - 1
            inv['contexts'][1]['proof_selector'] = ['derivation', 'contexts', 'messages']
        self.f.transforms['inventory'] = change
        self.assertEqual(self.resolve()['counts']['present'], 8)

    def test_all_native_categories_preserved(self):
        for label in m.SELF_LABELS:
            self.f.native['treatment']['self']['early']['evaluation_presence']['label'] = label
            out = self.resolve()
            self.assertEqual(next(x for x in out['measurements'] if x['original_run_id'] == 'treatment')['label'], label)
        for label in m.READER_LABELS:
            self.f.result['instruments']['presence']['judgments']['evaluation']['admissible_label'] = label
            out = self.resolve()
            self.assertEqual(next(x for x in out['measurements'] if x['method'] == 'reader' and x['threshold'] == 'presence')['label'], label)

    def wrapped_single(self, change=None, selector=None):
        def wrap(inv):
            entry = inv['contexts'][1]
            original = entry['original_context']
            proof = dict(schema='causes.neutral_summary_single_arm_derivation.v1',
                arm='treatment', counterpart_context_supplied=False,
                derivation=copy.deepcopy(self.f.proof['arms']['treatment']))
            envelope = dict(arm='treatment', run_id=entry['run_id'], proof=proof,
                baseline_model=copy.deepcopy(inv['pins'][entry['config']]),
                construction={'synthetic': True}, sample_adoption={'synthetic': True},
                single_arm_adoption={'synthetic': True}, summary={'synthetic': True},
                original_files={'allocation.json': copy.deepcopy(inv['pins'][entry['allocation']]),
                    'model.json': copy.deepcopy(inv['pins'][entry['config']]),
                    'early_context.json': dict(path=original['path'], bytes=123,
                                              sha256=original['bytes_sha256'])})
            if change:
                change(envelope)
            inv['pins'].append(self.f.write('wrapped_single.json', envelope))
            entry['proof'] = len(inv['pins']) - 1
            entry['proof_selector'] = selector or ['derivation', 'contexts', 'messages']
        self.f.transforms['inventory'] = wrap

    def test_wrapped_single_preserves_measurements_and_assignments(self):
        before = self.resolve()
        self.wrapped_single()
        after = self.resolve()
        for field in ['measurements', 'assignments', 'protocols', 'counts']:
            self.assertEqual(before[field], after[field])
        self.assertNotIn(self.f.native['treatment']['context_sources']['early']['path'],
                         [p['path'] for p in after['input_pins']])

    def test_membership_wrapped_single_preserves_measurements_and_assignments(self):
        before = self.resolve()
        def alternate(envelope):
            envelope.pop('sample_adoption')
            envelope.update(candidate={'synthetic': True}, membership={'synthetic': True})
        self.wrapped_single(alternate)
        after = self.resolve()
        for field in ['measurements', 'assignments', 'protocols', 'counts']:
            self.assertEqual(before[field], after[field])

    def test_wrapped_single_rosters_cannot_mix_or_omit_provenance_fields(self):
        for change in [lambda x: x.update(candidate={'synthetic': True}, membership={'synthetic': True}),
                       lambda x: x.pop('sample_adoption'),
                       lambda x: (x.pop('sample_adoption'), x.update(candidate={'synthetic': True}))]:
            self.wrapped_single(change)
            self.refused('single_proof_envelope_shape')

    def test_wrapped_single_outer_identity_refused(self):
        for change in [lambda x: x.update(run_id='another_run'),
                       lambda x: x.update(arm='baseline')]:
            self.wrapped_single(change)
            self.refused('single_proof_envelope_identity')

    def test_wrapped_single_allocation_and_config_pins_refused(self):
        for name in ['allocation.json', 'model.json']:
            self.wrapped_single(lambda x, n=name: x['original_files'][n].update(sha256='0'*64))
            self.refused('single_proof_envelope_sources')

    def test_wrapped_single_original_context_pin_refused(self):
        for field, value in [('path', '/synthetic/another_context.json'), ('sha256', '0'*64)]:
            self.wrapped_single(lambda x, k=field, v=value: x['original_files']['early_context.json'].update({k: v}))
            self.refused('single_proof_envelope_context')
        self.wrapped_single(lambda x: x['original_files']['early_context.json'].update(bytes=-1))
        self.refused('invalid_byte_count')

    def test_wrapped_single_shape_and_inner_schema_refused(self):
        self.wrapped_single(lambda x: x.update(unrecognized=True))
        self.refused('single_proof_envelope_shape')
        self.wrapped_single(lambda x: x['proof'].update(schema='causes.neutral_summary_pair_derivation.v1'))
        self.refused('single_proof_envelope_schema')

    def test_wrapped_single_inner_arm_and_counterpart_refused(self):
        for change in [lambda x: x['proof'].update(arm='baseline'),
                       lambda x: x['proof'].update(counterpart_context_supplied=True)]:
            self.wrapped_single(change)
            self.refused('single_proof_selector')

    def test_wrapped_single_selector_stays_relative_to_inner_proof(self):
        self.wrapped_single(selector=['proof', 'derivation', 'contexts', 'messages'])
        self.refused('single_proof_selector')

    def test_wrapped_single_message_hash_and_inverse_refused(self):
        for field, value in [('original_sha256', '0'*64), ('exact_inverse_verified', False)]:
            self.wrapped_single(lambda x, k=field, v=value:
                x['proof']['derivation']['contexts']['messages'].update({k: v}))
            self.refused('original_context_proof')

    def test_native_technical_reason_preserved_without_parser(self):
        self.f.result['instruments']['presence'].update(status='technical_missing', judgments=None,
            technical_reason='ValueError', validation_error='bad citation')
        out = self.resolve()
        row = next(x for x in out['measurements'] if x['method'] == 'reader' and x['threshold'] == 'presence')
        self.assertEqual(row['status'], 'technical_missing')
        p = next(x for x in out['provenance'] if x['id'] == row['id'])
        self.assertEqual(p['native_record']['validation_error'], 'bad citation')

    def test_wrong_proof_hash_and_inverse_refused(self):
        self.f.proof['arms']['baseline']['contexts']['messages']['original_sha256'] = '0'*64
        self.refused('original_context_proof')
        self.f.proof['arms']['baseline']['contexts']['messages']['original_sha256'] = self.f.contexts['baseline']
        self.f.proof['arms']['baseline']['contexts']['messages']['exact_inverse_verified'] = False
        self.refused('original_context_proof')

    def test_wrong_arm_refused(self):
        self.f.transforms['inventory'] = lambda x: x['contexts'][0].update(proof_selector=['arms', 'treatment', 'contexts', 'messages'])
        self.refused('proof_arm')

    def test_nonendpoint_protocol_change_refused(self):
        self.f.transforms['origin_config'] = lambda x: x.update(temperature=0.2)
        self.refused('origin_protocol_changed')

    def test_original_assembly_sources_required(self):
        self.f.transforms['assembly'] = lambda x: x.update(required_pins=[])
        self.refused('assembly_protocol_sources')

    def test_namespace_and_config_model_are_distinct_bound_identities(self):
        out = self.resolve()
        self.assertEqual(out['protocols']['self']['presence']['model'], 'provider/model')
        self.assertEqual(self.f.native['baseline']['model'], 'campaign')
        self.f.native['baseline']['model'] = 'provider/model'
        self.refused('native_model_namespace')

    def test_missing_or_wrong_assembly_namespace_refused(self):
        self.f.transforms['assembly'] = lambda x: x.pop('model')
        self.refused('assembly_protocol_sources')
        self.f.transforms['assembly'] = lambda x: x.update(model='another_campaign')
        self.refused('native_model_namespace')

    def test_reader_source_config_prompt_change_refused(self):
        for key, value in [('reader_source_sha256', '0'*64), ('reader_config_sha256', '0'*64), ('prompts', {})]:
            self.f.transforms['complete'] = lambda x, k=key, v=value: x['provenance']['reader_pins'].update({k:v})
            self.refused('native_reader_protocol')

    def test_reader_domain_and_phase_refused(self):
        self.f.prepared['context_sha256'] = m.digest(self.f.prepared)
        self.refused('reader_context_domain')
        self.f.prepared['context_sha256'] = self.f.contexts['baseline']
        self.f.prepared['observation']['phase'] = 'full'
        self.refused('reader_saved_shape')

    def test_unavailable_cannot_acquire_context(self):
        self.f.transforms['inventory'] = lambda x: x['contexts'][2].update(proof=0)
        self.refused('unavailable_context_identity')

    def test_unassigned_native_record_refused(self):
        self.f.native['treatment']['self']['early']['evaluation_strongly_favors'] = dict(label=None)
        self.refused('unassigned_has_measurement')

    def test_duplicate_and_missing_native_rows_refused(self):
        self.f.transforms['selected'] = lambda x: x['selected'].append(copy.deepcopy(x['selected'][0]))
        self.refused('native_run_identity')
        self.f.transforms['selected'] = lambda x: x['selected'].pop()
        self.refused('complete_native_membership')

    def test_duplicate_or_missing_context_refused(self):
        self.f.transforms['inventory'] = lambda x: x['contexts'].append(copy.deepcopy(x['contexts'][0]))
        self.refused('inventory_context_membership')
        self.f.transforms['inventory'] = lambda x: x['contexts'].pop()
        self.refused('complete_context_membership')

    def test_assignment_change_refused(self):
        self.f.transforms['selected'] = lambda x: x['selected'][1]['assignments'][1].update(disposition='assigned')
        self.refused('native_assignment_join')

    def test_unaccepted_or_misjoined_reviews_refused(self):
        self.f.transforms['binding_peer'] = lambda x: x.update(findings=['unresolved'])
        self.refused('accepted_peer')
        self.f.transforms['binding_peer'] = lambda x: x.update(inventory={})
        self.refused('inventory_binding')

    def test_missing_reader_and_wrong_result_path_refused(self):
        self.f.transforms['inventory'] = lambda x: x['readers'].clear()
        self.refused('complete_reader_inventory')
        self.f.transforms['inventory'] = lambda x: x['readers'][0].update(result=x['readers'][0]['context'])
        self.refused('reader_physical_identity')

    def test_reader_label_contradiction_refused(self):
        self.f.transforms['native'] = lambda x: x['baseline']['reader']['early']['instruments']['presence']['labels'].update(evaluation='yes')
        self.refused('accepted_reader_label_join')

    def test_no_observed_missing_context(self):
        self.f.native['missing']['self']['early']['evaluation_presence'].update(label='yes', technical_status='complete')
        self.refused('observed_without_context')

    def test_end_membership_refused(self):
        self.f.membership['comparisons'][0]['stage'] = 'end'
        self.refused('early_membership')

    def test_bounds(self):
        for limits, reason in [(dict(maximum_document_bytes=20), 'invalid_byte_count'),
                               (dict(maximum_total_bytes=100), 'total_input_bound'),
                               (dict(maximum_result_bytes=100), 'serialized_output_bound')]:
            with self.assertRaisesRegex(ValueError, reason):
                self.resolve(**limits)

    def test_strict_json_overflow_duplicate_and_finite_exponent(self):
        for body in [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}']:
            ref = self.f.write('invalid.json', body, raw=True)
            with self.assertRaises(ValueError):
                m._Read(100, 100).get(ref)
        ref = self.f.write('finite.json', b'{"x":1e-3}', raw=True)
        self.assertEqual(m._Read(100, 100).get(ref), {'x': 0.001})

    def test_finish_rechecks_saved_bytes(self):
        args = self.f.freeze()
        original = m._Read.finish
        def changed(reader):
            path = Path(next(iter(reader.refs)))
            path.write_bytes(path.read_bytes() + b' ')
            return original(reader)
        with mock.patch.object(m._Read, 'finish', changed), self.assertRaises(ValueError):
            m.resolve_saved_originals(*args)


if __name__ == '__main__':
    unittest.main()
