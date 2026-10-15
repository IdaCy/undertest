"""Real Admission and native queue integration; workers only return fixture data."""
import copy
import datetime as dt
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from test_campaign_supplement_completed_actor import Fixture


class CompletedActorReadyHarvestTests(unittest.TestCase):
    def fixture(self, *, count=2, clients=2, enabled=True, initial_failures=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        f = Fixture(Path(temporary.name).resolve(), count=count, clients=clients)
        f.time = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        for document in (f.plan, f.contract):
            document['retain_admission_after_healthy_harvest'] = True
            document['retain_admission_after_ready_harvest'] = enabled
        if initial_failures:
            old = f.plan['predecessor_custody']
            previous = direct.read_value(old['actual'], core.pinned)
            previous['consecutive_failures'] = initial_failures
            actual = f.save('predecessor_with_failures', previous)
            review = direct.read_value(old['review'], core.pinned)
            review['actual'] = actual
            custody = dict(actual=actual, review=f.save('failure_custody_review', review))
            f.plan['predecessor_custody'] = custody
            f.contract['predecessor_custody'] = custody
            f.plan['initial_failures'] = initial_failures
        f.guard_changes['consecutive_failures'] = initial_failures
        if clients > 2:
            review = f.save('capacity_review', dict(
                accepted_supplement_capacity_qualification=True,
                max_clients=clients, provider_identity=f.identity,
                max_new_requests=count, evidence=[f.evidence]))
            f.plan['capacity_limit'] = f.save('capacity', dict(
                schema='causes.supplement_client_capacity_adoption.v1',
                root_authorized=True, no_user_revocation=True,
                max_clients=clients, family='family', model='model',
                output_root=str(f.output), jobs_sha256=core.digest(f.jobs),
                max_new_requests=count, provider_identity=f.identity,
                at=core.now(), mode='qualification', capacity_review=review))
        return f

    def run_case(self, *, enabled=True, statuses=('complete',), initial_failures=0,
                 guard_change=None, source_change=False, expiry=None,
                 remaining_completion=None):
        count = len(statuses) + 1
        f = self.fixture(count=count, clients=4 if count > 2 else 2,
                         enabled=enabled, initial_failures=initial_failures)
        original_jobs = copy.deepcopy(f.jobs)
        baseline_rows = len(f.rows)
        gates = [threading.Event() for _ in statuses]
        futures, worked, phases, candidates = [], [], [], []
        ready_count = 0
        source_changed = False
        release_under_lock = False
        original_executor = core.concurrent.futures.ThreadPoolExecutor
        original_lock = threading.Lock
        original_journal = core.journal
        original_guard = f.guard
        if expiry == 'after_repeated_ready':
            f.ready_delays = [0] * count + [31]

        class TrackedExecutor(original_executor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future

        def finish(index):
            gates[index].set()
            futures[index].result(timeout=5)

        def worker(job, destination):
            index = f.jobs.index(job)
            worked.append(job['measurement_id'])
            if index < len(statuses):
                if not gates[index].wait(5):
                    raise AssertionError('fixture release missing')
                return {'technical_status': statuses[index]}
            return {'technical_status': 'complete'}

        def guard(phase):
            nonlocal ready_count, release_under_lock
            phases.append(phase)
            value = original_guard(phase)
            if phase == 'claim_ready':
                ready_count += 1
                if ready_count == count:
                    # The earlier worker finishes inside the last job's first
                    # original prepare, after its admission has already passed.
                    for index in range(1 if remaining_completion else len(statuses)):
                        finish(index)
                elif ready_count == count + 1 and remaining_completion:
                    if remaining_completion == 'repeated_prepare':
                        finish(1)
                    else:
                        release_under_lock = True
            return value

        def source(job):
            if source_changed:
                raise core.IntegrityError('synthetic changed immutable source')

        def journal(path, value):
            nonlocal source_changed
            original_journal(path, value)
            if value['event'] in ('route_success', 'route_failure'):
                f.guard_changes['consecutive_failures'] = value['consecutive_failures']
                if value['measurement_id'] == f.jobs[0]['measurement_id']:
                    f.guard_changes.update(guard_change or {})
                    source_changed = source_change
                    if expiry == 'harvest':
                        f.advance(31)

        class ClaimLock:
            def __init__(self):
                self.lock = original_lock()

            def __enter__(self):
                nonlocal release_under_lock
                if release_under_lock:
                    release_under_lock = False
                    finish(1)
                self.lock.acquire()
                return self

            def __exit__(self, *args):
                self.lock.release()

        f.admission = f.make(guard=guard, source_check=source)
        original_check = f.admission.check

        def check():
            value = original_check()
            candidates.append(value)
            return value

        f.admission.check = check
        with mock.patch.object(core.concurrent.futures, 'ThreadPoolExecutor', TrackedExecutor), \
             mock.patch.object(core, 'journal', side_effect=journal), \
             mock.patch.object(core, 'threading', types.SimpleNamespace(
                 Event=threading.Event, Lock=ClaimLock)):
            result = direct.run_queue(f.output, f.jobs, worker, f.admission, [],
                family='family', model='model', initial_failures=initial_failures,
                max_clients=f.plan['max_clients'], capacity_limit=f.plan['capacity_limit'],
                local_free_space_floor_bytes=1,
                retain_admission_after_healthy_harvest=True,
                retain_admission_after_ready_harvest=enabled)
        self.assertEqual(f.jobs, original_jobs)
        self.assertLessEqual(len(f.rows) - baseline_rows, 12 * f.admission.attempts + 16)
        # Every superseded proof stays independently readable with its original bytes.
        for _, value, reference in f.rows:
            self.assertEqual(direct.read_value(reference, core.pinned), value)
        return f, result, worked, phases, candidates

    def ready(self, f, job):
        return [(value, ref) for kind, value, ref in f.rows
                if kind == 'ready' and value['measurement_id'] == job['measurement_id']]

    def claimed(self, f, job):
        destination = core.measurement_path(f.output, job, family='family', model='model')
        self.assertEqual(core.load(destination / 'INPUT.json'), job)
        return core.load(destination / 'ADMISSION_OBSERVATION.json')

    def test_repeated_ready_preserves_original_clocks_and_reduces_admission_guards(self):
        f, result, worked, phases, candidates = self.run_case()
        baseline, old_result, _, old_phases, _ = self.run_case(enabled=False)
        self.assertEqual((result['new_final'], old_result['new_final']), (2, 2))
        self.assertEqual((phases.count('admission'), old_phases.count('admission')), (2, 3))
        self.assertEqual(phases.count('claim_ready'), old_phases.count('claim_ready'))
        self.assertEqual(phases, ['admission', 'claim_ready', 'admission',
                                  'claim_ready', 'claim_ready'])
        self.assertEqual((f.admission.attempts, f.capture_calls), (2, 1))
        self.assertEqual(worked, [job['measurement_id'] for job in f.jobs])
        ready = self.ready(f, f.jobs[-1])
        self.assertEqual(len(ready), 2)
        self.assertEqual(ready[0][0]['candidate'], ready[1][0]['candidate'])
        self.assertNotEqual(ready[0][1], ready[1][1])
        self.assertNotEqual(ready[0][0]['guard'], ready[1][0]['guard'])
        self.assertEqual([row['at'] for row, _ in ready],
                         ['2026-01-01T00:00:00.650000+00:00',
                          '2026-01-01T00:00:00.750000+00:00'])
        saved = self.claimed(f, f.jobs[-1])
        self.assertEqual(saved, candidates[-1])
        self.assertIs(candidates[-1], f.admission.current)
        self.assertEqual(saved['claim_ready'], ready[-1][1])
        self.assertEqual(saved['provider'], candidates[0]['provider'])
        self.assertEqual(saved['observed_from'], '2026-01-01T00:00:00.100000+00:00')
        self.assertEqual(saved['observed_until'], '2026-01-01T00:00:00.350000+00:00')
        self.assertEqual((saved['admission_attempt'], saved['capture_use']), (2, 2))
        self.assertTrue(all(row['input_sha256'] == core.digest(f.jobs[-1]) for row, _ in ready))
        history = core.read_history(f.output / '.control/journal.jsonl')
        self.assertTrue(history[0]['retain_admission_after_ready_harvest'])
        self.assertNotIn('retain_admission_after_ready_harvest',
                         core.read_history(baseline.output / '.control/journal.jsonl')[0])
        self.assertEqual([row['measurement_id'] for row in history if row['event'] == 'claimed'], worked)

    def test_fresh_live_guard_after_late_harvest_still_refuses_claim(self):
        for change in ({'pause_clear': False}, {'ownership_valid': False},
                       {'owner_identity': {'pid': 999}}, {'local_storage_valid': False},
                       {'waiting_requests': 1}, {'preemptions': 1},
                       {'consecutive_failures': 1}):
            with self.subTest(change=change):
                f, result, worked, phases, _ = self.run_case(guard_change=change)
                self.assertEqual((result['new_final'], result['remaining_unclaimed']), (1, 1))
                self.assertEqual(worked, [f.jobs[0]['measurement_id']])
                self.assertEqual((f.admission.attempts, phases.count('claim_ready')), (2, 3))
                self.assertEqual(len(self.ready(f, f.jobs[-1])), 1)
                self.assertTrue(any(kind == 'refusal' for kind, _, _ in f.rows))
                self.assertFalse(core.measurement_path(f.output, f.jobs[-1],
                    family='family', model='model').exists())

    def test_changed_source_after_late_harvest_refuses_before_another_ready(self):
        f, result, worked, phases, _ = self.run_case(source_change=True)
        self.assertIn('synthetic changed immutable source', result['stop_reason'])
        self.assertEqual(worked, [f.jobs[0]['measurement_id']])
        self.assertEqual(phases, ['admission', 'claim_ready', 'admission', 'claim_ready'])
        self.assertEqual(len(self.ready(f, f.jobs[-1])), 1)
        self.assertEqual(result['remaining_unclaimed'], 1)

    def test_expiry_during_harvest_or_after_repeated_ready_renews_same_object(self):
        for expiry in ('harvest', 'after_repeated_ready'):
            with self.subTest(expiry=expiry):
                f, result, worked, _, candidates = self.run_case(expiry=expiry)
                self.assertEqual((result['new_final'], len(worked)), (2, 2))
                self.assertEqual((f.admission.attempts, f.capture_calls, len(candidates)), (3, 2, 2))
                self.assertIs(candidates[-1], f.admission.current)
                ready = self.ready(f, f.jobs[-1])
                self.assertEqual(len(ready), 2 if expiry == 'harvest' else 3)
                self.assertNotEqual(ready[0][0]['candidate'], ready[-1][0]['candidate'])
                if expiry == 'after_repeated_ready':
                    self.assertEqual(ready[0][0]['candidate'], ready[1][0]['candidate'])
                    self.assertNotEqual(ready[0][1], ready[1][1])
                expired = [value for kind, value, _ in f.rows if kind == 'expiry']
                self.assertEqual(len(expired), 1)
                self.assertEqual(expired[0]['admission_attempt'], 2)
                self.assertEqual(expired[0]['candidate'], ready[0][0]['candidate'])
                self.assertIs(expired[0]['claimed'], False)
                saved = self.claimed(f, f.jobs[-1])
                self.assertEqual(saved['claim_ready'], ready[-1][1])
                self.assertEqual(saved['admission_attempt'], 3)
                self.assertNotEqual(saved['provider'], candidates[0]['provider'])
                self.assertEqual(candidates[0]['observed_from'],
                                 '2026-01-01T00:00:00.100000+00:00')
                self.assertGreater(direct.instant(saved['observed_from']),
                                   direct.instant(candidates[0]['observed_until']))

    def test_second_late_completion_uses_original_fallback_before_claim(self):
        for when in ('repeated_prepare', 'locked_gate'):
            with self.subTest(when=when):
                f, result, worked, _, candidates = self.run_case(
                    statuses=('complete', 'complete'), remaining_completion=when)
                self.assertEqual((result['new_final'], f.admission.attempts), (3, 4))
                self.assertCountEqual(worked, [job['measurement_id'] for job in f.jobs])
                ready = self.ready(f, f.jobs[-1])
                self.assertEqual(len(ready), 3)
                self.assertEqual(ready[0][0]['candidate'], ready[1][0]['candidate'])
                self.assertNotEqual(ready[1][0]['candidate'], ready[2][0]['candidate'])
                self.assertEqual(self.claimed(f, f.jobs[-1]), candidates[-1])
                self.assertEqual(candidates[-1]['claim_ready'], ready[-1][1])
                self.assertEqual(candidates[-1]['admission_attempt'], 4)

    def test_changed_failure_history_rebuilds_even_when_final_counter_is_zero(self):
        for statuses, initial in ((('complete',), 1),
                                  (('invalid_answer_schema', 'complete'), 0)):
            with self.subTest(statuses=statuses, initial=initial):
                f, result, _, _, candidates = self.run_case(
                    statuses=statuses, initial_failures=initial)
                self.assertEqual(f.admission.attempts, len(f.jobs) + 1)
                self.assertEqual(result['consecutive_failures'], 0)
                ready = self.ready(f, f.jobs[-1])
                self.assertEqual(len(ready), 2)
                self.assertNotEqual(ready[0][0]['candidate'], ready[1][0]['candidate'])
                self.assertEqual(self.claimed(f, f.jobs[-1]), candidates[-1])
                history = core.read_history(f.output / '.control/journal.jsonl')
                prior = [row for row in history if row['event'] in ('route_success', 'route_failure')
                         and row['measurement_id'] != f.jobs[-1]['measurement_id']]
                self.assertEqual([row['consecutive_failures'] for row in prior],
                                 [0] if initial else [1, 0])
                if initial:
                    self.assertEqual(f.capture_calls, 2)
                    self.assertNotEqual(candidates[-2]['provider'], candidates[-1]['provider'])
                else:
                    failed = core.measurement_path(f.output, f.jobs[0], family='family', model='model')
                    final = core.load(failed / 'FINAL.json')
                    self.assertEqual(final['technical_status'], 'invalid_answer_schema')
                    self.assertIs(final['retry'], False)

    def test_plan_contract_argument_and_early_retention_must_agree(self):
        for target in ('plan', 'contract', 'argument', 'early_retention', 'non_boolean'):
            with self.subTest(target=target):
                f = self.fixture()
                enabled, early = True, True
                if target in ('plan', 'contract'):
                    getattr(f, target).pop('retain_admission_after_ready_harvest')
                elif target == 'argument':
                    enabled = False
                elif target == 'early_retention':
                    early = False
                    for document in (f.plan, f.contract):
                        document['retain_admission_after_healthy_harvest'] = False
                else:
                    enabled = 1
                    for document in (f.plan, f.contract):
                        document['retain_admission_after_ready_harvest'] = 1
                f.admission = f.make()
                worker = mock.Mock()
                with self.assertRaisesRegex(core.IntegrityError, 'ready_harvest_binding'):
                    direct.run_queue(f.output, f.jobs, worker, f.admission, [],
                        family='family', model='model', initial_failures=0, max_clients=2,
                        capacity_limit=None, local_free_space_floor_bytes=1,
                        retain_admission_after_healthy_harvest=early,
                        retain_admission_after_ready_harvest=enabled)
                worker.assert_not_called()
                self.assertFalse((f.output / '.control/journal.jsonl').exists())
                self.assertEqual(f.admission.attempts, 0)


if __name__ == '__main__':
    unittest.main()
