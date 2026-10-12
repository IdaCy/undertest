"""Larger pools require exact provider authority and retain every attempt."""
import base64
import copy
import datetime as dt
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_history as history
from causes import campaign_supplement_launch as launch
from tests.causes import test_campaign_supplement as queue_fixtures
from tests.causes import test_campaign_supplement_history as history_fixtures
from tests.causes import test_campaign_supplement_launch as launch_fixtures


def capacity(f, jobs, cap, budget, *, name='capacity', identity=None):
    identity = identity or {'provider_pid': 123, 'model': 'synthetic', 'gpu_uuid': 'fixture-gpu'}
    review = {'accepted_supplement_capacity_qualification': True,
              'max_clients': cap, 'max_new_requests': budget,
              'provider_identity': identity, 'evidence': [core.pin(f.root / 'context.json')]}
    review_path = f.root / (name + '.review.json'); core.save(review_path, review)
    adoption = {'schema': 'causes.supplement_client_capacity_adoption.v1',
                'root_authorized': True, 'no_user_revocation': True, 'at': core.now(),
                'family': jobs[0]['family'], 'model': jobs[0]['model'],
                'output_root': str(f.output), 'jobs_sha256': core.digest(jobs),
                'max_clients': cap, 'max_new_requests': budget,
                'provider_identity': identity, 'mode': 'qualification',
                'capacity_review': core.pin(review_path)}
    path = f.root / (name + '.json'); core.save(path, adoption)
    return core.pin(path), adoption


