import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_collect as collector
from causes import derived_probe_collect as derived_collect
from causes import derived_probe_inputs as derived
from causes import derived_reader_inputs as saved


class DerivedProbeCollectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = dict(model='model', endpoint='http://fixture.invalid',
            sampling=dict(temperature=.6, top_p=.95), tools=[], probe_max_tokens=8192,
            request_timeout_seconds=600, probe_response_format=dict(type='json_object'),
            answer_categories=['yes', 'no'], questions=[dict(id='evaluation_' + t,
                awareness_type='evaluation', threshold=t, text='Frozen question: ' + t)
                for t in derived.THRESHOLDS])
        self.protocol = dict(schema=derived.PROTOCOL_SCHEMA, family='derived_probe_fixture',
            model='model', config=self.save('config.json', self.config),
            sources={n: saved.pin(m.__file__) for n, m in derived.MODULES.items()})
        self.origin = dict(config=self.protocol['config'], allocation=self.save('allocation.json',
            dict(run_id='original', seed=41, repetition=0, canonical_variant='F07L05')))
        initial = [dict(role='system', content='Same neutral task summary.'),
                   dict(role='user', content='Implement the requested change.')]
        self.view = dict(initial_context=initial, prefix_context=None, phase='prefix',
            messages=initial + [dict(role='assistant', content='This may be an evaluation.',
                reasoning='Keep this model-authored text.')])
        self.reference = dict(file=self.save('view.json', dict(arms=dict(baseline=self.view))),
                              selector=['arms', 'baseline'])
        self.row = derived.build_row('original', 'neutral', 'early', self.reference,
            [self.save('provenance.json', dict(private_condition='never in request'))],
            self.origin, self.protocol)
        self.output = self.root / 'output'
        self.answer = 'yes'

    def save(self, name, value):
        p = self.root / name
        p.write_bytes(runner.encoded(value) + b'\n')
        return saved.pin(p)

    def record(self, value, read=False):
        if isinstance(value, dict):
            raw = saved.raw(value)
            return raw if read else value
        ref = saved.pin(value)
        return saved.raw(ref) if read else ref

    def exchange(self, endpoint, payload, destination, timeout):
        self.assertEqual((endpoint, timeout), (self.config['endpoint'], 600))
        destination = Path(destination)
        destination.mkdir(parents=True)
        request_hash = runner.sha(payload)
        runner.save(destination / 'request.json', payload)
        runner.save(destination / 'attempt.json', dict(request_sha256=request_hash, retry=False))
        response = dict(prompt_text=runner.encoded(payload['messages']).decode(),
            choices=[dict(finish_reason='stop', message=dict(role='assistant',
            content=json.dumps(dict(answer=self.answer, reason='saved fixture'))))])
        runner.save(destination / 'response.raw', response)
        raw = (destination / 'response.raw').read_bytes()
        runner.save(destination / 'receipt.json', dict(http_status=200, status='complete',
            retry=False, response_sha256=hashlib.sha256(raw).hexdigest(), request_sha256=request_hash))
        return response

    def generate(self, expected=3):
        check = lambda job: derived.prepare_probe(job, self.row, self.protocol, self.config)
        with patch.object(runner, 'exchange', self.exchange), patch.object(core.shutil, 'disk_usage') as disk:
            disk.return_value.free = 100 * 1024**3
            result = core.run_queue(self.output, self.row['jobs'],
                lambda job, dest: core.probe_measurement(job, dest, check, self.config, runner),
                lambda: dict(fixture=True), check, [], family=self.protocol['family'],
                model=self.protocol['model'], max_clients=2)
        self.assertEqual(result['new_final'], expected)
        self.terminals = {e['measurement_id']: e for e in core.read_history(
            self.output / '.control/journal.jsonl') if e['event'] in ('route_success', 'route_failure')}

    def verify(self, job=None, row=None):
        job = job or self.row['jobs'][0]
        with patch.object(runner, 'exchange', side_effect=AssertionError('no network')):
            return derived_collect.verify_final(job, self.terminals[job['measurement_id']],
                root=self.output, row=row or self.row, protocol=self.protocol, record=self.record)

    def test_native_saved_finals_all_three_instruments_keep_frozen_job_identity(self):
        original = copy.deepcopy(self.row)
        self.generate()
        for job in self.row['jobs']:
            result = self.verify(job)
            self.assertEqual(result['label'], 'yes')
            self.assertEqual(result['offline_verification'], 'exact_frozen_request_render_and_parser_reconstructed')
            self.assertEqual(result['input_canonical_sha256'], core.digest(job))
            self.assertEqual(result['derived_view'], self.reference)
            self.assertEqual(result['derived_input_binding']['config'], self.protocol['config'])
            self.assertNotIn('frozen_config', job)
            self.assertNotIn('context', job)
        self.assertEqual(self.row, original)

    def test_changed_view_origin_provenance_or_job_refuses(self):
        self.generate()
        for ref in [self.reference['file'], self.origin['allocation'], *self.row['provenance']]:
            p = Path(ref['path']); raw = p.read_bytes(); p.write_bytes(raw + b' ')
            with self.subTest(path=p), self.assertRaises(core.IntegrityError):
                self.verify()
            p.write_bytes(raw)
        changed = copy.deepcopy(self.row)
        changed['jobs'][0]['seed'] += 1
        with self.assertRaises(core.IntegrityError):
            self.verify(changed['jobs'][0], changed)

    def test_binding_cannot_bypass_default_input_checks_or_replace_legacy_fields(self):
        self.generate()
        job = self.row['jobs'][0]; terminal = self.terminals[job['measurement_id']]
        with self.assertRaises(KeyError):
            collector.verify_final(job, terminal, root=self.output, family=self.protocol['family'],
                model='model', contexts={}, config=self.config, runner=runner, record=self.record)
        binding = self.verify()['derived_input_binding']
        with self.assertRaisesRegex(core.IntegrityError, 'requires_request_preparer'):
            collector.verify_final(job, terminal, root=self.output, family=self.protocol['family'],
                model='model', contexts={}, config=self.config, runner=runner, record=self.record,
                input_binding=binding)
        for field in ['context', 'frozen_config']:
            changed = dict(job, **{field: self.protocol['config']})
            with self.subTest(field=field), self.assertRaises(core.IntegrityError):
                self.verify(changed)

    def test_saved_payload_change_refuses_even_with_rehashed_final_and_terminal(self):
        self.generate()
        job = self.row['jobs'][0]; terminal = self.terminals[job['measurement_id']]
        final_path = Path(terminal['final']['path']); final = json.loads(final_path.read_bytes())
        request = final_path.parent / 'capture/request.json'
        payload = json.loads(request.read_bytes()); payload['messages'][0]['content'] = 'Reintroduced cue'
        request.write_bytes(runner.encoded(payload) + b'\n')
        final['capture_files'] = [saved.pin(request) if Path(r['path']) == request else r
                                  for r in final['capture_files']]
        final_path.write_bytes(runner.encoded(final) + b'\n'); terminal['final'] = saved.pin(final_path)
        with self.assertRaisesRegex(core.IntegrityError, 'actual_payload_identity'):
            self.verify()

    def test_invalid_answer_remains_missing(self):
        self.answer = 'invalid category'
        self.generate()
        for job in self.row['jobs']:
            if job['measurement_id'] not in self.terminals:
                continue
            result = self.verify(job)
            self.assertIsNone(result['label'])
            self.assertFalse(result['technical_status'] == 'complete')


if __name__ == '__main__':
    unittest.main()
