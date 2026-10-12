"""Explicit larger pools retain authority, original ordering and held work."""
import base64
import copy
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
from tests.causes.test_supplement_high_capacity import capacity, HighCapacityHistoryTests


class LargerQueueTests(unittest.TestCase):
    def setUp(self):
        self.f = queue_fixtures.SupplementTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_twelve_then_sixteen_real_workers_keep_literal_requests(self):
        f = self.f
        jobs = [f.job('run' + str(i)) for i in range(28)]
        original = copy.deepcopy(jobs)
        for cap, end, new in ((12, 12, 12), (16, 28, 16)):
            scope = jobs[:end]
            ref, adopted = capacity(f, scope, cap, new, name=str(cap))
            barrier, lock = threading.Barrier(cap), threading.Lock()
            state = dict(active=0, peak=0)

            def exchange(*args):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                try:
                    barrier.wait(timeout=5)
                    return f.exchange(*args)
                finally:
                    with lock:
                        state['active'] -= 1

            with mock.patch.object(runner, 'exchange', side_effect=exchange) as calls:
                result = f.queue(scope, max_clients=cap, capacity_limit=ref,
                                 admission=lambda: {'identity': adopted['provider_identity']})
            self.assertEqual((state['peak'], calls.call_count, result['new_final']), (cap, new, new))
        self.assertEqual(jobs, original)
        self.assertEqual([r['measurement_id'] for r in f.history() if r['event'] == 'claimed'],
                         [j['measurement_id'] for j in jobs])
        for job in jobs:
            dest = core.measurement_path(f.output, job, family=f.family, model=f.model)
            self.assertEqual(core.load(dest / 'INPUT.json'), job)
            self.assertEqual(core.pin(dest / 'capture/request.json')['sha256'], job['request_file_sha256'])

    def test_held_sixteen_drains_then_eight_consumes_only_unclaimed(self):
        f = self.f
        jobs = [f.job('run' + str(i)) for i in range(32)]
        ref, adopted = capacity(f, jobs, 16, 32)
        barrier = threading.Barrier(16)
        calls = 0

        def admission():
            nonlocal calls
            calls += 1
            if calls > 16:
                raise core.AdmissionHold('synthetic_capacity_hold')
            return {'identity': adopted['provider_identity']}

        def worker(job, dest):
            barrier.wait(timeout=5)
            return f.worker(job, dest)

        with mock.patch.object(runner, 'exchange', side_effect=f.exchange):
            result = f.queue(jobs, worker=worker, max_clients=16, capacity_limit=ref, admission=admission)
        self.assertEqual((result['new_final'], result['active'], result['remaining_unclaimed']), (16, 0, 16))
        self.assertIn('synthetic_capacity_hold', result['stop_reason'])
        preserved = [core.pin(core.measurement_path(f.output, j, family=f.family, model=f.model) / 'FINAL.json') for j in jobs[:16]]
        ref, adopted = capacity(f, jobs, 8, 16, name='lower')
        with mock.patch.object(runner, 'exchange', side_effect=f.exchange) as exchange:
            result = f.queue(jobs, max_clients=8, capacity_limit=ref,
                             admission=lambda: {'identity': adopted['provider_identity']})
        self.assertEqual((result['existing_final'], result['new_final'], exchange.call_count), (16, 16, 16))
        self.assertEqual([r['measurement_id'] for r in f.history() if r['event'] == 'claimed'], [j['measurement_id'] for j in jobs])
        for ref in preserved:
            core.pinned(ref)

    def test_lower_pool_carries_retained_nonquarantined_failure(self):
        f = self.f
        jobs = [f.job('run' + str(i)) for i in range(3)]
        ref, adopted = capacity(f, jobs, 12, 3)
        claims = 0

        def admission():
            nonlocal claims
            claims += 1
            if claims > 1:
                raise core.AdmissionHold('synthetic_capacity_hold')
            return {'identity': adopted['provider_identity']}

        result = f.queue(jobs, worker=lambda *_: {'technical_status': 'failure'}, max_clients=12,
                         capacity_limit=ref, admission=admission)
        self.assertEqual((result['new_final'], result['consecutive_failures']), (1, 1))
        ref, adopted = capacity(f, jobs, 8, 2, name='lower')
        with mock.patch.object(runner, 'exchange', side_effect=f.exchange):
            result = f.queue(jobs, max_clients=8, capacity_limit=ref,
                             admission=lambda: {'identity': adopted['provider_identity']})
        starts = [r for r in f.history() if r['event'] == 'controller_started']
        self.assertEqual(starts[-1]['inherited_failures'], 1)
        self.assertEqual((result['existing_final'], result['new_final']), (1, 2))
        self.assertEqual(sum(r['event'] == 'route_failure' for r in f.history()), 1)

    def test_quarantine_cannot_be_bypassed_by_lower_pool(self):
        f = self.f
        jobs = [f.job('run' + str(i)) for i in range(20)]
        ref, adopted = capacity(f, jobs, 16, 20)
        barrier = threading.Barrier(16)

        def failed(*_):
            barrier.wait(timeout=5)
            return {'technical_status': 'failure'}

        result = f.queue(jobs, worker=failed, max_clients=16, capacity_limit=ref,
                         admission=lambda: {'identity': adopted['provider_identity']})
        self.assertEqual(result['new_final'], 16)
        ref, _ = capacity(f, jobs, 8, 4, name='lower')
        with self.assertRaisesRegex(core.AdmissionHold, 'manual_reconciliation_required'):
            f.queue(jobs, max_clients=8, capacity_limit=ref)
        self.assertEqual(sum(r['event'] == 'claimed' for r in f.history()), 16)

    def test_exact_larger_authority_and_finite_budgets_required(self):
        f = self.f
        jobs = [f.job('a'), f.job('b')]
        for cap in (12, 16):
            with self.subTest(cap=cap):
                with self.assertRaisesRegex(core.IntegrityError, 'adoption_required'):
                    f.queue(jobs, max_clients=cap)
                ref, adopted = capacity(f, jobs, cap, 2, name=str(cap))
                for key, value in [('max_clients', 8), ('provider_identity', {'wrong': True}), ('max_new_requests', 1)]:
                    changed = dict(adopted, **{key: value})
                    path = f.root / (str(cap) + key + '.json')
                    core.save(path, changed)
                    with self.assertRaises(core.IntegrityError):
                        f.queue(jobs, max_clients=cap, capacity_limit=core.pin(path))
        for cap in (9, 15, 17, True, 16.0):
            with self.assertRaisesRegex(core.IntegrityError, 'client_cap'):
                f.queue(jobs, max_clients=cap)
        self.assertFalse(f.output.exists())


