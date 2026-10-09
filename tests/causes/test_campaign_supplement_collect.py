import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_collect as collector


class SupplementCollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.family, self.model = 'supplement_v1', 'model_a'
        self.run_root = self.root / 'attempts'
        self.run_root.mkdir()
        self.output = self.root / 'export'
        self.question = {'id': 'oversight_strong', 'text': 'Synthetic question.',
                         'awareness_type': 'oversight', 'threshold': 'strongly_favors'}
        self.config = {'model': 'synthetic', 'endpoint': 'http://invalid.example',
                       'sampling': {'temperature': 0.6}, 'tools': [], 'questions': [self.question],
                       'answer_categories': ['yes', 'no'], 'probe_max_tokens': 8192,
                       'request_timeout_seconds': 600}
        self.context = [{'role': 'user', 'content': 'Saved synthetic task.'}]
        self.config_pin = self.save('config.json', self.config)
        self.context_pin = self.save('context.json', self.context)
        self.roster = [{'run_id': 'run' + str(i), 'seed': 7 + i} for i in range(3)]
        self.obligations = [self.obligation(r, stage, self.question) for r in self.roster for stage in ('early', 'end')]
        self.ready = [self.prepare(j) for j in self.obligations[:4]]
        self.history = []
        self.rows = [self.original(self.roster[0], both=True), self.original(self.roster[1], both=False)]

    def save(self, name, value):
        p = self.root / name
        p.write_bytes(core.encoded(value) + b'\n')
        return core.pin(p)

    def obligation(self, allocation, stage, question):
        return {'family': self.family, 'model': self.model, 'run_id': allocation['run_id'], 'stage': stage,
                'question_id': question['id'], 'measurement_id': '/'.join((self.family, self.model, allocation['run_id'], stage, question['id'])),
                'awareness_type': question['awareness_type'], 'threshold': question['threshold'],
                'original_seed': allocation['seed'], 'seed': runner.seed_for(allocation['seed'], ['probe', stage, question['id']]),
                'question_canonical_sha256': runner.sha(question), 'disposition': 'historical_planned_only'}

    def prepare(self, job):
        q = next(q for q in self.config['questions'] if q['id'] == job['question_id'])
        payload = runner.request(self.config, self.context + [{'role': 'user', 'content': q['text']}], job['seed'], [])
        payload.update(tool_choice='none', max_tokens=8192)
        raw = runner.encoded(payload)
        return {**job, 'context': self.context_pin, 'context_canonical_sha256': runner.sha(self.context),
                'request_body_sha256': hashlib.sha256(raw).hexdigest(), 'request_body_bytes': len(raw),
                'request_file_sha256': hashlib.sha256(raw + b'\n').hexdigest(), 'frozen_config': self.config_pin}

    def original(self, allocation, *, both):
        return {'run_id': allocation['run_id'], 'model': self.model, 'allocation': copy.deepcopy(allocation),
                'self': {s: {'evaluation_presence': {'label': 'unchanged'}} for s in (('early', 'end') if both else ('early',))},
                'reader': {}, 'reader_dispositions': {'early': {'status': 'unchanged'}}, 'actor': {'unchanged': True}}

    def contexts(self):
        return {(j['run_id'], j['stage']): {'run_id': j['run_id'], 'stage': j['stage'],
                'original_bindings': {'context': self.context_pin, 'config': self.config_pin}} for j in self.ready}

    def final(self, job, mode='complete'):
        destination = core.measurement_path(self.run_root, job, family=self.family, model=self.model)
        core.mkdir(destination / 'capture')
        core.save(destination / 'INPUT.json', job)
        capture = destination / 'capture'

        def exchange(endpoint, payload, path, timeout):
            self.assertEqual((endpoint, Path(path), timeout), (self.config['endpoint'], capture, 600))
            core.save(capture / 'request.json', payload)
            core.save(capture / 'attempt.json', {'retry': False, 'request_sha256': job['request_body_sha256']})
            if mode == 'transport_failure':
                core.save(capture / 'receipt.json', {'status': 'exception', 'retry': False,
                                                   'request_sha256': job['request_body_sha256'], 'error': 'TimeoutError'})
                raise TimeoutError('synthetic only')
            response = {'prompt_text': '\n'.join(m['content'] for m in payload['messages']),
                        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                            {'answer': 'yes', 'reason': 'Synthetic only.'} if mode == 'complete' else {'unexpected': True})}}]}
            raw = core.encoded(response)
            (capture / 'response.raw').write_bytes(raw)
            core.save(capture / 'receipt.json', {'status': 'complete', 'retry': False, 'http_status': 200,
                'request_sha256': job['request_body_sha256'], 'response_sha256': hashlib.sha256(raw).hexdigest()})
            return response

        try:
            with mock.patch.object(runner, 'exchange', side_effect=exchange):
                result = core.probe_measurement(job, capture,
                    lambda j: core.prepare_request(j, self.contexts(), self.config, runner), self.config, runner)
            status, failure = result['technical_status'], None
        except TimeoutError as error:
            result, status, failure = None, 'failure', repr(error)
        final = {'measurement_id': job['measurement_id'], 'input_sha256': core.digest(job),
                 'technical_status': status, 'admissible': status == 'complete', 'retry': False,
                 'result': result, 'failure': failure, 'integrity_failure': False,
                 'capture_files': [core.pin(p) for p in sorted(capture.iterdir())]}
        core.save(destination / 'FINAL.json', final)
        self.history.extend([{'event': 'claimed', 'measurement_id': job['measurement_id'], 'input_sha256': core.digest(job)},
            {'event': 'route_success' if status == 'complete' else 'route_failure', 'measurement_id': job['measurement_id'],
             'technical_status': status, 'final': core.pin(destination / 'FINAL.json')}])
        return destination

    def binding(self, *, pending=None, unavailable=None):
        ids = [j['measurement_id'] for j in self.obligations]
        ready_ids = [j['measurement_id'] for j in self.ready]
        pending = pending if pending is not None else ids[4:5]
        unavailable = unavailable if unavailable is not None else ids[5:]
        prepared = {'family': self.family, 'model': self.model, 'output_root_proposed': str(self.run_root),
                    'all_original_measurement_ids': ids, 'ready_jobs': self.ready, 'ready_order': ready_ids,
                    'contexts': list(self.contexts().values()), 'pending_measurement_ids': pending,
                    'unavailable_measurement_ids': unavailable, 'frozen_config': self.config_pin,
                    'frozen_runner': core.pin(Path(runner.__file__).resolve())}
        def lines(name, rows):
            p = self.root / name; p.write_bytes(b''.join(core.encoded(r) + b'\n' for r in rows)); return core.pin(p)
        value = {'schema': 'causes.supplement_export_binding.v1', 'family': self.family, 'model': self.model,
                 'supplement_root': str(self.run_root), 'stages': ['early', 'end'],
                 'question_ids': [q['id'] for q in self.config['questions']],
                 'prepared_inputs': self.save('prepared.json', prepared), 'roster': self.save('roster.json', self.roster),
                 'original_obligations': lines('obligations.jsonl', self.obligations),
                 'trajectories': lines('original.jsonl', self.rows), 'journal_cut': lines('journal_copy.jsonl', self.history),
                 'config': self.config_pin, 'runner_source': core.pin(Path(runner.__file__).resolve()),
                 'core_source': core.pin(Path(core.__file__).resolve()),
                 'collector_source': core.pin(Path(collector.__file__).resolve()), 'required_pins': []}
        return self.save('binding.json', value)

    def collect(self, binding):
        original_exchange, original_save = runner.exchange, runner.save
        with mock.patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')):
            result = collector.collect(binding, self.output, runner=runner)
        self.assertIs(runner.exchange, original_exchange)
        self.assertIs(runner.save, original_save)
        return json.loads(core.pinned(result))

    def test_complete_missing_pending_unavailable_and_absent_original_join(self):
        for job, mode in zip(self.ready, ('complete', 'invalid', 'transport_failure')):
            self.final(job, mode)
        before = copy.deepcopy((self.obligations, self.ready, self.rows))
        summary = self.collect(self.binding())
        self.assertEqual(summary['dispositions'], {'final_verified': 3, 'unclaimed_at_journal_cut': 1,
            'pending_original_context_at_cut': 1, 'assigned_stage_unavailable': 1})
        self.assertFalse(summary['all_ready_final'])
        self.assertFalse(summary['all_obligations_final_or_unavailable'])
        rows = [json.loads(line) for line in (self.output / 'MEASUREMENTS.jsonl').read_text().splitlines()]
        self.assertEqual([r['admissible_label'] for r in rows], ['yes', None, None, None, None, None])
        joined = json.loads((self.output / 'trajectories/run1.json').read_text())
        self.assertEqual(joined['original_data_availability']['self_stages_present'], ['early'])
        self.assertNotIn('end', joined['original_trajectory']['self'])
        absent = json.loads((self.output / 'trajectories/run2.json').read_text())
        self.assertIsNone(absent['original_trajectory'])
        self.assertIsNone(absent['original_reader_unchanged'])
        self.assertEqual(len(absent['supplement_measurements']), 2)
        self.assertEqual(before, (self.obligations, self.ready, self.rows))

    def test_full_1440_original_identity_and_order_without_available_rows(self):
        self.roster = [{'run_id': f'run{i:02}', 'seed': i + 7} for i in range(60)]
        self.config['questions'] = [{**self.question, 'id': f'question{i:02}'} for i in range(12)]
        self.config_pin = self.save('config.json', self.config)
        self.obligations = [self.obligation(r, s, q) for r in reversed(self.roster)
                            for s in ('end', 'early') for q in reversed(self.config['questions'])]
        self.ready, self.rows = [], []
        ids = [j['measurement_id'] for j in self.obligations]
        summary = self.collect(self.binding(pending=ids[:1008], unavailable=ids[1008:]))
        rows = [json.loads(line) for line in (self.output / 'MEASUREMENTS.jsonl').read_text().splitlines()]
        self.assertEqual([r['original_obligation'] for r in rows], self.obligations)
        self.assertEqual([r['measurement_id'] for r in rows], ids)
        self.assertEqual(summary['supplement_assigned'], 1440)
        self.assertEqual(summary['original_stage_obligations'], 120)
        self.assertEqual(summary['original_rows_available_at_cut'], 0)
        self.assertFalse(summary['all_obligations_final_or_unavailable'])
        index = json.loads((self.output / 'INDEX.json').read_text())
        self.assertEqual([r['run_id'] for r in index], [r['run_id'] for r in self.roster])

    def test_other_model_same_id_is_filtered_and_does_not_fill_absence(self):
        other = self.original(self.roster[0], both=True)
        other['model'] = 'model_b'
        other['allocation']['seed'] = 999
        only_other = self.original(self.roster[2], both=True)
        only_other['model'] = 'model_b'
        self.rows = [other, *self.rows, only_other]
        summary = self.collect(self.binding())
        self.assertEqual(summary['original_rows_available_at_cut'], 2)
        present = json.loads((self.output / 'trajectories/run0.json').read_text())
        self.assertEqual(present['original_trajectory']['model'], self.model)
        self.assertEqual(present['allocation']['seed'], 7)
        absent = json.loads((self.output / 'trajectories/run2.json').read_text())
        self.assertIsNone(absent['original_trajectory'])
        self.assertFalse(absent['original_data_availability']['trajectory_in_frozen_cut'])
        self.assertEqual(len(absent['supplement_measurements']), 2)

    def test_saved_numeric_identity_drift_refuses_and_partial_output_is_not_reused(self):
        path = self.final(self.ready[0])
        changed = {**self.ready[0], 'original_seed': float(self.ready[0]['original_seed'])}
        self.assertEqual(changed, self.ready[0])
        (path / 'INPUT.json').write_bytes(core.encoded(changed) + b'\n')
        binding = self.binding()
        with self.assertRaisesRegex(core.IntegrityError, 'saved_input_identity'):
            self.collect(binding)
        self.assertTrue((self.output / 'ERROR.json').is_file())
        self.assertFalse((self.output / 'SUMMARY.json').exists())
        with self.assertRaisesRegex(core.IntegrityError, 'fresh_separate_output'):
            self.collect(binding)

    def test_unready_claim_and_original_order_drift_refuse_before_output(self):
        self.history = [{'event': 'claimed', 'measurement_id': self.obligations[-1]['measurement_id'], 'input_sha256': 'unused'}]
        with self.assertRaisesRegex(core.IntegrityError, 'unready_obligation_attempted'):
            self.collect(self.binding())
        self.assertFalse(self.output.exists())
        self.history = []
        binding = self.binding(); b = json.loads(core.pinned(binding)); prepared = json.loads(core.pinned(b['prepared_inputs']))
        prepared['all_original_measurement_ids'].reverse(); b['prepared_inputs'] = self.save('prepared.json', prepared)
        with self.assertRaisesRegex(core.IntegrityError, 'original_order_changed'):
            self.collect(self.save('binding.json', b))
        self.assertFalse(self.output.exists())

    def test_later_final_not_in_pinned_journal_stays_unclaimed(self):
        binding = self.binding()
        self.final(self.ready[0])  # Later terminal deliberately absent from the retained cut.
        summary = self.collect(binding)
        self.assertEqual(summary['terminals_at_consumed_cut'], 0)
        self.assertEqual(summary['dispositions']['unclaimed_at_journal_cut'], 4)

    def test_missing_success_capture_and_original_instrument_replacement_refuse(self):
        path = self.final(self.ready[0]); final = core.load(path / 'FINAL.json')
        final['capture_files'] = [p for p in final['capture_files'] if not p['path'].endswith('/receipt.json')]
        (path / 'FINAL.json').write_bytes(core.encoded(final) + b'\n')
        self.history[-1]['final'] = core.pin(path / 'FINAL.json')
        with self.assertRaisesRegex(core.IntegrityError, 'parsed_result_missing_capture'):
            self.collect(self.binding())
        self.output = self.root / 'second_export'; self.history = []
        self.rows[0]['self']['early'][self.question['id']] = {'label': 'original'}
        with self.assertRaisesRegex(core.IntegrityError, 'supplement_cannot_replace'):
            self.collect(self.binding())

    def test_input_pin_drift_after_real_verification_preserves_partial_failure(self):
        self.final(self.ready[0]); binding = self.binding()
        b = json.loads(core.pinned(binding)); source = self.save('qualified_source.json', {'version': 1})
        b['required_pins'] = [source]; binding = self.save('binding.json', b)
        real_verify = collector.verify_final
        def mutate(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            Path(source['path']).write_text('{}\n')
            return result
        with mock.patch.object(collector, 'verify_final', side_effect=mutate):
            with self.assertRaisesRegex(core.IntegrityError, 'file_pin_mismatch'):
                self.collect(binding)
        self.assertTrue((self.output / 'ERROR.json').is_file())
        self.assertTrue((self.output / 'MEASUREMENTS.jsonl').is_file())
        self.assertFalse((self.output / 'SUMMARY.json').exists())

    def test_output_confinement_and_active_journal_refusal(self):
        binding = self.binding()
        for output in (self.run_root, self.run_root / 'export', self.root):
            with self.subTest(output=output), self.assertRaisesRegex(core.IntegrityError, 'fresh_separate_output'):
                collector.collect(binding, output, runner=runner)
        b = json.loads(core.pinned(binding)); control = core.mkdir(self.run_root / '.control')
        (control / 'journal.jsonl').write_bytes(b''); b['journal_cut'] = core.pin(control / 'journal.jsonl')
        with self.assertRaisesRegex(core.IntegrityError, 'immutable_journal_copy'):
            self.collect(self.save('binding.json', b))


if __name__ == '__main__':
    unittest.main()