class QueueCapacityTests(unittest.TestCase):
    def setUp(self):
        self.f = queue_fixtures.SupplementTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_four_then_eight_real_workers_preserve_original_requests_without_replay(self):
        f = self.f; jobs = [f.job('run' + str(i)) for i in range(12)]
        original = copy.deepcopy(jobs)
        for number, (cap, scope, count) in enumerate(((4, jobs[:4], 4), (8, jobs, 8))):
            ref, adopted = capacity(f, scope, cap, count, name='step' + str(number))
            barrier, lock = threading.Barrier(cap), threading.Lock()
            state = {'active': 0, 'peak': 0}
            def exchange(*args):
                with lock:
                    state['active'] += 1; state['peak'] = max(state['peak'], state['active'])
                try:
                    barrier.wait(timeout=5)
                    return f.exchange(*args)
                finally:
                    with lock: state['active'] -= 1
            with mock.patch.object(runner, 'exchange', side_effect=exchange) as calls:
                result = f.queue(scope, max_clients=cap, capacity_limit=ref,
                                 admission=lambda: {'identity': adopted['provider_identity']})
            self.assertEqual(state['peak'], cap)
            self.assertEqual(calls.call_count, count)
            self.assertEqual(result['new_final'], count)
            with mock.patch.object(runner, 'exchange', side_effect=AssertionError('no replay')):
                again = f.queue(scope, max_clients=cap, capacity_limit=ref,
                                admission=lambda: {'identity': adopted['provider_identity']})
            self.assertEqual(again['new_final'], 0)
        self.assertEqual(jobs, original)
        claims = [r['measurement_id'] for r in f.history() if r['event'] == 'claimed']
        self.assertEqual(claims, [j['measurement_id'] for j in jobs])
        for job in jobs:
            dest = core.measurement_path(f.output, job, family=f.family, model=f.model)
            self.assertEqual(core.load(dest / 'INPUT.json'), job)
            self.assertEqual(core.pin(dest / 'capture/request.json')['sha256'], job['request_file_sha256'])
        starts = [r for r in f.history() if r['event'] == 'controller_started']
        self.assertEqual([r['max_clients'] for r in starts], [4, 4, 8, 8])
        self.assertTrue(all('capacity_limit' in r for r in starts))

    def test_scope_type_and_freshness_refusals_precede_output(self):
        f = self.f; jobs = [f.job('a'), f.job('b')]
        for cap in (0, 3, 5, 9, True, 4.0):
            with self.subTest(cap=cap), self.assertRaisesRegex(core.IntegrityError, 'client_cap'):
                f.queue(jobs, max_clients=cap)
        with self.assertRaisesRegex(core.IntegrityError, 'provider_capacity_adoption_required'):
            f.queue(jobs, max_clients=4)
        ref, adopted = capacity(f, jobs, 4, 2)
        bad = [dict(adopted, root_authorized=False), dict(adopted, no_user_revocation=False),
               dict(adopted, max_clients=True), dict(adopted, model='different'),
               dict(adopted, jobs_sha256='0' * 64), dict(adopted, max_new_requests=True),
               dict(adopted, mode='unknown'),
               dict(adopted, at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat())]
        for i, value in enumerate(bad):
            p = f.root / ('bad' + str(i) + '.json'); core.save(p, value)
            with self.subTest(i=i), self.assertRaises((core.IntegrityError, core.AdmissionHold)):
                f.queue(jobs, max_clients=4, capacity_limit=core.pin(p))
        small, _ = capacity(f, jobs, 4, 1, name='small')
        with self.assertRaisesRegex(core.IntegrityError, 'request_budget'):
            f.queue(jobs, max_clients=4, capacity_limit=small)
        self.assertFalse(f.output.exists())

    def test_changed_live_provider_stops_before_claim(self):
        f = self.f; jobs = [f.job()]; ref, _ = capacity(f, jobs, 4, 1)
        result = f.queue(jobs, max_clients=4, capacity_limit=ref,
                         admission=lambda: {'identity': {'provider_pid': 999}})
        self.assertEqual(result['new_final'], 0)
        self.assertEqual(result['remaining_unclaimed'], 1)
        self.assertIn('provider_capacity_identity_changed', result['stop_reason'])
        self.assertFalse(any(r['event'] == 'claimed' for r in f.history()))

    def test_qualified_capacity_requires_its_distinct_review_flag_and_provider(self):
        f = self.f; jobs = [f.job()]
        _, adopted = capacity(f, jobs, 4, 1)
        adopted['mode'] = 'qualified'
        path = f.root / 'qualified.json'; core.save(path, adopted)
        with self.assertRaisesRegex(core.IntegrityError, 'capacity_review_required'):
            core.validate_capacity_limit(core.pin(path), 4, f.output, jobs, family=f.family, model=f.model)
        review = json.loads(core.pinned(adopted['capacity_review']))
        review['accepted_supplement_provider_capacity'] = True
        review_path = f.root / 'qualified.review.json'; core.save(review_path, review)
        adopted['capacity_review'] = core.pin(review_path)
        path = f.root / 'qualified.accepted.json'; core.save(path, adopted)
        self.assertEqual(core.validate_capacity_limit(core.pin(path), 4, f.output, jobs,
                         family=f.family, model=f.model), adopted)
        review['provider_identity'] = {'provider_pid': 999}
        review_path = f.root / 'qualified.changed.review.json'; core.save(review_path, review)
        adopted['capacity_review'] = core.pin(review_path)
        path = f.root / 'qualified.changed.json'; core.save(path, adopted)
        with self.assertRaisesRegex(core.IntegrityError, 'capacity_review_required'):
            core.validate_capacity_limit(core.pin(path), 4, f.output, jobs, family=f.family, model=f.model)
        self.assertFalse(f.output.exists())

    def test_pause_drains_exact_active_four_without_replacement(self):
        f = self.f; jobs = [f.job('run' + str(i)) for i in range(8)]
        ref, adopted = capacity(f, jobs, 4, 8)
        pause = f.root / 'PAUSE'
        barrier = threading.Barrier(4, action=lambda: pause.write_text('owned fixture pause\n'))
        def worker(job, dest):
            barrier.wait(timeout=5)
            return f.worker(job, dest)
        with mock.patch.object(runner, 'exchange', side_effect=f.exchange):
            result = core.run_queue(f.output, jobs, worker,
                lambda: {'identity': adopted['provider_identity']}, f.source_check, [pause],
                family=f.family, model=f.model, max_clients=4, capacity_limit=ref)
        self.assertEqual((result['new_final'], result['remaining_unclaimed'], result['active']), (4, 4, 0))
        self.assertEqual([r['measurement_id'] for r in f.history() if r['event']=='claimed'],
                         [j['measurement_id'] for j in jobs[:4]])

    def test_eight_inflight_failures_retained_and_never_replayed(self):
        f = self.f; jobs = [f.job('run' + str(i)) for i in range(16)]
        ref, adopted = capacity(f, jobs, 8, 16)
        barrier = threading.Barrier(8)
        def worker(job, dest):
            barrier.wait(timeout=5)
            return {'technical_status': 'failure'}
        result = f.queue(jobs, worker=worker, max_clients=8, capacity_limit=ref,
                         admission=lambda: {'identity': adopted['provider_identity']})
        self.assertEqual((result['new_final'], result['remaining_unclaimed']), (8, 8))
        self.assertEqual(result['consecutive_failures'], 8)
        self.assertEqual(sum(r['event']=='route_failure' for r in f.history()), 8)
        with self.assertRaisesRegex(core.AdmissionHold, 'manual_reconciliation_required'):
            f.queue(jobs, max_clients=8, capacity_limit=ref)


