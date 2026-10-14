import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct


class Fixture:
    def __init__(self, root, count=3, clients=2):
        self.root = root
        self.output = root / 'output'
        self.output.mkdir()
        self.time = dt.datetime.now(dt.timezone.utc)
        self.rows = []
        self.capture_calls = self.prepare_calls = 0
        self.ready_delays = []
        self.prepare_delays = []
        self.guard_changes = {}
        self.evidence = self.save('evidence', {'synthetic': True})
        self.identity = {'provider': 'synthetic'}
        self.process = {'pid': 42, 'start_ticks': 123}
        self.jobs = [dict(measurement_id=f'family/model/run{i}/early/question',
                         family='family', model='model', run_id=f'run{i}',
                         stage='early', question_id='question', seed=i)
                     for i in range(count + 1)]
        full_jobs = self.save('jobs', self.jobs)
        self.occupied = [self.jobs[0]['measurement_id']]
        previous = dict(schema='causes.finite_supplement_tranche_closed.v1',
                        active=0, ambiguous=[], clean_native_ending=True,
                        complete_ids=self.occupied, unstarted_ids=[x['measurement_id'] for x in self.jobs[1:]],
                        consecutive_failures=0, qualification_findings=[{'inherited_invalid': True}],
                        full_jobs=full_jobs)
        prior = self.save('predecessor', previous)
        prior_peer = self.save('predecessor_peer', dict(
            accepted_finite_supplement_closed_custody=True, actual=prior, findings=[],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            qualification_findings=previous['qualification_findings']))
        completion = self.save('completed', dict(
            schema='causes.completed_actor_direct_probe_custody.v1', active_actor_roles=[],
            old_actor_owners_absent=True, no_unresolved_actors=True,
            completed_actor_records=[self.evidence], provider_identity=self.identity,
            provider_process=self.process))
        completion_peer = self.save('completed_peer', dict(
            accepted_completed_actor_direct_probe_custody=True, actual=completion, findings=[]))
        self.jobs = self.jobs[1:]
        self.plan = dict(mode=direct.MODE, max_clients=clients, initial_failures=0,
            completed_actor_custody=dict(actual=completion, review=completion_peer),
            predecessor_custody=dict(actual=prior, review=prior_peer),
            maximum_new_requests=count, jobs_sha256=core.digest(self.jobs), occupied_ids=self.occupied,
            provider_identity=self.identity, owner_identity={'pid': 123, 'start_ticks': 456},
            source_epoch='f' * 64, maximum_admission_attempts=32, maximum_captures=16,
            maximum_capture_uses=8, output_root=str(self.output), family='family', model='model',
            capacity_limit=None, pause_paths=[], local_free_space_floor_bytes=1)
        self.contract = dict(schema=direct.CONTRACT_SCHEMA, active_actor_roles=[],
            completed_actor_custody=self.plan['completed_actor_custody'],
            predecessor_custody=self.plan['predecessor_custody'], max_clients=clients,
            server_max_sequences=16, maximum_observation_age_seconds=30,
            claim_ready_age_seconds=29, provider_identity=self.identity,
            provider_process=self.process, baseline_preemptions=0)
        self.admission = self.make()

    def clock(self):
        return self.time.isoformat()

    def advance(self, seconds):
        self.time += dt.timedelta(seconds=seconds)

    def save(self, kind, value):
        path = self.root / (f'{len(self.rows):04d}_{kind}.json')
        core.save(path, value)
        ref = core.pin(path)
        self.rows.append((kind, copy.deepcopy(value), ref))
        if kind == 'ready' and self.ready_delays:
            self.advance(self.ready_delays.pop(0))
        return ref

    def pressure(self):
        return dict(identity=self.identity, process=self.process,
                    running_requests=0, waiting_requests=0, preemptions=0)

    def capture(self):
        begin = self.clock()
        self.advance(.25)
        self.capture_calls += 1
        return dict(schema=direct.PROVIDER_SCHEMA, observed_from=begin,
                    observed_until=self.clock(), healthy=True, evidence=[self.evidence],
                    kernel_error_records=[], **self.pressure())

    def guard(self, phase):
        begin = self.clock()
        self.advance(.1)
        return dict(schema=direct.GUARD_SCHEMA, observed_from=begin,
                    observed_until=self.clock(), evidence=[self.evidence],
                    owner_identity=self.plan['owner_identity'], source_epoch=self.plan['source_epoch'],
                    pause_clear=True, ownership_valid=True, actor_owners_absent=True,
                    provider_exclusive=True, local_storage_valid=True, quarantined=False,
                    consecutive_failures=0, **self.pressure()) | self.guard_changes

    def prepare(self, job):
        self.prepare_calls += 1
        if self.prepare_delays:
            self.advance(self.prepare_delays.pop(0))

    def make(self, **changes):
        kwargs = dict(capture=self.capture, guard=self.guard, retain=self.save,
                      source_check=lambda job: None, prepare_request=self.prepare,
                      clock=self.clock)
        kwargs.update(changes)
        return direct.Admission(self.plan, self.contract, **kwargs)

    def validate(self, jobs=None):
        direct.validate_contract(self.plan, self.contract, self.jobs if jobs is None else jobs,
                                 initial_failures=0, max_clients=self.plan['max_clients'])

    def run(self, worker=None):
        return direct.run_queue(self.output, self.jobs,
            worker or (lambda job, destination: dict(technical_status='complete')),
            self.admission, [], family='family', model='model', initial_failures=0,
            max_clients=self.plan['max_clients'], capacity_limit=self.plan['capacity_limit'],
            local_free_space_floor_bytes=1)


class CompletedActorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = Fixture(Path(temporary.name).resolve())

    def test_exact_completed_and_predecessor_custody_and_original_jobs(self):
        self.f.validate()
        mutated = copy.deepcopy(self.f.jobs)
        mutated[0]['seed'] += 1
        self.f.plan['jobs_sha256'] = core.digest(mutated)
        with self.assertRaisesRegex(core.IntegrityError, 'original_untouched_prefix'):
            self.f.validate(mutated)

    def test_occupied_id_and_reordered_suffix_are_rejected(self):
        self.f.plan['occupied_ids'].append(self.f.jobs[0]['measurement_id'])
        with self.assertRaisesRegex(core.IntegrityError, 'no_replay'):
            self.f.validate()
        self.f.plan['occupied_ids'].pop()
        reordered = list(reversed(self.f.jobs))
        self.f.plan['jobs_sha256'] = core.digest(reordered)
        with self.assertRaisesRegex(core.IntegrityError, 'original_untouched_prefix'):
            self.f.validate(reordered)

    def test_old_actor_reservation_cannot_be_silently_zeroed(self):
        self.f.contract['schema'] = 'old_co_residency_contract'
        with self.assertRaisesRegex(core.IntegrityError, 'explicit_mode'):
            self.f.validate()
        self.f.contract['schema'] = direct.CONTRACT_SCHEMA
        self.f.contract['active_actor_roles'] = ['actor']
        with self.assertRaisesRegex(core.IntegrityError, 'custody_required'):
            self.f.validate()

    def test_missing_or_modified_accepted_custody_refuses(self):
        ref = self.f.plan['completed_actor_custody']['review']
        Path(ref['path']).write_bytes(b'{}\n')
        with self.assertRaises(core.IntegrityError):
            self.f.validate()

    def test_original_provider_clocks_survive_reuse_and_uses_are_bounded(self):
        self.f.plan['maximum_capture_uses'] = 2
        admission = self.f.make()
        first = admission.check()
        second = admission.check()  # The first candidate was canceled/unclaimed.
        self.assertEqual(first['observed_from'], second['observed_from'])
        self.assertEqual(first['provider'], second['provider'])
        self.assertEqual((first['capture_use'], second['capture_use']), (1, 2))
        third = admission.check()
        self.assertNotEqual(first['provider'], third['provider'])
        self.assertEqual(self.f.capture_calls, 2)
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'candidate']), 3)

    def test_expired_cache_refreshes_without_retiming_prior_capture(self):
        first = self.f.admission.check()
        saved = copy.deepcopy(first)
        self.f.advance(30)
        second = self.f.admission.check()
        self.assertEqual(first, saved)
        self.assertNotEqual(first['observed_from'], second['observed_from'])
        self.assertEqual(self.f.capture_calls, 2)

    def test_multiple_preclaim_renewals_preserve_native_object_and_job(self):
        self.f.prepare_delays = [31, 31, 0]
        admission = self.f.admission
        value = admission.check()
        before = copy.deepcopy(self.f.jobs[0])
        admission.prepare(self.f.jobs[0])
        self.assertIs(value, admission.current)
        self.assertEqual(value['admission_attempt'], 3)
        self.assertEqual(self.f.jobs[0], before)
        self.assertEqual(self.f.prepare_calls, 3)
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'expiry']), 2)

    def test_expiry_after_ready_publication_retains_old_ready_proof(self):
        self.f.ready_delays = [31]
        value = self.f.admission.check()
        self.f.admission.prepare(self.f.jobs[0])
        ready = [r for r in self.f.rows if r[0] == 'ready']
        self.assertEqual(len(ready), 2)
        self.assertEqual(value['claim_ready'], ready[-1][2])
        self.assertNotEqual(ready[0][1]['candidate'], ready[1][1]['candidate'])

    def test_bounded_expiry_exhaustion_never_claims_or_calls_worker(self):
        self.f.plan['maximum_admission_attempts'] = 2
        self.f.plan['maximum_captures'] = 2
        self.f.prepare_delays = [31, 31, 31]
        self.f.admission = self.f.make()
        called = []
        result = self.f.run(lambda *args: called.append(args))
        self.assertFalse(called)
        self.assertEqual(result['new_final'], 0)
        self.assertIn('admission_budget_exhausted', result['stop_reason'])
        self.assertEqual(self.f.admission.attempts, 2)

    def test_nonage_admission_hold_is_not_retried(self):
        def refuse(job):
            raise core.AdmissionHold('changed_source_or_route')
        admission = self.f.make(prepare_request=refuse)
        admission.check()
        with self.assertRaisesRegex(core.AdmissionHold, 'changed_source_or_route'):
            admission.prepare(self.f.jobs[0])
        self.assertEqual((admission.attempts, self.f.capture_calls), (1, 1))

    def test_pressure_owner_pause_storage_and_actor_exclusion_changes_hold(self):
        for change in ({'waiting_requests': 1}, {'preemptions': 1},
                       {'owner_identity': {'pid': 999}}, {'pause_clear': False},
                       {'local_storage_valid': False}, {'actor_owners_absent': False}):
            with self.subTest(change=change):
                admission = self.f.make()
                admission.check()
                self.f.guard_changes = change
                with self.assertRaises(core.IntegrityError):
                    admission.prepare(self.f.jobs[0])
                self.assertEqual(admission.attempts, 1)
                self.f.guard_changes = {}

    def test_failure_epoch_change_invalidates_cached_provider(self):
        admission = self.f.admission
        first = admission.check()
        self.f.guard_changes = {'consecutive_failures': 1}
        second = admission.check()
        self.assertNotEqual(first['provider'], second['provider'])
        self.assertEqual(self.f.capture_calls, 2)

    def test_future_clock_and_rollback_are_terminal_not_renewals(self):
        admission = self.f.admission
        admission.check()
        self.f.advance(-1)
        with self.assertRaisesRegex(core.IntegrityError, 'clock_rollback'):
            admission.prepare(self.f.jobs[0])
        self.assertEqual(admission.attempts, 1)
        refusal = [r[1] for r in self.f.rows if r[0] == 'refusal'][-1]
        self.assertLess(direct.instant(refusal['last_clock_read']),
                        direct.instant(refusal['last_accepted_clock']))
        self.assertTrue(refusal['refusal_clock_is_not_new_observation'])

    def test_request_mutation_is_detected_before_native_claim(self):
        def mutate(job):
            job['seed'] += 1
        admission = self.f.make(prepare_request=mutate)
        admission.check()
        with self.assertRaisesRegex(core.IntegrityError, 'request_mutated'):
            admission.prepare(self.f.jobs[0])

    def test_callback_clock_rollback_retains_returned_sample_and_refusal(self):
        for callback in ('guard', 'capture'):
            with self.subTest(callback=callback):
                original = getattr(self.f, callback)
                def backwards(*args):
                    value = original(*args)
                    self.f.advance(-1)
                    return value
                admission = self.f.make(**{callback: backwards})
                with self.assertRaisesRegex(core.IntegrityError, 'clock_rollback'):
                    admission.check()
                sample = [r[1] for r in self.f.rows if r[0] == callback][-1]
                self.assertIn('clock_rollback', sample['clock_error'])
                self.assertIn('value', sample)
                self.assertEqual([r[1] for r in self.f.rows if r[0] == 'refusal'][-1]
                                 ['last_clock_read'], sample['callback_until'])

    def test_expiry_serialization_does_not_request_another_clock(self):
        admission = self.f.admission
        original = admission._age
        expiry_clock = []
        def age(*args, **kwargs):
            try:
                original(*args, **kwargs)
            except direct._Expired:
                expiry_clock.append(admission.last_clock_read)
                raise
        admission._age = age
        self.f.prepare_delays = [31, 0]
        admission.check()
        admission.prepare(self.f.jobs[0])
        expiry = [r[1] for r in self.f.rows if r[0] == 'expiry'][-1]
        self.assertEqual(expiry['at'], expiry_clock[0])
        self.assertTrue(expiry['expiry_clock_is_not_new_observation'])

    def test_native_queue_runs_original_fixed_jobs_without_new_api_or_replay(self):
        result = self.f.run()
        self.assertEqual(result['new_final'], len(self.f.jobs))
        self.assertEqual(result['active'], 0)
        self.assertIsNone(result['stop_reason'])
        events = core.read_history(self.f.output / '.control' / 'journal.jsonl')
        claims = [r for r in events if r['event'] == 'claimed']
        self.assertEqual([r['measurement_id'] for r in claims],
                         [j['measurement_id'] for j in self.f.jobs])
        for job in self.f.jobs:
            value = core.load(core.measurement_path(self.f.output, job,
                family='family', model='model') / 'ADMISSION_OBSERVATION.json')
            ready = direct.read_value(value['claim_ready'], core.pinned)
            self.assertEqual(ready['input_sha256'], core.digest(job))
        with self.assertRaisesRegex(core.IntegrityError, 'fresh_handoff_root'):
            self.f.run()

    def test_eight_clients_still_require_original_native_capacity_adoption(self):
        self.f.plan['max_clients'] = self.f.contract['max_clients'] = 8
        self.f.admission = self.f.make()
        with self.assertRaisesRegex(core.IntegrityError, 'provider_capacity_adoption_required'):
            self.f.run()
        self.assertFalse((self.f.output / '.control' / 'journal.jsonl').exists())

    def test_original_native_eight_client_capacity_path_is_used(self):
        f = self.f
        f.plan['max_clients'] = f.contract['max_clients'] = 8
        peer = f.save('capacity_peer', dict(accepted_supplement_capacity_qualification=True,
            max_clients=8, provider_identity=f.identity, max_new_requests=len(f.jobs),
            evidence=[f.evidence]))
        f.plan['capacity_limit'] = f.save('capacity', dict(
            schema='causes.supplement_client_capacity_adoption.v1', root_authorized=True,
            no_user_revocation=True, max_clients=8, family='family', model='model',
            output_root=str(f.output), jobs_sha256=core.digest(f.jobs),
            max_new_requests=len(f.jobs), provider_identity=f.identity,
            at=core.now(), mode='qualification', capacity_review=peer))
        f.admission = f.make()
        result = f.run()
        self.assertEqual(result['new_final'], len(f.jobs))
        start = core.read_history(f.output / '.control' / 'journal.jsonl')[0]
        self.assertEqual(start['max_clients'], 8)
        self.assertEqual(start['capacity_limit'], f.plan['capacity_limit'])

    def test_exact_ready_bound_is_accepted_and_later_original_clock_renews(self):
        f = self.f
        value = f.admission.check()
        f.time = direct.instant(value['observed_from']) + dt.timedelta(seconds=28.9)
        f.admission.prepare(f.jobs[0])  # Guard takes exactly0.1s: ready age29.
        self.assertEqual(f.admission.attempts, 1)
        self.assertEqual((direct.instant(f.clock()) -
                          direct.instant(value['observed_from'])).total_seconds(), 29)
        f.advance(1.001)
        next_value = f.admission.check()
        self.assertNotEqual(next_value['provider'], value['provider'])

    def test_changed_cached_evidence_refuses_before_claim(self):
        value = self.f.admission.check()
        ref = value['provider']
        Path(ref['path']).write_bytes(b'{}\n')
        with self.assertRaises(core.IntegrityError):
            self.f.admission.prepare(self.f.jobs[0])
        self.assertEqual(self.f.capture_calls, 1)

    def test_capture_budget_stops_before_an_extra_capture(self):
        self.f.plan['maximum_captures'] = 1
        self.f.plan['maximum_capture_uses'] = 1
        admission = self.f.make()
        admission.check()
        with self.assertRaisesRegex(core.AdmissionHold, 'capture_budget_exhausted'):
            admission.check()
        self.assertEqual(self.f.capture_calls, 1)

    def test_slow_guard_is_retained_and_only_age_can_renew(self):
        original = self.f.guard
        once = [True]
        def slow(phase):
            value = original(phase)
            if once.pop() if once else False:
                self.f.advance(31)
            return value
        admission = self.f.make(guard=slow)
        value = admission.check()
        self.assertEqual(value['admission_attempt'], 2)
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'expiry']), 1)

    def test_pressure_refusal_retains_sample_and_does_not_refresh(self):
        self.f.guard_changes = {'waiting_requests': 1}
        with self.assertRaises(core.IntegrityError):
            self.f.admission.check()
        self.assertEqual(self.f.capture_calls, 0)
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'guard']), 1)
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'refusal']), 1)

    def test_forward_clock_cannot_make_old_callback_sample_fresh(self):
        old = self.f.guard('old')
        self.f.advance(1)
        admission = self.f.make(guard=lambda phase: old)
        with self.assertRaisesRegex(core.IntegrityError, 'guard_clock'):
            admission.check()
        self.assertEqual(admission.attempts, 1)

    def test_candidate_or_original_guard_change_refuses_before_claim(self):
        for key in ('candidate', 'guard'):
            with self.subTest(key=key):
                admission = self.f.make()
                value = admission.check()
                Path(value[key]['path']).write_bytes(b'{}\n')
                with self.assertRaises(core.IntegrityError):
                    admission.prepare(self.f.jobs[0])

    def test_nonpinned_or_modified_evidence_is_retained_then_refused(self):
        self.f.guard_changes = {'evidence': [{'not_a_pin': True}]}
        with self.assertRaisesRegex(core.IntegrityError, 'completed_actor_pin'):
            self.f.admission.check()
        self.assertEqual(len([r for r in self.f.rows if r[0] == 'guard']), 1)
        self.f.guard_changes = {}
        Path(self.f.evidence['path']).write_bytes(b'changed')
        with self.assertRaises(core.IntegrityError):
            self.f.make().check()

    def test_worker_exit_integrity_change_stops_native_queue(self):
        changed = []
        def source(job):
            if changed:
                raise core.IntegrityError('changed_during_worker')
        def worker(job, destination):
            changed.append(True)
            return dict(technical_status='complete')
        self.f.admission = self.f.make(source_check=source)
        result = self.f.run(worker)
        self.assertIn(result['stop_reason'],
                      ('integrity_failure', "IntegrityError('changed_during_worker')"))
        events = core.read_history(self.f.output / '.control' / 'journal.jsonl')
        finals = [core.load(r['final']['path']) for r in events if r['event'] == 'route_failure']
        self.assertTrue(finals)
        self.assertTrue(all(r['integrity_failure'] for r in finals))


if __name__ == '__main__':
    unittest.main()
