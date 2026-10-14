"""Deterministic queue tests: synthetic workers only, never HTTP or host calls."""
import copy
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

from causes import campaign_supplement as core


class HealthyHarvestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'output'
        self.jobs = [dict(family='fixed', model='model', run_id=f'run{i}',
            stage='early', question_id='question', original_seed=i, seed=i,
            measurement_id=f'fixed/model/run{i}/early/question') for i in range(4)]
        self.free = types.SimpleNamespace(free=100 * 1024**3)
        self.disk = mock.patch.object(core.shutil, 'disk_usage', return_value=self.free)
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def history(self):
        return core.read_history(self.output / '.control/journal.jsonl')

    def capacity(self, jobs, clients):
        identity = {'synthetic_provider': True}
        evidence = self.root / 'capacity_evidence.json'
        core.save(evidence, {'synthetic': True})
        review = self.root / 'capacity_review.json'
        core.save(review, dict(accepted_supplement_capacity_qualification=True,
            max_clients=clients, provider_identity=identity,
            max_new_requests=len(jobs), evidence=[core.pin(evidence)]))
        path = self.root / 'capacity.json'
        core.save(path, dict(schema='causes.supplement_client_capacity_adoption.v1',
            root_authorized=True, no_user_revocation=True, max_clients=clients,
            family='fixed', model='model', output_root=str(self.output),
            jobs_sha256=core.digest(jobs), max_new_requests=len(jobs),
            provider_identity=identity, at=core.now(), mode='qualification',
            capacity_review=core.pin(review)))
        return core.pin(path), identity

    def scenario(self, *, enabled=True, statuses=('complete',), initial_failures=0,
                 release_at='admission', mutate=None, prepare=None, journal_hook=None,
                 recorder_failure=False, clients=2, pause_paths=()):
        count = len(statuses) + 1
        jobs = self.jobs[:count]
        capacity, identity = (self.capacity(jobs, clients) if clients > 2 else (None, {}))
        release = threading.Event()
        second_release = threading.Event()
        locked_release = [False]
        futures, admissions, prepared, worked = [], [], [], []
        original_executor = core.concurrent.futures.ThreadPoolExecutor
        original_save, original_journal = core.save, core.journal
        original_lock = threading.Lock

        class TrackedExecutor(original_executor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future

        def finish():
            release.set()
            for future in futures[:1 if release_at == 'snapshot' else len(statuses)]:
                try:
                    future.result(timeout=3)
                except BaseException:
                    # The queue, not this synchronization hook, classifies it.
                    pass

        def worker(job, destination):
            worked.append(job['measurement_id'])
            index = int(job['run_id'][3:])
            if index < len(statuses):
                gate = second_release if release_at == 'snapshot' and index == 1 else release
                if not gate.wait(3):
                    raise AssertionError('fixture release missing')
                value = statuses[index]
                if isinstance(value, BaseException):
                    raise value
                return {'technical_status': value}
            return {'technical_status': 'complete'}

        def admission():
            value = dict(reservation=len(admissions)+1, identity=identity)
            admissions.append(value)
            if len(admissions) == count and release_at in ('admission', 'snapshot'):
                finish()
                if mutate:
                    mutate(jobs, capacity, value)
            return value

        def source(job):
            prepared.append((job['measurement_id'], len(admissions), self.history()))
            if job is jobs[-1] and release_at == 'prepare' and not release.is_set():
                finish()
            if job is jobs[-1] and release_at == 'locked' and not release.is_set():
                locked_release[0] = True
            if prepare:
                prepare(job, admissions)

        def save(path, value):
            if recorder_failure and Path(path).name == 'FINAL.json' and value['measurement_id'] == jobs[0]['measurement_id']:
                raise OSError('synthetic recorder failure')
            return original_save(path, value)

        def journal(path, value):
            original_journal(path, value)
            if (release_at == 'snapshot' and value['event'] == 'route_success' and
                    value['measurement_id'] == jobs[0]['measurement_id']):
                second_release.set()
                futures[1].result(timeout=3)
            if journal_hook:
                journal_hook(value, admissions, capacity)

        class ClaimLock:
            def __init__(self):
                self.lock = original_lock()
            def __enter__(self):
                if locked_release[0]:
                    locked_release[0] = False
                    finish()
                self.lock.acquire()
                return self
            def __exit__(self, *args):
                self.lock.release()

        with mock.patch.object(core.concurrent.futures, 'ThreadPoolExecutor', TrackedExecutor), \
             mock.patch.object(core, 'save', side_effect=save), \
             mock.patch.object(core, 'journal', side_effect=journal), \
             mock.patch.object(core, 'threading', types.SimpleNamespace(
                 Event=threading.Event, Lock=ClaimLock)):
            result = core.run_queue(self.output, jobs, worker, admission, source, pause_paths,
                family='fixed', model='model', max_clients=clients,
                initial_failures=initial_failures, capacity_limit=capacity,
                retain_admission_after_healthy_harvest=enabled)
        return result, jobs, admissions, prepared, worked

    def test_opt_in_reuses_exact_candidate_and_prepares_after_harvest(self):
        result, jobs, admissions, prepared, worked = self.scenario()
        self.assertEqual(len(admissions), 2)
        self.assertEqual([row[0] for row in prepared], [job['measurement_id'] for job in jobs])
        terminal = [row for row in prepared[-1][2] if row['event'] == 'route_success']
        self.assertEqual([row['measurement_id'] for row in terminal], [jobs[0]['measurement_id']])
        self.assertEqual(result['new_final'], 2)
        self.assertEqual(worked, [job['measurement_id'] for job in jobs])
        destination = core.measurement_path(self.output, jobs[-1], family='fixed', model='model')
        self.assertEqual(core.load(destination/'INPUT.json'), jobs[-1])
        self.assertEqual(core.load(destination/'ADMISSION_OBSERVATION.json'), admissions[1])
        self.assertTrue(self.history()[0]['retain_admission_after_healthy_harvest'])

    def test_default_still_discards_candidate_before_preparation(self):
        result, jobs, admissions, prepared, _ = self.scenario(enabled=False)
        self.assertEqual(len(admissions), 3)
        self.assertEqual(prepared[-1][1], 3)
        self.assertNotIn('retain_admission_after_healthy_harvest', self.history()[0])
        self.assertEqual(result['new_final'], 2)

    def test_failure_forbids_reuse_even_below_stop_threshold(self):
        result, jobs, admissions, prepared, _ = self.scenario(statuses=('invalid_answer_schema',))
        self.assertEqual(len(admissions), 3)
        self.assertEqual(prepared[-1][1], 3)
        failures = [row for row in prepared[-1][2] if row['event'] == 'route_failure']
        self.assertEqual(failures[0]['consecutive_failures'], 1)
        self.assertEqual(result['consecutive_failures'], 0)

    def test_success_resetting_nonzero_failure_state_forbids_reuse(self):
        _, _, admissions, prepared, _ = self.scenario(initial_failures=1)
        self.assertEqual(len(admissions), 3)
        self.assertEqual(prepared[-1][1], 3)
        self.assertEqual(self.history()[0]['inherited_failures'], 1)

    def test_mixed_batch_cannot_reuse_even_when_final_counter_matches(self):
        result, jobs, admissions, prepared, _ = self.scenario(
            statuses=('invalid_answer_schema', 'complete'), clients=4)
        self.assertEqual(len(admissions), 4)
        self.assertEqual(prepared[-1][1], 4)
        outcomes = [row for row in prepared[-1][2] if row['event'] in ('route_success', 'route_failure')]
        self.assertEqual([row['measurement_id'] for row in outcomes], [job['measurement_id'] for job in jobs[:2]])
        self.assertEqual([row['consecutive_failures'] for row in outcomes], [1, 0])
        self.assertEqual(result['new_final'], 3)

    def test_integrity_failure_still_stops_and_never_prepares_replacement(self):
        result, _, admissions, prepared, worked = self.scenario(
            statuses=(core.IntegrityError('synthetic integrity failure'),))
        self.assertEqual((len(admissions), len(prepared), len(worked)), (2, 1, 1))
        self.assertEqual(result['stop_reason'], 'integrity_failure')
        self.assertEqual(result['remaining_unclaimed'], 1)

    def test_recorder_failure_still_quarantines_unresolved_attempt(self):
        result, _, _, prepared, worked = self.scenario(recorder_failure=True)
        self.assertEqual((len(prepared), len(worked)), (1, 1))
        self.assertEqual(result['stop_reason'], 'recorder_or_unresolved_current_attempt')
        self.assertTrue(any(row['event'] == 'unresolved_current_attempt' for row in self.history()))

    def test_third_failure_retains_native_threshold_and_history(self):
        result, _, _, prepared, worked = self.scenario(
            statuses=('invalid_answer_schema',), initial_failures=2)
        self.assertEqual((len(prepared), len(worked)), (1, 1))
        self.assertEqual(result['consecutive_failures'], 3)
        self.assertEqual(result['stop_reason'], 'three_consecutive_infrastructure_failures')

    def test_changed_assignment_is_refused_before_request_preparation(self):
        def mutate(jobs, capacity, observation):
            jobs[-1]['seed'] += 1
        result, _, _, prepared, worked = self.scenario(mutate=mutate)
        self.assertIn('reserved_admission_changed', result['stop_reason'])
        self.assertEqual((len(prepared), len(worked)), (1, 1))

    def test_changed_capacity_reference_is_refused(self):
        def mutate(jobs, capacity, observation):
            capacity['sha256'] = '0' * 64
        result, _, _, prepared, _ = self.scenario(mutate=mutate, clients=4)
        self.assertIn('reserved_admission_changed', result['stop_reason'])
        self.assertEqual(len(prepared), 1)

    def test_changed_observation_during_harvest_is_refused(self):
        def mutate(row, admissions, capacity):
            if row['event'] == 'route_success' and len(admissions) == 2:
                admissions[-1]['reservation'] = 999
        result, _, _, prepared, _ = self.scenario(journal_hook=mutate)
        self.assertIn('reserved_admission_changed', result['stop_reason'])
        self.assertEqual(len(prepared), 1)

    def test_fresh_source_or_guard_refusal_still_prevents_claim(self):
        def source(job, admissions):
            if job['run_id'] == 'run1':
                raise core.IntegrityError('fresh source or readiness refusal')
        result, _, admissions, prepared, worked = self.scenario(prepare=source)
        self.assertEqual(len(admissions), 2)
        self.assertEqual((len(prepared), len(worked)), (2, 1))
        self.assertIn('fresh source or readiness refusal', result['stop_reason'])

    def test_completion_during_readiness_still_discards_at_later_gate(self):
        result, _, admissions, prepared, worked = self.scenario(release_at='prepare')
        self.assertEqual(len(admissions), 3)
        self.assertEqual([row[1] for row in prepared], [1, 2, 3])
        self.assertEqual((result['new_final'], len(worked)), (2, 2))

    def test_completion_at_locked_gate_still_discards_before_claim(self):
        result, _, admissions, prepared, worked = self.scenario(release_at='locked')
        self.assertEqual(len(admissions), 3)
        self.assertEqual([row[1] for row in prepared], [1, 2, 3])
        self.assertEqual((result['new_final'], len(worked)), (2, 2))

    def test_completion_after_harvest_snapshot_still_hits_later_gate(self):
        result, jobs, admissions, prepared, worked = self.scenario(
            release_at='snapshot', statuses=('complete', 'complete'), clients=4)
        self.assertEqual(len(admissions), 4)
        first_ready = [row for row in prepared if row[0] == jobs[-1]['measurement_id']][0]
        self.assertEqual([row['measurement_id'] for row in first_ready[2]
                          if row['event'] == 'route_success'], [jobs[0]['measurement_id']])
        self.assertEqual((result['new_final'], len(worked)), (3, 3))

    def test_changed_capacity_file_is_checked_again_after_harvest(self):
        def mutate(row, admissions, capacity):
            if row['event'] == 'route_success':
                Path(capacity['path']).write_bytes(b'{}\n')
        result, _, _, prepared, worked = self.scenario(journal_hook=mutate, clients=4)
        self.assertIsNotNone(result['stop_reason'])
        self.assertEqual((len(prepared), len(worked)), (1, 1))

    def test_disk_floor_is_rechecked_after_harvest_before_preparation(self):
        def lower_space(row, admissions, capacity):
            if row['event'] == 'route_success':
                self.free.free = 79 * 1024**3
        result, _, _, prepared, worked = self.scenario(journal_hook=lower_space)
        self.assertIn('local_disk_below80GiB', result['stop_reason'])
        self.assertEqual((len(prepared), len(worked)), (1, 1))

    def test_pause_is_rechecked_after_harvest_before_preparation(self):
        pause = self.root / 'PAUSE'
        def pause_after_harvest(row, admissions, capacity):
            if row['event'] == 'route_success':
                pause.write_text('synthetic pause')
        result, _, _, prepared, worked = self.scenario(
            journal_hook=pause_after_harvest, pause_paths=[pause])
        self.assertIn('operator_pause', result['stop_reason'])
        self.assertEqual((len(prepared), len(worked)), (1, 1))

    def test_repeated_healthy_completions_do_not_starve_harvest_or_repeat_claims(self):
        futures, admissions, prepared = [], [], []
        releases = [threading.Event() for _ in self.jobs]
        original_executor = core.concurrent.futures.ThreadPoolExecutor
        class TrackedExecutor(original_executor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future
        def worker(job, destination):
            index = int(job['run_id'][3:])
            if index < len(self.jobs)-1:
                self.assertTrue(releases[index].wait(3))
            return {'technical_status': 'complete'}
        def admission():
            admissions.append(len(admissions)+1)
            if futures:
                releases[len(futures)-1].set()
                futures[-1].result(timeout=3)
            return {'reservation': admissions[-1]}
        def prepare(job):
            terminals = [row for row in self.history() if row['event'] == 'route_success']
            self.assertEqual(len(terminals), len(prepared))
            prepared.append(job['measurement_id'])
        with mock.patch.object(core.concurrent.futures, 'ThreadPoolExecutor', TrackedExecutor):
            result = core.run_queue(self.output, self.jobs, worker, admission, prepare, [],
                family='fixed', model='model', max_clients=2,
                retain_admission_after_healthy_harvest=True)
        self.assertEqual(len(admissions), len(self.jobs))
        self.assertEqual((result['new_final'], result['remaining_unclaimed']), (len(self.jobs), 0))
        self.assertEqual([row['measurement_id'] for row in self.history()
                          if row['event'] == 'claimed'], prepared)

    def test_invalid_option_refuses_before_output(self):
        for value in (1, 0, None, 'yes'):
            with self.subTest(value=value), self.assertRaisesRegex(core.IntegrityError, 'healthy_harvest_option'):
                core.run_queue(self.output, self.jobs[:1], mock.Mock(), mock.Mock(), mock.Mock(), [],
                    family='fixed', model='model', retain_admission_after_healthy_harvest=value)
            self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