class HighCapacityHistoryTests(unittest.TestCase):
    def batch(self, cap=4, budget=4, count=4):
        actions = [('claim', i) for i in range(count)] + [('success', i) for i in range(count)]
        b = history_fixtures.make_batch('larger', [('continue', 0, actions)], size=count)
        p = json.loads(base64.b64decode(b['invocations'][0]['policy']['base64']))
        p.update(max_clients=cap, max_new_requests=budget,
                 capacity_limit=history_fixtures.pin('/proofs/capacity.json'))
        b['invocations'][0]['policy'] = history_fixtures.embedded('/policies/larger.json', history_fixtures.raw_bundle(p))
        history_fixtures.alter_journal(b, lambda rows: rows[0].update(
            max_clients=cap, max_new_requests=budget, capacity_limit=p['capacity_limit'], queue_prefix_count=count))
        return b

    def replay(self, b):
        return history.replay_native_history(history_fixtures.raw_bundle(history_fixtures.seal_batches([b])))

    def test_four_and_eight_replay_with_explicit_capacity_binding(self):
        for cap in (4, 8):
            result = self.replay(self.batch(cap, cap, cap))
            self.assertEqual(result['batches'][0]['terminal'], cap)
            self.assertFalse(result['runtime_authorized'])

    def test_missing_binding_overflow_and_budget_refuse(self):
        b = self.batch();history_fixtures.alter_journal(b, lambda rows: rows[0].pop('capacity_limit'))
        with self.assertRaisesRegex(ValueError, 'capacity_adoption_changed'): self.replay(b)
        with self.assertRaisesRegex(ValueError, 'client_cap_exceeded'): self.replay(self.batch(4, 5, 5))
        with self.assertRaisesRegex(ValueError, 'capacity_request_budget'): self.replay(self.batch(8, 4, 5))

    def test_cumulative_prefixes_preserve_full_assignment_and_prior_failure(self):
        first = [('claim', i) for i in range(4)] + [('success', i) for i in range(3)] + [('failure', 3)]
        second = [('claim', i) for i in range(4, 12)] + [('success', i) for i in range(4, 12)]
        b = history_fixtures.make_batch('prefix', [('continue', 0, first), ('continue', 1, second)], size=12)
        for i, (cap, count, budget) in enumerate(((4, 4, 4), (8, 12, 8))):
            item = b['invocations'][i]
            p = json.loads(base64.b64decode(item['policy']['base64']))
            p.update(max_clients=cap, queue_prefix_count=count, max_new_requests=budget,
                     capacity_limit=history_fixtures.pin('/proofs/prefix' + str(i) + '.json'))
            item['policy'] = history_fixtures.embedded('/policies/prefix' + str(i) + '.json', history_fixtures.raw_bundle(p))
            def edit(rows):
                rows[item['started_line']-1].update(max_clients=cap, queue_prefix_count=count,
                    capacity_limit=p['capacity_limit'], max_new_requests=budget)
                if i == 0:
                    rows[0]['pending'] = 4
                    next(r for r in rows if r['event']=='controller_finished')['remaining_unclaimed'] = 0
            history_fixtures.alter_journal(b, edit)
        result = self.replay(b)
        self.assertEqual(result['batches'][0]['terminal'], 12)
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual(result['unresolved_events'], 0)
        # Shrinking the later prefix must not hide previously claimed work.
        item = b['invocations'][1]
        p = json.loads(base64.b64decode(item['policy']['base64']))
        p['queue_prefix_count'] = 2
        item['policy'] = history_fixtures.embedded('/policies/regressed.json', history_fixtures.raw_bundle(p))
        history_fixtures.alter_journal(b, lambda rows: rows[item['started_line']-1].update(queue_prefix_count=2))
        with self.assertRaisesRegex(ValueError, 'native_queue_prefix_regressed'):
            self.replay(b)


