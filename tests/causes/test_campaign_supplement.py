import copy
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as supplement


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'output'
        self.family = 'supplement_v1'
        self.model = 'model_a'
        self.context = [{'role': 'user', 'content': 'Synthetic task.'}]
        self.question = {'id': 'oversight_strong', 'text': 'Synthetic supplementary question.',
                         'awareness_type': 'oversight_awareness', 'threshold': 'strongly_favors'}
        self.config = {'model': 'synthetic-model', 'endpoint': 'http://invalid.example/synthetic',
                       'sampling': {'temperature': 0.6}, 'tools': [], 'questions': [self.question],
                       'answer_categories': ['yes', 'no'], 'probe_max_tokens': 8192,
                       'request_timeout_seconds': 600}
        context_path = self.root / 'context.json'
        supplement.save(context_path, self.context)
        self.context_pin = supplement.pin(context_path)
        self.contexts = {}
        self.disk = mock.patch.object(supplement.shutil, 'disk_usage',
                                      return_value=types.SimpleNamespace(free=100 * 1024**3))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def job(self, name='run0'):
        seed = runner.seed_for(7, ['probe', 'early', self.question['id']])
        payload = runner.request(self.config, self.context + [{'role': 'user', 'content': self.question['text']}], seed, [])
        payload.update(tool_choice='none', max_tokens=8192)
        raw = runner.encoded(payload)
        job = {'family': self.family, 'model': self.model, 'run_id': name, 'stage': 'early',
               'question_id': self.question['id'], 'measurement_id': f'{self.family}/{self.model}/{name}/early/{self.question["id"]}',
               'context': self.context_pin, 'context_canonical_sha256': runner.sha(self.context),
               'question_canonical_sha256': runner.sha(self.question), 'original_seed': 7, 'seed': seed,
               'request_body_sha256': supplement.hashlib.sha256(raw).hexdigest(),
               'request_file_sha256': supplement.hashlib.sha256(raw + b'\n').hexdigest(),
               'request_body_bytes': len(raw)}
        self.contexts[(name, 'early')] = {'original_bindings': {'context': self.context_pin}}
        return job

    def source_check(self, job):
        return supplement.prepare_request(job, self.contexts, self.config, runner)

    def exchange(self, endpoint, payload, destination, timeout):
        self.assertEqual(endpoint, self.config['endpoint'])
        self.assertEqual(timeout, 600)
        self.assertEqual(payload['max_tokens'], 8192)
        self.assertEqual(payload['tool_choice'], 'none')
        self.assertFalse(payload['stream'])
        supplement.mkdir(destination)
        supplement.save(destination / 'request.json', payload)
        supplement.save(destination / 'attempt.json', {'retry': False})
        response = {'prompt_text': '\n'.join(message['content'] for message in payload['messages']),
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"yes","reason":"Synthetic fixture."}'}}]}
        (destination / 'response.raw').write_bytes(runner.encoded(response))
        supplement.save(destination / 'receipt.json', {'status': 'complete', 'retry': False})
        return response

    def worker(self, job, destination):
        return supplement.probe_measurement(job, destination, self.source_check, self.config, runner)

    def queue(self, jobs, worker=None, admission=None, **kwargs):
        return supplement.run_queue(self.output, jobs, worker or self.worker,
                                    admission or (lambda: {'qualified': True}), self.source_check, [],
                                    family=self.family, model=self.model, **kwargs)

    def history(self):
        return supplement.read_history(self.output / '.control/journal.jsonl')

    def test_real_runner_capture_and_terminal_no_replay(self):
        job = self.job()
        before = copy.deepcopy((job, self.contexts, self.config))
        with mock.patch.object(runner, 'exchange', side_effect=self.exchange) as exchange:
            result = self.queue([job])
            self.assertEqual(result['new_final'], 1)
            again = self.queue([job])
            self.assertEqual(again['existing_final'], 1)
            self.assertEqual(again['new_final'], 0)
            self.assertEqual(exchange.call_count, 1)
        destination = supplement.measurement_path(self.output, job, family=self.family, model=self.model)
        final = supplement.load(destination / 'FINAL.json')
        self.assertFalse(final['retry'])
        self.assertEqual(final['result']['technical_status'], 'complete')
        self.assertEqual(before, (job, self.contexts, self.config))
        (destination / 'capture/response.raw').write_bytes(b'tampered')
        with self.assertRaisesRegex(supplement.IntegrityError, 'file_pin_mismatch'):
            self.queue([job], worker=mock.Mock(side_effect=AssertionError('must not replay')))

    def test_identity_scope_and_path_refusals_precede_output(self):
        job = self.job()
        for field, value in [('model', 'other'), ('family', 'other'), ('stage', 'late'), ('run_id', '..')]:
            with self.subTest(field=field):
                changed = {**job, field: value}
                with self.assertRaises(supplement.IntegrityError):
                    self.queue([changed])
                self.assertFalse(self.output.exists())
        for model in ['', '..', '/escape', 'a/b']:
            with self.subTest(model=model), self.assertRaises(supplement.IntegrityError):
                supplement.measurement_path(self.output, job, family=self.family, model=model)
        with self.assertRaisesRegex(supplement.IntegrityError, 'invalid_namespace'):
            supplement.measurement_path(self.output, {**job, 'run_id': '..', 'measurement_id': f'{self.family}/{self.model}/../early/{self.question["id"]}'}, family=self.family, model=self.model)

    def test_completed_input_numeric_identity_drift_is_refused(self):
        job = self.job()
        with mock.patch.object(runner, 'exchange', side_effect=self.exchange):
            self.assertEqual(self.queue([job])['new_final'], 1)
        destination = supplement.measurement_path(self.output, job, family=self.family, model=self.model)
        changed = {**job, 'original_seed': float(job['original_seed'])}
        self.assertEqual(changed, job)
        self.assertNotEqual(supplement.digest(changed), supplement.digest(job))
        (destination / 'INPUT.json').write_bytes(supplement.encoded(changed) + b'\n')
        worker = mock.Mock(side_effect=AssertionError('must not replay'))
        with self.assertRaisesRegex(supplement.IntegrityError, 'existing_input_conflict'):
            self.queue([job], worker=worker)
        worker.assert_not_called()

    def test_partial_and_unknown_attempts_are_not_replayed(self):
        job = self.job()
        destination = supplement.measurement_path(self.output, job, family=self.family, model=self.model)
        supplement.mkdir(destination)
        supplement.save(destination / 'INPUT.json', job)
        worker = mock.Mock(side_effect=AssertionError('must not replay'))
        with self.assertRaisesRegex(supplement.AdmissionHold, 'partial_attempt'):
            self.queue([job], worker=worker)
        worker.assert_not_called()
        with self.assertRaisesRegex(supplement.AdmissionHold, 'manual_reconciliation'):
            self.queue([job], worker=worker)
        other_output = supplement.mkdir(self.root / 'other')
        supplement.mkdir(other_output / self.model / 'unregistered' / 'early' / 'question')
        with self.assertRaisesRegex(supplement.IntegrityError, 'unregistered_attempt'):
            supplement.reconcile(other_output, [job], [], family=self.family, model=self.model)

    def test_request_context_seed_question_and_payload_integrity(self):
        job = self.job()
        self.assertEqual(self.source_check(job), (self.context, self.question))
        for field, value, error in [('seed', job['seed'] + 1, 'seed_identity'),
                                    ('question_canonical_sha256', '0' * 64, 'question_identity'),
                                    ('context_canonical_sha256', '0' * 64, 'context_canonical_hash'),
                                    ('request_body_sha256', '0' * 64, 'request_reconstruction_identity')]:
            with self.subTest(field=field), self.assertRaisesRegex(supplement.IntegrityError, error):
                self.source_check({**job, field: value})
        Path(self.context_pin['path']).write_bytes(b'[]\n')
        with self.assertRaisesRegex(supplement.IntegrityError, 'file_pin_mismatch'):
            self.source_check(job)

    def test_inherited_failure_counter_stops_before_replacement(self):
        jobs = [self.job('run' + str(i)) for i in range(4)]
        worker = mock.Mock(return_value={'technical_status': 'invalid_answer_schema'})
        result = self.queue(jobs, worker=worker, initial_failures=2, max_clients=1)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(result['new_final'], 1)
        self.assertEqual(result['remaining_unclaimed'], 3)
        self.assertEqual(result['consecutive_failures'], 3)
        self.assertEqual(result['stop_reason'], 'three_consecutive_infrastructure_failures')
        self.assertEqual(self.history()[0]['inherited_failures'], 2)
        with self.assertRaisesRegex(supplement.AdmissionHold, 'manual_reconciliation'):
            self.queue(jobs, worker=worker, initial_failures=0)
        self.assertEqual(worker.call_count, 1)

    def test_slow_admission_accounts_completed_failure_before_new_claim(self):
        jobs = [self.job('run' + str(i)) for i in range(3)]
        release = threading.Event()
        futures = []

        class TrackedExecutor(supplement.concurrent.futures.ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future

        def worker(job, destination):
            self.assertTrue(release.wait(2))
            return {'technical_status': 'invalid_answer_schema'}

        admissions = []

        def admission():
            admissions.append(True)
            if len(admissions) == 2:
                release.set()
                futures[0].result(timeout=2)
            return {'qualified': True}

        with mock.patch.object(supplement.concurrent.futures, 'ThreadPoolExecutor', TrackedExecutor):
            result = self.queue(jobs, worker=worker, admission=admission, initial_failures=2, max_clients=2)
        self.assertEqual(result['new_final'], 1)
        self.assertEqual(result['remaining_unclaimed'], 2)
        self.assertEqual(sum(row['event'] == 'claimed' for row in self.history()), 1)

    def test_admission_and_integrity_failures_do_not_retry(self):
        jobs = [self.job('run0'), self.job('run1')]
        worker = mock.Mock(side_effect=supplement.IntegrityError('rendered_prompt_missing'))
        result = self.queue(jobs, worker=worker, max_clients=1)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(result['stop_reason'], 'integrity_failure')
        self.assertEqual(result['remaining_unclaimed'], 1)
        fresh = self.root / 'held'
        self.output = fresh
        def hold():
            raise supplement.AdmissionHold('route_not_qualified')
        result = self.queue(jobs, worker=worker, admission=hold)
        self.assertIn('route_not_qualified', result['stop_reason'])
        self.assertEqual(result['new_final'], 0)
        self.assertEqual(worker.call_count, 1)

    def test_failure_and_client_parameters_and_unterminated_history(self):
        job = self.job()
        for kwargs in [{'max_clients': True}, {'max_clients': 3}, {'initial_failures': True}, {'initial_failures': -1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(supplement.IntegrityError):
                self.queue([job], **kwargs)
            self.assertFalse(self.output.exists())
        log = self.root / 'partial.jsonl'
        log.write_bytes(b'{"event":"claimed"}')
        with self.assertRaisesRegex(supplement.IntegrityError, 'unterminated_journal'):
            supplement.read_history(log)

    def test_storage_hold_drains_and_resume_preserves_existing_measurement(self):
        jobs = [self.job('run' + str(i)) for i in range(3)]
        free = types.SimpleNamespace(free=79 * 1024**3)
        worker = mock.Mock(return_value={'technical_status': 'complete'})
        with mock.patch.object(supplement.shutil, 'disk_usage', return_value=free), \
             mock.patch.object(runner, 'exchange', side_effect=self.exchange):
            held = self.queue(jobs, worker=worker, max_clients=1)
            self.assertIn('local_disk_below80GiB', held['stop_reason'])
            worker.assert_not_called()

            def finish(job, destination):
                result = self.worker(job, destination)
                free.free = 64 * 1024**3 - 1
                return result

            worker.side_effect = finish
            stopped = self.queue(jobs, worker=worker, max_clients=1,
                                 local_free_space_floor_bytes=64 * 1024**3)
            self.assertEqual((stopped['new_final'], stopped['remaining_unclaimed'], stopped['active']), (1, 2, 0))
            self.assertIn('local_disk_below_floor:', stopped['stop_reason'])
            first = supplement.measurement_path(self.output, jobs[0], family=self.family, model=self.model)
            retained = supplement.pin(first / 'FINAL.json')
            free.free = 64 * 1024**3
            worker.side_effect = self.worker
            resumed = self.queue(jobs, worker=worker, max_clients=1,
                                 local_free_space_floor_bytes=64 * 1024**3)
        self.assertEqual((resumed['existing_final'], resumed['new_final'], resumed['remaining_unclaimed']), (1, 2, 0))
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(supplement.pin(first / 'FINAL.json'), retained)
        self.assertEqual(resumed['local_free_space_floor_bytes'], 64 * 1024**3)
        self.assertEqual(sum(row['event'] == 'claimed' for row in self.history()), 3)

    def test_invalid_storage_floor_refuses_before_any_output(self):
        for value in (True, False, 0, -1, 1.0, '1', None):
            with self.subTest(value=value), self.assertRaisesRegex(supplement.IntegrityError, 'invalid_local_space_floor'):
                self.queue([self.job()], local_free_space_floor_bytes=value)
            self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
