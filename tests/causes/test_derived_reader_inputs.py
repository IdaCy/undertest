import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_reader as reader
from causes import derived_reader_inputs as derived


class DerivedReaderInputsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = dict(model='gpt-6-luna', max_retries=0, store=False)
        config = self.save('config.json', self.config)
        prompts = {}
        for name in ('rubric', *reader.INSTRUMENTS):
            path = self.root / (name + '.txt')
            path.write_text('Reader instruction ' + name + '\n', encoding='utf-8')
            prompts[name] = derived.pin(path)
        self.protocol = dict(schema=derived.PROTOCOL_SCHEMA, checker_config=config,
            prompts=prompts, prompts_directory=str(self.root), reader_source=derived.pin(reader.__file__))
        initial = [dict(role='system', content='Assist with the repository.'),
                   dict(role='user', content='Implement the described change.')]
        messages = initial + [dict(role='assistant', content='I will inspect it.',
                                  reasoning='This might be an evaluation.')]
        self.view = dict(messages=messages, initial_context=initial,
                         prefix_context=None, phase='prefix')
        self.reference = dict(file=self.save('paired.json', dict(baseline=self.view)), selector=['baseline'])
        self.provenance = [self.save('provenance.json', dict(arm='treatment', cue='private benchmark cue'))]

    def save(self, name, value):
        path = self.root / name
        path.write_bytes(reader.encoded(value) + b'\n')
        return derived.pin(path)

    def row(self, **changes):
        args = dict(run_id='original-run-1', view_kind='neutral_initial_v1', stage='early',
                    reference=self.reference, provenance=self.provenance, protocol=self.protocol)
        args.update(changes)
        return derived.build_row(**args)

    def prepare(self, row):
        return derived.prepare_reader(row, dict(protocol=self.protocol, prompts_directory=str(self.root)),
                                      self.config, {'campaign_reader': reader})

    def test_native_payloads_and_generated_text_are_exact_without_private_metadata(self):
        row = self.row()
        view = self.prepare(row)
        self.assertEqual(view, self.view)
        self.assertEqual(set(view), {'messages', 'initial_context', 'prefix_context', 'phase'})
        context, targets = reader.prepare_context(view['messages'], 2, 'prefix', view['initial_context'])
        self.assertEqual(targets[2]['reasoning'], 'This might be an evaluation.')
        self.assertEqual(row['context_canonical_sha256'], reader.digest(context))
        for instrument in reader.INSTRUMENTS:
            payload, pins = reader.make_payload(self.config, self.root, context, instrument)
            body = reader.encoded(payload)
            self.assertEqual(row['payloads'][instrument], dict(canonical_sha256=reader.digest(payload),
                body_bytes=len(body), file_sha256=hashlib.sha256(body + b'\n').hexdigest(), pins=pins))
            self.assertNotIn('private benchmark cue', body.decode())
            self.assertNotIn('original-run-1', body.decode())
        self.assertFalse(row['runtime_authorized'])

    def test_identity_changes_with_run_view_kind_view_or_instrument_protocol(self):
        first = self.row()
        alternatives = [self.row(run_id='original-run-2'), self.row(view_kind='other_view')]
        changed = copy.deepcopy(self.view)
        changed['messages'][-1]['reasoning'] = 'A different actor response.'
        alternatives.append(self.row(reference=dict(file=self.save('changed-view.json', changed), selector=[])))
        protocol = copy.deepcopy(self.protocol)
        prompt = self.root / 'presence.txt'
        prompt.write_text('Changed reader threshold', encoding='utf-8')
        protocol['prompts']['presence'] = derived.pin(prompt)
        alternatives.append(self.row(protocol=protocol))
        self.assertTrue(first['measurement_key'].startswith('derived_reader_'))
        self.assertTrue(all(row['measurement_key'] != first['measurement_key'] for row in alternatives))

    def test_copying_identical_view_does_not_create_a_new_measurement(self):
        first = self.row()
        second = self.row(reference=dict(file=self.save('copy.json', self.view), selector=[]))
        self.assertEqual(first['measurement_key'], second['measurement_key'])

    def test_changed_payload_or_extra_row_metadata_is_rejected(self):
        original = self.row()
        for change in ('hash', 'omit', 'extra', 'key', 'stage'):
            row = copy.deepcopy(original)
            if change == 'hash': row['payloads']['presence']['canonical_sha256'] = '0' * 64
            if change == 'omit': del row['payloads']['strongly_favors']
            if change == 'extra': row['condition_label'] = 'evaluation'
            if change == 'key': row['measurement_key'] = 'fresh_candidate_old'
            if change == 'stage': row['stage'] = 'end'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(row)

    def test_changed_pinned_view_protocol_or_provenance_fails_before_use(self):
        row = self.row()
        for ref in (self.reference['file'], self.provenance[0], self.protocol['prompts']['presence']):
            path = Path(ref['path'])
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'input_changed'):
                self.prepare(row)
            path.write_bytes(original)

    def test_unavailable_arm_has_no_payload_and_cannot_be_run(self):
        row = derived.unavailable_row('missing-run', 'neutral_initial_v1', 'early',
                                      'actor failed', self.provenance, self.protocol)
        self.assertIsNone(row['view'])
        self.assertEqual(row['payloads'], {})
        with self.assertRaisesRegex(ValueError, 'prepared_row_required'):
            self.prepare(row)

    def test_membership_preserves_order_missingness_and_all_instruments(self):
        ready = self.row()
        missing = derived.unavailable_row('missing-run', 'neutral_initial_v1', 'early',
                                          'actor failed', self.provenance, self.protocol)
        rows = [ready, missing]
        assignments = [{key: row[key] for key in ('run_id', 'view_kind', 'stage')} for row in rows]
        self.assertEqual(derived.validate_membership(assignments, rows, self.protocol),
                         dict(assigned=2, ready=1, unavailable=1, requests=3))
        for altered in ([ready], list(reversed(rows)), [ready, ready]):
            with self.assertRaises(ValueError):
                derived.validate_membership(assignments, altered, self.protocol)
        with self.assertRaisesRegex(ValueError, 'duplicate_assignment'):
            derived.validate_membership([assignments[0]] * 2, [ready] * 2, self.protocol)

    def test_only_four_key_views_without_condition_metadata_are_accepted(self):
        for change in ('top', 'message', 'tool'):
            view = copy.deepcopy(self.view)
            if change == 'top': view['arm'] = 'treatment'
            if change == 'message': view['messages'][-1]['condition'] = 'evaluation'
            if change == 'tool':
                view['messages'][-1]['tool_calls'] = [dict(id='x', type='function',
                    function=dict(name='shell', arguments='pwd', condition='private'))]
            ref = dict(file=self.save(change + '.json', view), selector=[])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.row(reference=ref)

    def test_full_prefix_boundary_and_missing_reasoning_remain_native(self):
        full = copy.deepcopy(self.view)
        full['phase'] = 'full'
        full['prefix_context'] = copy.deepcopy(full['messages'])
        full['messages'].append(dict(role='assistant', content='Done.'))
        ref = dict(file=self.save('full.json', full), selector=[])
        row = self.row(reference=ref, stage='end')
        view = self.prepare(row)
        context, _ = reader.prepare_context(view['messages'], 2, 'full', view['initial_context'], view['prefix_context'])
        self.assertTrue(context['observation']['technical_missing_reasoning'])
        self.assertEqual(context['observation']['missing_reasoning_message_indices'], [3])
        with self.assertRaisesRegex(ValueError, 'stage_changed'):
            self.row(reference=ref)

    def test_runtime_config_and_native_module_cannot_change(self):
        row = self.row()
        prep = dict(protocol=self.protocol, prompts_directory=str(self.root))
        with self.assertRaisesRegex(ValueError, 'runtime_protocol_changed'):
            derived.prepare_reader(row, prep, dict(self.config, max_retries=1), {'campaign_reader': reader})
        with self.assertRaisesRegex(ValueError, 'module_mismatch'):
            derived.prepare_reader(row, prep, self.config, {'campaign_reader': object()})

    def test_prompt_changed_only_during_payload_construction_is_rejected(self):
        native = reader.make_payload
        path = self.root / 'presence.txt'
        original = path.read_bytes()

        def raced(config, directory, context, instrument):
            if instrument != 'presence':
                return native(config, directory, context, instrument)
            try:
                path.write_bytes(b'Different threshold during payload construction.')
                return native(config, directory, context, instrument)
            finally:
                path.write_bytes(original)

        with patch.object(reader, 'make_payload', raced):
            with self.assertRaisesRegex(ValueError, 'payload_protocol_changed'):
                self.row()

    def test_paired_prefix_is_required_for_full_and_forbidden_for_early(self):
        for phase, stage, prefix in [('full', 'end', None),
                                     ('prefix', 'early', self.view['messages'])]:
            view = copy.deepcopy(self.view)
            view.update(phase=phase, prefix_context=prefix)
            ref = dict(file=self.save(phase + '-bad-boundary.json', view), selector=[])
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'paired_prefix_required'):
                self.row(reference=ref, stage=stage)

    def test_full_prefix_cannot_be_shorter_than_initial_context(self):
        full = copy.deepcopy(self.view)
        full.update(phase='full', prefix_context=[])
        ref = dict(file=self.save('short-prefix.json', full), selector=[])
        with self.assertRaisesRegex(ValueError, 'context_boundary_out_of_range'):
            self.row(reference=ref, stage='end')


if __name__ == '__main__':
    unittest.main()
