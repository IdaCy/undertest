"""Real Admission/queue composition with synthetic evidence and no requests."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from test_campaign_supplement_completed_actor import Fixture


class CompletedActorHealthyHarvestTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        f = Fixture(Path(temporary.name).resolve(), count=2, clients=2)
        f.plan['retain_admission_after_healthy_harvest'] = True
        f.contract['retain_admission_after_healthy_harvest'] = True
        return f

    def run_case(self, *, guard_change=None, expiry_at_harvest=False, source_change=False):
        f = self.fixture()
        original_jobs = copy.deepcopy(f.jobs)
        release = threading.Event()
        futures, worked, guard_calls, candidate_objects = [], [], [], []
        original_executor = core.concurrent.futures.ThreadPoolExecutor
        original_journal = core.journal
        original_guard = f.guard

        class TrackedExecutor(original_executor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                futures.append(future)
                return future

        def worker(job, destination):
            worked.append(job['measurement_id'])
            if job is f.jobs[0]:
                if not release.wait(3):
                    raise AssertionError('fixture release missing')
            return {'technical_status': 'complete'}

        def guard(phase):
            guard_calls.append(phase)
            value = original_guard(phase)
            if phase == 'admission' and guard_calls.count('admission') == 2:
                release.set()
                futures[0].result(timeout=3)
            return value

        changed = []
        def source(job):
            if changed:
                raise core.IntegrityError('synthetic changed immutable source')

        f.admission = f.make(guard=guard, source_check=source)
        original_check = f.admission.check
        def check():
            value = original_check()
            candidate_objects.append(value)
            return value
        f.admission.check = check

        def journal(path, value):
            original_journal(path, value)
            if value['event'] == 'route_success' and value['measurement_id'] == f.jobs[0]['measurement_id']:
                if guard_change:
                    f.guard_changes.update(guard_change)
                if expiry_at_harvest:
                    f.advance(31)
                if source_change:
                    changed.append(True)

        with mock.patch.object(core.concurrent.futures, 'ThreadPoolExecutor', TrackedExecutor), \
             mock.patch.object(core, 'journal', side_effect=journal):
            result = direct.run_queue(f.output, f.jobs, worker, f.admission, [],
                family='family', model='model', initial_failures=0, max_clients=2,
                capacity_limit=None, local_free_space_floor_bytes=1,
                retain_admission_after_healthy_harvest=True)
        self.assertEqual(f.jobs, original_jobs)
        return f, result, worked, guard_calls, candidate_objects

    def test_same_candidate_reaches_real_fresh_guards_and_original_claim(self):
        f, result, worked, phases, candidates = self.run_case()
        self.assertEqual(result['new_final'], 2)
        self.assertEqual(phases, ['admission', 'claim_ready', 'admission', 'claim_ready'])
        self.assertEqual(f.admission.attempts, 2)
        self.assertEqual(len(candidates), 2)
        self.assertIs(candidates[-1], f.admission.current)
        destination = core.measurement_path(f.output, f.jobs[-1], family='family', model='model')
        saved = core.load(destination/'ADMISSION_OBSERVATION.json')
        self.assertEqual(saved, candidates[-1])
        self.assertEqual(saved['provider'], candidates[0]['provider'])
        self.assertEqual(saved['observed_from'], candidates[0]['observed_from'])
        self.assertEqual(saved['capture_use'], 2)
        ready = direct.read_value(saved['claim_ready'], core.pinned)
        self.assertEqual(ready['input_sha256'], core.digest(f.jobs[-1]))
        self.assertEqual(worked, [job['measurement_id'] for job in f.jobs])

    def test_expiry_during_harvest_uses_existing_same_object_renewal(self):
        f, result, worked, phases, candidates = self.run_case(expiry_at_harvest=True)
        self.assertEqual((result['new_final'], len(worked)), (2, 2))
        self.assertEqual(f.admission.attempts, 3)
        self.assertEqual(len(candidates), 2)  # Renewal happens inside prepare, not a new native admission.
        self.assertIs(candidates[-1], f.admission.current)
        self.assertEqual(candidates[-1]['admission_attempt'], 3)
        expiry = [row for row in f.rows if row[0] == 'expiry']
        self.assertEqual(len(expiry), 1)
        self.assertFalse(expiry[0][1]['claimed'])
        self.assertEqual(expiry[0][1]['admission_attempt'], 2)
        self.assertNotEqual(candidates[0]['provider'], candidates[-1]['provider'])

    def test_changed_fresh_guard_still_refuses_before_second_claim(self):
        cases = ({'pause_clear': False}, {'ownership_valid': False},
                 {'owner_identity': {'pid': 999}}, {'local_storage_valid': False},
                 {'waiting_requests': 1}, {'preemptions': 1},
                 {'consecutive_failures': 1})
        for changed in cases:
            with self.subTest(changed=changed):
                f, result, worked, phases, _ = self.run_case(guard_change=changed)
                self.assertEqual(result['remaining_unclaimed'], 1)
                self.assertEqual(len(worked), 1)
                self.assertEqual(phases[-1], 'claim_ready')
                self.assertEqual(f.admission.attempts, 2)
                self.assertTrue([row for row in f.rows if row[0] == 'refusal'])

    def test_changed_immutable_source_still_refuses_before_ready_or_claim(self):
        f, result, worked, phases, _ = self.run_case(source_change=True)
        self.assertIn('changed immutable source', result['stop_reason'])
        self.assertEqual(len(worked), 1)
        self.assertEqual(phases, ['admission', 'claim_ready', 'admission'])

    def test_plan_contract_and_argument_must_explicitly_agree(self):
        for target in ('plan', 'contract', 'argument'):
            with self.subTest(target=target):
                f = self.fixture()
                if target in ('plan', 'contract'):
                    getattr(f, target).pop('retain_admission_after_healthy_harvest')
                f.admission = f.make()
                with self.assertRaisesRegex(core.IntegrityError, 'healthy_harvest_binding'):
                    direct.run_queue(f.output, f.jobs, mock.Mock(), f.admission, [],
                        family='family', model='model', initial_failures=0, max_clients=2,
                        capacity_limit=None, local_free_space_floor_bytes=1,
                        retain_admission_after_healthy_harvest=(target != 'argument'))
                self.assertFalse((f.output/'.control'/'journal.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