class LargerHistoryTests(unittest.TestCase):
    def test_twelve_and_sixteen_native_history_and_overflow(self):
        fixture = HighCapacityHistoryTests()
        for cap in (12, 16):
            result = fixture.replay(fixture.batch(cap, cap, cap))
            self.assertEqual(result['batches'][0]['terminal'], cap)
            with self.assertRaisesRegex(ValueError, 'client_cap_exceeded'):
                fixture.replay(fixture.batch(cap, cap + 1, cap + 1))

    def test_lower_continuation_keeps_partial_prefix_and_failure(self):
        segments = [('continue', 0, [('claim', 0), ('failure', 0)]),
                    ('continue', 1, [('claim', i) for i in range(1, 9)] + [('success', i) for i in range(1, 9)])]
        batch = history_fixtures.make_batch('downshift', segments, size=9)
        for index, cap in enumerate((16, 8)):
            item = batch['invocations'][index]
            policy = json.loads(base64.b64decode(item['policy']['base64']))
            policy.update(max_clients=cap, max_new_requests=9-index, queue_prefix_count=9,
                          capacity_limit=history_fixtures.pin('/proofs/capacity' + str(index)))
            item['policy'] = history_fixtures.embedded('/policies/' + str(index), history_fixtures.raw_bundle(policy))
            history_fixtures.alter_journal(batch, lambda rows: rows[item['started_line']-1].update(
                max_clients=cap, max_new_requests=9-index, queue_prefix_count=9, capacity_limit=policy['capacity_limit']))
        result = history.replay_native_history(history_fixtures.raw_bundle(history_fixtures.seal_batches([batch])))
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['batches'][0]['terminal'], 9)
        self.assertEqual(result['unresolved_events'], 0)


class LargerLauncherTests(unittest.TestCase):
    def test_larger_caps_reach_exact_adoption_gate(self):
        f = launch_fixtures.LauncherTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        policy, _, _ = f.policy()
        for cap in (12, 16):
            selected = dict(policy, max_clients=cap, host_adapter_source=core.pin(Path(__file__).resolve()))
            ref = f.save(str(cap) + '.policy.json', selected)
            adoption = f.save(str(cap) + '.adoption.json', dict(
                at=core.now(), authorized_supplement_invocation=True, policy=ref,
                phase=selected['phase'], max_clients=8, pilot_measurement_ids=selected['pilot_measurement_ids']))
            with self.assertRaisesRegex(core.IntegrityError, 'adoption_scope_changed'):
                launch.launch(ref, adoption, runner=runner, observation=lambda: self.fail('no observation'),
                              replay=f.replay, source_check=lambda: None)
        self.assertFalse(f.output.exists())


if __name__ == '__main__':
    unittest.main()