class HighCapacityLauncherTests(unittest.TestCase):
    def test_reviewed_four_client_continuation_reuses_two_original_pilot_finals(self):
        f = launch_fixtures.LauncherTests(); f.setUp();self.addCleanup(f.doCleanups)
        p, _, _ = f.policy(); jobs = [f.job('run' + str(i)) for i in range(10)]
        prepared = json.loads(core.pinned(p['prepared']))
        prepared.update(ready_jobs=jobs, ready_order=[j['measurement_id'] for j in jobs], contexts=list(f.contexts.values()))
        p['prepared'] = f.save('six.prepared.json', prepared)
        p['host_adapter_source'] = core.pin(Path(__file__).resolve())
        def invoke(policy, name, capref=None):
            ref=f.save(name+'.policy.json', policy)
            adoption=dict(at=core.now(),authorized_supplement_invocation=True,policy=ref,
                          phase=policy['phase'],max_clients=policy['max_clients'],pilot_measurement_ids=policy['pilot_measurement_ids'])
            if capref: adoption['capacity_limit']=capref
            if 'queue_prefix_count' in policy: adoption['queue_prefix_count']=policy['queue_prefix_count']
            ar=f.save(name+'.adoption.json',adoption)
            return launch.launch(ref,ar,runner=runner,observation=lambda:f.observation(policy),replay=f.replay,source_check=lambda:None)
        with mock.patch.object(runner,'exchange',side_effect=f.exchange):
            self.assertEqual(invoke(p,'pilot')['new_final'],2)
        finals=[core.pin(core.measurement_path(f.output,j,family='family',model='model_a')/'FINAL.json')for j in jobs[:2]]
        p.update(phase='continue',max_clients=4,pilot_acceptance=f.save('pilot.accepted.json',dict(accepted_for_continuation=True,prepared=p['prepared'],pilot_measurement_ids=p['pilot_measurement_ids'],finals=finals)))
        capref, adopted=capacity(f,jobs[:6],4,4,identity=p['runtime_identity'])
        p.update(capacity_limit=capref,max_new_requests=4,queue_prefix_count=6)
        p['required_pins'] += [capref,adopted['capacity_review']]
        barrier=threading.Barrier(4)
        def exchange(*args):barrier.wait(timeout=5);return f.exchange(*args)
        with mock.patch.object(runner,'exchange',side_effect=exchange)as calls:
            self.assertEqual(invoke(p,'four',capref)['new_final'],4)
        self.assertEqual(calls.call_count,4)
        self.assertFalse(core.measurement_path(f.output,jobs[6],family='family',model='model_a').exists())
        capref, adopted=capacity(f,jobs,8,4,name='eight',identity=p['runtime_identity'])
        p.update(max_clients=8,capacity_limit=capref,queue_prefix_count=10)
        p['required_pins'] += [capref,adopted['capacity_review']]
        with mock.patch.object(runner,'exchange',side_effect=exchange)as calls:
            self.assertEqual(invoke(p,'eight',capref)['new_final'],4)
        self.assertEqual(calls.call_count,4)


if __name__ == '__main__':
    unittest.main()
